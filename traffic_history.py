"""Private, rolling 72-hour top-K history for the existing proxy sampler."""
import argparse
import collections
import datetime as dt
import json
import logging
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

from settings import ZONE, PROXY_PORTS
WINDOW = 10
TOP_K = 20
RETENTION = 72 * 3600
DEFAULT_DB = Path(__file__).resolve().parent / 'data/history.sqlite3'


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, timeout=1)
        self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS windows '
                        '(id INTEGER PRIMARY KEY, started REAL NOT NULL, '
                        'ended REAL NOT NULL, payload TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS windows_end ON windows(ended)')
        self.db.commit()
        self.last_prune = 0

    def append(self, record, now=None):
        now = time.time() if now is None else now
        payload = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
        with self.db:
            self.db.execute('INSERT INTO windows(started,ended,payload) VALUES (?,?,?)',
                            (record['started_epoch'], record['ended_epoch'], payload))
            if now - self.last_prune >= 60:
                self.db.execute('DELETE FROM windows WHERE ended<=?', (now - RETENTION,))
                self.last_prune = now
        if self.last_prune == now:
            self.db.execute('PRAGMA incremental_vacuum(64)')
            self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')

    def close(self):
        self.db.close()


class ProcessResolver:
    """Best-effort loopback socket ownership; never changes proxy configuration."""
    def __init__(self, folder):
        self.folder = Path(folder)
        self.ports = {}
        self.refreshed = 0
        self.error = None

    def refresh(self):
        if time.monotonic() - self.refreshed < 2:
            return
        self.refreshed = time.monotonic()
        receipt = self.folder / 'history-lsof.pid.json'
        proc = subprocess.Popen(
            ['/usr/sbin/lsof', '-nP', '-iTCP', '-sTCP:ESTABLISHED', '-Fpcn'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        receipt.write_text(json.dumps({'pid': proc.pid, 'executable': '/usr/sbin/lsof'}))
        os.chmod(receipt, 0o600)
        try:
            try:
                output, _ = proc.communicate(timeout=1.5)
            except subprocess.TimeoutExpired:
                owned = json.loads(receipt.read_text())
                check = subprocess.run(
                    ['/bin/ps', '-p', str(proc.pid), '-o', 'uid=,comm='],
                    capture_output=True, text=True).stdout.strip().split(None, 1)
                if (owned['pid'] == proc.pid > 1 and len(check) == 2
                        and check[0] == str(os.getuid()) and check[1] == '/usr/sbin/lsof'):
                    proc.kill()
                    proc.communicate()
                raise TimeoutError('lsof ownership lookup timed out')
            if proc.returncode not in (0, 1):
                raise RuntimeError('lsof ownership lookup failed')
            allowed_ports = '|'.join(str(port) for port in PROXY_PORTS)
            ports = {}
            pid = name = None
            for line in output.splitlines():
                if line.startswith('p'):
                    pid = int(line[1:])
                elif line.startswith('c'):
                    name = line[1:][:120]
                elif line.startswith('n'):
                    match = re.fullmatch(
                        r'(127\.0\.0\.1|\[::1\]):(\d+)->'
                        rf'(?:127\.0\.0\.1|\[::1\]):(?:{allowed_ports})', line[1:])
                    if match:
                        ports[(match[1].strip('[]'), match[2])] = (name, pid)
            self.ports = ports
            self.error = None
        finally:
            receipt.unlink(missing_ok=True)

    def identify(self, flow):
        name = flow.get('process') or Path(flow.get('process_path') or '').name
        if name:
            return str(name)[:120], None, 'metadata'
        name, pid = self.ports.get(
            (flow.get('source_ip'), str(flow.get('source_port'))), (None, None))
        return (name, pid, 'lsof') if name else ('unknown', None, 'unknown')


class Window:
    def __init__(self, now):
        self.bucket = int(now.timestamp()) // WINDOW
        self.started = self.ended = now.timestamp()
        self.samples = 0
        self.targets = {}
        self.errors = set()
        self.daily = None
        self.dropped_samples = 0

    def add(self, now, flows, daily, resolver, error=None, dropped=0):
        self.ended = now.timestamp()
        self.samples += 1
        self.daily = daily
        self.dropped_samples = dropped
        if error:
            self.errors.add(str(error)[:80])
        for flow in flows:
            process, pid, source = resolver.identify(flow)
            host = str(flow.get('host') or 'unknown')[:160]
            entry = self.targets.setdefault((host, process), dict(
                host=host, process=process, upload=0, download=0, pids=set(),
                attribution=set(), flows=set()))
            for key in ('upload', 'download'):
                entry[key] += max(0, int(flow.get(key, 0)))
            if pid:
                entry['pids'].add(pid)
            entry['attribution'].add(source)
            if flow.get('flow_id'):
                entry['flows'].add(flow['flow_id'])

    def record(self):
        rows = sorted(self.targets.values(),
                      key=lambda row: row['upload'] + row['download'], reverse=True)
        top = []
        for row in rows[:TOP_K]:
            item = {key: row[key] for key in ('host', 'process', 'upload', 'download')}
            item.update(pids=sorted(row['pids'])[:16],
                        attribution=sorted(row['attribution']),
                        connections=len(row['flows']))
            top.append(item)
        return dict(version=1, started_epoch=self.started, ended_epoch=self.ended,
                    started_at=dt.datetime.fromtimestamp(self.started, ZONE).isoformat(),
                    ended_at=dt.datetime.fromtimestamp(self.ended, ZONE).isoformat(),
                    top_k=TOP_K, rank_by='upload+download', samples=self.samples,
                    top=top, other_targets=max(0, len(rows)-TOP_K),
                    other_upload=sum(row['upload'] for row in rows[TOP_K:]),
                    other_download=sum(row['download'] for row in rows[TOP_K:]),
                    daily=self.daily, sampler_errors=sorted(self.errors),
                    dropped_samples_total=self.dropped_samples)


class HistoryRecorder:
    """All socket lookups and disk operations run outside the guard's hot path."""
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.queue = queue.Queue(maxsize=200)
        self.stopping = threading.Event()
        self.dropped = 0
        self.last_error = None
        self.last_written_at = None
        self.thread = threading.Thread(target=self.run, name='traffic-history', daemon=True)
        self.thread.start()

    def submit(self, now, observations, daily, error=None):
        try:
            self.queue.put_nowait((now, observations, daily, error, self.dropped))
        except queue.Full:
            self.dropped += 1

    def status(self):
        return dict(enabled=True, window_seconds=WINDOW, top_k=TOP_K,
                    retention_hours=RETENTION//3600, last_written_at=self.last_written_at,
                    last_error=self.last_error, dropped_samples=self.dropped)

    def run(self):
        store = None
        window = None
        resolver = ProcessResolver(self.path.parent)
        try:
            store = HistoryStore(self.path)
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    now, flows, daily, error, dropped = self.queue.get(timeout=.2)
                except queue.Empty:
                    if window and time.time() >= (window.bucket+1)*WINDOW:
                        self.write(store, window)
                        window = None
                    continue
                if window and int(now.timestamp())//WINDOW != window.bucket:
                    self.write(store, window)
                    window = None
                if window is None:
                    window = Window(now)
                try:
                    resolver.refresh()
                except Exception as lookup_error:
                    resolver.ports = {}
                    resolver.error = type(lookup_error).__name__
                window.add(now, flows, daily, resolver, error=error, dropped=dropped)
                if resolver.error:
                    window.errors.add('process_lookup:'+resolver.error)
            if window:
                self.write(store, window)
        except Exception as error:
            self.last_error = type(error).__name__
            logging.exception('Traffic history worker stopped')
        finally:
            if store:
                store.close()

    def write(self, store, window):
        try:
            store.append(window.record())
            self.last_written_at = dt.datetime.now(ZONE).isoformat()
            self.last_error = None
        except Exception as error:
            self.last_error = type(error).__name__
            logging.error('Traffic history write failed: %s', self.last_error)

    def close(self):
        self.stopping.set()
        self.thread.join(4)


def software_name(process, host=''):
    name = process.casefold()
    if name == 'codex' or name.startswith(('codex (', 'chatgpt')):
        return 'Codex / ChatGPT'
    if name.startswith('google chrome'):
        return 'Google Chrome'
    if name.startswith('microsoft edge'):
        return 'Microsoft Edge'
    if name == 'agy' or (name == 'language_server' and host.endswith('.googleapis.com')):
        return 'Antigravity / agy'
    if name in ('claude', 'claude.exe'):
        return 'Claude Code'
    if name.startswith('adspower'):
        return 'AdsPower'
    if name.startswith('morelogin'):
        return 'MoreLogin'
    if name == 'egress-guard':
        return '本地代理网关（转发）'
    if name in ('unknown', ''):
        return '未识别进程'
    return process


def query(path, since, until, sort='download', top=20, now=None, group_by='target'):
    now = time.time() if now is None else now
    since = max(since, now-RETENTION)
    result = dict(requested_since=dt.datetime.fromtimestamp(since, ZONE).isoformat(),
                  requested_until=dt.datetime.fromtimestamp(until, ZONE).isoformat(),
                  windows=0, other_upload=0, other_download=0, top=[],
                  observed_upload=0, observed_download=0, sampler_errors=[],
                  dropped_samples_total=0, recorded_since=None, recorded_until=None,
                  group_by=group_by)
    if not Path(path).exists():
        return result
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
    groups = collections.defaultdict(lambda: dict(upload=0, download=0, windows=0))
    details = collections.defaultdict(lambda: dict(hosts=set(), processes=set()))
    errors = set()
    try:
        for (text,) in db.execute(
                'SELECT payload FROM windows WHERE ended>? AND started<? ORDER BY ended',
                (since, until)):
            record = json.loads(text)
            result['windows'] += 1
            if result['recorded_since'] is None:
                result['recorded_since'] = record['started_at']
            result['recorded_until'] = record['ended_at']
            errors.update(record['sampler_errors'])
            result['dropped_samples_total'] = max(
                result['dropped_samples_total'], record['dropped_samples_total'])
            for direction in ('upload', 'download'):
                result['other_'+direction] += record['other_'+direction]
                result['observed_'+direction] += record['other_'+direction]
            for row in record['top']:
                key = ('', software_name(row['process'], row['host'])) \
                    if group_by == 'software' else (row['host'], row['process'])
                group = groups[key]
                if group_by == 'software':
                    details[key]['hosts'].add(row['host'])
                    details[key]['processes'].add(row['process'])
                group['windows'] += 1
                for direction in ('upload', 'download'):
                    group[direction] += row[direction]
                    result['observed_'+direction] += row[direction]
    finally:
        db.close()
    key = (lambda item: sum(item[1][direction] for direction in ('upload', 'download'))) \
        if sort == 'total' else lambda item: item[1][sort]
    result['top'] = [dict(host=host, process=process, **counts)
                     for (host, process), counts in sorted(
                         groups.items(), key=key, reverse=True)[:top]]
    if group_by == 'software':
        for row in result['top']:
            detail = details[('', row['process'])]
            row.update(hosts=sorted(detail['hosts'])[:5],
                       target_count=len(detail['hosts']), processes=sorted(detail['processes']))
    result['sampler_errors'] = sorted(errors)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    period = parser.add_mutually_exclusive_group()
    period.add_argument('--today', action='store_true')
    period.add_argument('--hours', type=float, default=24)
    period.add_argument('--since', help='ISO local time, e.g. 2026-11-11T14:30:00+08:00')
    parser.add_argument('--until', help='ISO local time (default: now)')
    parser.add_argument('--sort', choices=['upload', 'download', 'total'], default='download')
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--group', choices=['target', 'software'], default='target')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    now = dt.datetime.now(ZONE)
    def parse(value):
        parsed = dt.datetime.fromisoformat(value)
        return parsed.replace(tzinfo=ZONE) if parsed.tzinfo is None else parsed
    if args.today:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif args.since:
        since = parse(args.since)
    else:
        if not 0 < args.hours <= 72:
            parser.error('--hours must be greater than 0 and at most 72')
        since = now-dt.timedelta(hours=args.hours)
    until = parse(args.until) if args.until else now
    if until <= since or not 1 <= args.top <= 100:
        parser.error('use an increasing time range and --top between 1 and 100')
    result = query(args.db, since.timestamp(), until.timestamp(), args.sort, args.top,
                   group_by=args.group)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print('Recorded:', result['recorded_since'], 'to', result['recorded_until'])
    print('WINDOWS:', result['windows'], '(10-second top-20 records; retention: 72 hours)')
    print('DOWNLOAD MB  UPLOAD MB  PROCESS / HOST')
    for row in result['top']:
        print(f"{row['download']/1e6:11.3f}  {row['upload']/1e6:9.3f}  "
              f"{row['process']} / {row['host']}")
    print('Other targets: download %.3f MB, upload %.3f MB' %
          (result['other_download']/1e6, result['other_upload']/1e6))
    print('Connection observations, not billing. Unseen closed tails remain unclassified; '
          'each target total covers only windows where it entered the top 20. '
          'Time boundaries may include a partial 10-second window.')
    if result['sampler_errors'] or result['dropped_samples_total']:
        print('Coverage warnings:', result['sampler_errors'],
              'dropped samples:', result['dropped_samples_total'])


if __name__ == '__main__':
    main()
