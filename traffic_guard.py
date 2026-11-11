#!/usr/bin/env python3
"""Daily local Clash traffic warnings; an unacknowledged warning trips a latch."""
import argparse
import copy
import collections
import datetime as dt
import fcntl
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import proxy_accounting as accounting
import traffic_history

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
STEP_BYTES = 10_000_000_000
INTERVAL = 0.1
CHECKPOINT_INTERVAL = 1
STATUS_INTERVAL = 1
from settings import ZONE
STOP = threading.Event()
POPUPS = []


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.next')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write('\n')
    temporary.replace(path)


def initial_state(now):
    return dict(version=1, day=now.astimezone(ZONE).date().isoformat(),
                tracking_started_at=now.isoformat(), daily_bytes=0, last_bucket=0,
                previous=None, pending=None, blocked=False, blocking_alert=None,
                restore=None, last_error=None)


def threshold_events(state, now):
    if state['blocked']:
        return []
    bucket = state['daily_bytes'] // STEP_BYTES
    events = []
    while state['last_bucket'] < bucket:
        state['last_bucket'] += 1
        alert = dict(id=uuid.uuid4().hex, day=state['day'], bucket=state['last_bucket'],
                     used_bytes=state['daily_bytes'], created_at=now.isoformat())
        if state.get('version') == 2:
            alert['accounting'] = accounting.totals(state['meter'])
        if state.get('pending'):
            state.update(blocked=True, blocked_at=now.isoformat(), blocking_alert=alert,
                         block_reason='previous_traffic_warning_not_acknowledged')
            events.append(('block', alert))
            break
        state['pending'] = alert
        events.append(('warning', alert))
    return events


def advance(state, counters, now):
    """Classify real snapshots; retain the legacy path for old receipts/mocks."""
    day = now.astimezone(ZONE).date().isoformat()
    previous = state.get('previous')
    legacy_delta = 0
    if previous is not None:
        changed = previous['generation'] != counters['generation']
        for key in ('upload', 'download'):
            current, before = int(counters[key]), int(previous[key])
            delta = current if changed or current < before else current - before
            legacy_delta += max(0, delta)
    if state['day'] != day:
        state.update(day=day, daily_bytes=0, last_bucket=0)
    if 'connections' in counters:
        if state.get('version') != 2:
            # Old totals cannot be retroactively split. Preserve them explicitly
            # as historical uncertainty so upgrading cannot forgive a threshold.
            state['meter'] = accounting.new_meter(
                counters, now, legacy_bytes=state['daily_bytes'] + legacy_delta)
            state['version'] = 2
            counters['_proxy_observations'] = []
        else:
            counters['_proxy_observations'] = accounting.update(state['meter'], counters, now)
        state['daily_bytes'] = accounting.totals(state['meter'])['budget_bytes']
    else:
        if state.get('version') == 2:
            raise RuntimeError('Classified accounting requires connection snapshots')
        state['daily_bytes'] += legacy_delta
    state['previous'] = {key: counters[key] for key in ('upload', 'download', 'generation')}
    state['checked_at'] = now.isoformat()
    return threshold_events(state, now)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=3):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
        try:
            # macOS LOCAL_PEERPID identifies the actual core process, unlike a
            # socket inode which can change during a controller-only rebind.
            self.peer_pid = self.sock.getsockopt(0, 2)
        except OSError:
            self.peer_pid = None


class Controller:
    def __init__(self, config):
        self.path = config.get('controller_socket')
        self.address = config.get('controller_url', 'http://127.0.0.1:9090')
        self.secret = config.get('controller_secret', '')
        if self.path:
            self.path = str(Path(self.path).expanduser())
        else:
            address = urlparse(self.address)
            if (address.scheme not in ('http', 'https') or
                    address.hostname not in ('127.0.0.1', 'localhost', '::1') or
                    address.username or address.password or address.query or
                    address.fragment or address.path not in ('', '/')):
                raise ValueError('controller_url must be a loopback HTTP(S) origin')
            self.endpoint = address
        self.connection = None

    def close(self):
        if self.connection:
            self.connection.close()
        self.connection = None

    def call(self, method, path, body=None):
        if self.connection is None:
            if self.path:
                self.connection = UnixHTTP(self.path)
            else:
                connection_class = http.client.HTTPSConnection if self.endpoint.scheme == 'https' \
                    else http.client.HTTPConnection
                self.connection = connection_class(
                    self.endpoint.hostname, self.endpoint.port, timeout=3)
        headers = {'Authorization': 'Bearer ' + self.secret}
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        try:
            self.connection.request(method, path, body=payload, headers=headers)
            self.core_pid = getattr(self.connection, 'peer_pid', None)
            response = self.connection.getresponse()
            data = response.read()
            if not 200 <= response.status < 300:
                raise RuntimeError('Clash controller HTTP ' + str(response.status))
            if response.will_close:
                self.close()
            return json.loads(data) if data else {}
        except Exception:
            self.close()
            raise

    def counters(self):
        info = self.call('GET', '/connections')
        generation = str(os.stat(self.path).st_ino) if self.path else self.address
        return dict(upload=int(info['uploadTotal']), download=int(info['downloadTotal']),
                    generation=generation, core_pid=getattr(self, 'core_pid', None),
                    connections=info.get('connections') or [])

    def snapshot(self):
        config = self.call('GET', '/configs')
        group = self.call('GET', '/proxies/GLOBAL')
        if 'REJECT' not in group.get('all', []):
            raise RuntimeError('Clash GLOBAL selector has no REJECT option')
        return dict(mode=config['mode'], selection=group['now'])

    def block(self):
        # Retain the listening proxy: quitting it could cause clients to use direct access.
        self.call('PUT', '/proxies/GLOBAL', {'name': 'REJECT'})
        self.call('PATCH', '/configs', {'mode': 'global'})
        self.call('DELETE', '/connections')
        current = self.snapshot()
        if current != dict(mode='global', selection='REJECT'):
            raise RuntimeError('Clash traffic block could not be confirmed')

    def restore(self, previous):
        if not previous or previous.get('selection') == 'REJECT':
            raise RuntimeError('No previous usable proxy selection was saved')
        self.call('PUT', '/proxies/GLOBAL', {'name': previous['selection']})
        self.call('PATCH', '/configs', {'mode': previous['mode']})


def ack_path(alert):
    return DATA / 'alerts' / (alert['id'] + '.ack.json')


def acknowledged(alert):
    try:
        value = json.loads(ack_path(alert).read_text())
        return value.get('id') == alert['id'] and value.get('acknowledged') is True
    except (OSError, ValueError):
        return False


def read_acknowledgments(state):
    pending = state.get('pending')
    if pending and acknowledged(pending):
        state['pending'] = None
        state['last_acknowledged_at'] = dt.datetime.now(ZONE).isoformat()


def popup_alive(alert):
    pid = alert.get('popup_pid', 0)
    if pid <= 1:
        return False
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=', '-o', 'command='],
                            capture_output=True, text=True)
    pieces = result.stdout.strip().split(None, 1)
    return (result.returncode == 0 and len(pieces) == 2 and pieces[0] == str(os.getuid())
            and str(DATA / 'TrafficGuardAlert') in pieces[1] and alert['id'] in pieces[1])


def show_popup(alert, blocked=False):
    if time.time() < alert.get('_popup_checked_until', 0):
        return
    alert['_popup_checked_until'] = time.time() + 2
    if acknowledged(alert) or popup_alive(alert) or time.time() < alert.get('retry_after', 0):
        return
    request_path = DATA / 'alerts' / (alert['id'] + '.json')
    used = alert['used_bytes'] / 1_000_000_000
    if blocked:
        title = '代理已切断'
        message = (f"今日代理告警预算 {used:.2f} GB。\n\n"
                   '上一条流量警告尚未关闭，已触发代理切断。\n'
                   '关闭本窗口不会恢复代理；需要手动恢复。')
    else:
        title = '代理流量警告'
        message = (f"今日代理告警预算 {used:.2f} GB，已达到 {alert['bucket'] * 10} GB 提醒线。\n\n"
                   '每增加 10 GB 提醒一次。下一档到达时，若本窗口仍未关闭，将切断代理。\n'
                   '关闭窗口表示已知晓。预算包含已确认代理及未归类余量，排除已确认直连。')
    atomic_json(request_path, dict(id=alert['id'], title=title, message=message,
                                  ackPath=str(ack_path(alert))))
    proc = subprocess.Popen([str(DATA / 'TrafficGuardAlert'), '--request', str(request_path)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    POPUPS.append(proc)
    alert['popup_pid'] = proc.pid
    alert['retry_after'] = time.time() + 30
    atomic_json(request_path.with_suffix('.pid.json'), dict(pid=proc.pid, id=alert['id']))


def load_state():
    path = DATA / 'state.json'
    if path.exists():
        value = json.loads(path.read_text())
        if value.get('version') not in (1, 2):
            raise RuntimeError('Unsupported traffic guard state; refusing to reset usage')
        return value
    return initial_state(dt.datetime.now(ZONE))


def run_locked(action):
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    if action == 'status':
        print(json.dumps(load_state(), ensure_ascii=False, indent=2))
        return
    with (DATA / 'control.lock').open('a') as lock:
        if action == 'resume':
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = load_state()
            if not state['blocked']:
                print('Proxy is not blocked by the daily traffic guard.'); return
            controller = Controller(json.loads((ROOT / 'config.json').read_text()))
            controller.restore(state.get('restore'))
            for alert in (state.get('pending'), state.get('blocking_alert')):
                if alert:
                    atomic_json(ack_path(alert), dict(id=alert['id'], acknowledged=True,
                                closed_at=dt.datetime.now(ZONE).isoformat(), reason='manual_resume'))
            state.update(blocked=False, pending=None, blocking_alert=None, restore=None,
                         last_bucket=state['daily_bytes'] // STEP_BYTES,
                         resumed_at=dt.datetime.now(ZONE).isoformat())
            atomic_json(DATA / 'state.json', state)
            print('Proxy restored. The daily counter is retained; the next 10 GB tier will warn again.')
            return
    run_daemon()


def tick(state, controller, now, persist=True):
    read_acknowledgments(state)
    if state['blocked']:
        if state.get('restore') is None:
            previous = controller.snapshot()
            if previous['selection'] != 'REJECT':
                state['restore'] = previous
                atomic_json(DATA / 'state.json', state)
        controller.block()
    sample = controller.counters()
    read_acknowledgments(state)
    candidate = copy.deepcopy(state)
    events = advance(candidate, sample, now)
    new_block = any(kind == 'block' for kind, _ in events)
    if new_block:
        previous = controller.snapshot()
        # A user may close the window while the local controller is responding.
        # Re-evaluate using the same counters after checking the close receipt again.
        read_acknowledgments(state)
        candidate = copy.deepcopy(state)
        events = advance(candidate, sample, now)
        new_block = any(kind == 'block' for kind, _ in events)
        if new_block:
            candidate['restore'] = previous
    state.clear()
    state.update(candidate)
    # Persist the latch and restore point before changing the controller.
    if persist or events:
        atomic_json(DATA / 'state.json', state)
    if new_block:
        controller.block()
        logging.warning('Blocked Clash: previous warning was not acknowledged; daily bytes=%d', state['daily_bytes'])
    if state.get('pending'):
        show_popup(state['pending'])
    if state.get('blocking_alert'):
        show_popup(state['blocking_alert'], blocked=True)
    return sample.get('_proxy_observations', [])


def public_status(state, history):
    now = dt.datetime.now(ZONE)
    meter = state.get('meter')
    result = dict(version=1, checked_at=now.isoformat(), sample_at=state.get('checked_at'),
                  day=state['day'], blocked=state['blocked'],
                  warning_pending=state.get('pending') is not None,
                  next_warning_bytes=(state['last_bucket'] + 1) * STEP_BYTES,
                  last_error=state.get('last_error'), upload_bps=0, download_bps=0,
                  top=[], sample_interval_ms=100)
    if not meter:
        result.update(status='starting', proxy_bytes=0, direct_bytes=0, unknown_bytes=0,
                      historical_bytes=state['daily_bytes'], budget_bytes=state['daily_bytes'])
        return result
    result.update(accounting.totals(meter))
    result.update(status='blocked' if state['blocked'] else
                  'unavailable' if state.get('last_error') else 'monitoring',
                  proxy_upload=meter['proxy']['upload'],
                  proxy_download=meter['proxy']['download'],
                  started_at=meter['day_started_at'],
                  current_proxy_connections=meter['current_proxy_connections'],
                  current_direct_connections=meter['current_direct_connections'],
                  coverage_gaps=meter['coverage_gaps'])
    if len(history) > 1:
        first, last = history[0], history[-1]
        seconds = max(.001, last[0] - first[0])
        result['upload_bps'] = max(0, last[1] - first[1]) / seconds
        result['download_bps'] = max(0, last[2] - first[2]) / seconds
        hosts = {}
        for row in list(history)[1:]:
            for flow in row[3]:
                host = hosts.setdefault(flow['host'], dict(upload=0, download=0))
                host['upload'] += flow['upload']
                host['download'] += flow['download']
        result['top'] = [dict(host=host, upload_bps=counts['upload']/seconds,
                             download_bps=counts['download']/seconds)
                         for host, counts in sorted(hosts.items(),
                             key=lambda item: sum(item[1].values()), reverse=True)[:5]]
    return result


def run_daemon():
    with (DATA / 'daemon.lock').open('a') as lifetime:
        fcntl.flock(lifetime, fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic_json(DATA / 'daemon.pid.json', {'pid': os.getpid(), 'script': str(Path(__file__).resolve())})
        handler = RotatingFileHandler(DATA / 'guard.log', maxBytes=1_000_000, backupCount=2)
        logging.basicConfig(handlers=[handler], level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s')
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: STOP.set())
        state = load_state()
        last_saved = (DATA / 'state.json').stat().st_mtime_ns if (DATA / 'state.json').exists() else None
        config_stamp = None
        controller = None
        last_error = None
        history = collections.deque()
        recorder = traffic_history.HistoryRecorder(DATA / 'history.sqlite3')
        last_flush = last_status = 0
        last_day = state['day']
        try:
            while not STOP.is_set():
                begun = time.monotonic()
                POPUPS[:] = [proc for proc in POPUPS if proc.poll() is None]
                with (DATA / 'control.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    disk_stamp = (DATA / 'state.json').stat().st_mtime_ns if (DATA / 'state.json').exists() else None
                    if disk_stamp != last_saved:
                        # The resume command shares this lock and is the only
                        # external writer. Never overwrite its latch/ack update.
                        state = load_state()
                        last_saved = disk_stamp
                        history.clear()
                    stamp = (ROOT / 'config.json').stat().st_mtime_ns
                    if stamp != config_stamp:
                        if controller:
                            controller.close()
                        controller = Controller(json.loads((ROOT / 'config.json').read_text()))
                        config_stamp = stamp
                    observations = []
                    try:
                        observations = tick(state, controller, dt.datetime.now(ZONE), persist=False)
                        state['last_error'] = None
                        last_error = None
                        meter = state.get('meter')
                        if meter:
                            if last_day != state['day']:
                                history.clear()
                                last_day = state['day']
                            history.append((begun, meter['proxy']['upload'], meter['proxy']['download'], observations))
                            while len(history) > 2 and history[1][0] < begun - 1:
                                history.popleft()
                    except Exception as error:
                        state['last_error'] = type(error).__name__
                        history.clear()
                        if state['last_error'] != last_error:
                            logging.error('Traffic guard operation failed: %s', state['last_error'])
                            last_error = state['last_error']
                    recorder.submit(dt.datetime.now(ZONE), observations,
                                    public_status(state, []), error=state.get('last_error'))
                    if begun - last_flush >= CHECKPOINT_INTERVAL or state['blocked']:
                        atomic_json(DATA / 'state.json', state)
                        last_flush = begun
                    last_saved = (DATA / 'state.json').stat().st_mtime_ns if (DATA / 'state.json').exists() else None
                    if begun - last_status >= STATUS_INTERVAL:
                        status = public_status(state, history)
                        status['traffic_history'] = recorder.status()
                        atomic_json(DATA / 'meter-status.json', status)
                        last_status = begun
                interval = 2 if state['blocked'] else INTERVAL
                STOP.wait(max(.005, interval - (time.monotonic() - begun)))
        finally:
            with (DATA / 'control.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if (DATA / 'state.json').exists() and (DATA / 'state.json').stat().st_mtime_ns != last_saved:
                    state = load_state()
                atomic_json(DATA / 'state.json', state)
                final = public_status(state, [])
                final.update(status='stopped', last_error='Sampler stopped')
                atomic_json(DATA / 'meter-status.json', final)
            if controller:
                controller.close()
            recorder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'status', 'resume'], nargs='?', default='status')
    args = parser.parse_args()
    run_locked(args.action)


if __name__ == '__main__':
    main()
