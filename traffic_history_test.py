"""Persistence, retention, attribution and real sampler-to-history checks."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import proxy_accounting as accounting
import traffic_history as history

NOW = dt.datetime(2026, 11, 11, 15, tzinfo=history.ZONE)


class Resolver:
    error = None
    def __init__(self, *_): pass
    def refresh(self): pass
    def identify(self, flow): return flow.get('process') or 'unknown', None, 'metadata'


def flow(host, down, up=0, process='codex'):
    return dict(host=host, download=down, upload=up, process=process, flow_id=host)


class HistoryChecks(unittest.TestCase):
    def test_software_groups_all_hosts_and_helpers_before_applying_top_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'history.sqlite3'
            store = history.HistoryStore(path)
            window = history.Window(NOW)
            window.add(NOW, [flow('one.test', 90, process='Google Chrome Helper'),
                             flow('two.test', 90, process='Google Chrome Helper (Aperitif)'),
                             flow('three.test', 100, process='codex')], {}, Resolver())
            store.append(window.record(), now=NOW.timestamp())
            store.close()
            result = history.query(path, NOW.timestamp()-1, NOW.timestamp()+1,
                                   top=1, now=NOW.timestamp()+1, group_by='software')
            self.assertEqual(result['top'][0]['process'], 'Google Chrome')
            self.assertEqual(result['top'][0]['download'], 180)
            self.assertEqual(result['top'][0]['target_count'], 2)

    def test_relay_and_unresolved_processes_remain_explicit_in_software_names(self):
        self.assertEqual(history.software_name('egress-guard'), '本地代理网关（转发）')
        self.assertEqual(history.software_name('unknown'), '未识别进程')
        self.assertEqual(history.software_name('Codex (Service)'), 'Codex / ChatGPT')
        self.assertEqual(history.software_name('language_server', 'aicode.googleapis.com'),
                         'Antigravity / agy')
        self.assertEqual(history.software_name('language_server', 'unrelated.test'),
                         'language_server')

    def test_top_k_keeps_other_bytes_and_never_persists_all_connection_ids(self):
        window = history.Window(NOW)
        rows = [flow(f'{i}.test', i+1) for i in range(25)]
        window.add(NOW, rows, {}, Resolver())
        record = window.record()
        self.assertEqual(len(record['top']), 20)
        self.assertEqual(record['top'][0]['host'], '24.test')
        self.assertEqual(record['other_download'], sum(range(1, 6)))
        self.assertEqual(record['other_targets'], 5)
        self.assertEqual(sum(r['download'] for r in record['top']) +
                         record['other_download'], sum(range(1, 26)))
        self.assertNotIn('flow_id', json.dumps(record))

    def test_upload_can_enter_top_k_independently_of_download(self):
        window = history.Window(NOW)
        window.add(NOW, [flow('down.test', 100), flow('up.test', 0, up=200)], {}, Resolver())
        self.assertEqual(window.record()['top'][0]['host'], 'up.test')

    def test_queries_separate_hosts_processes_and_time_ranges(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'history.sqlite3'
            store = history.HistoryStore(path)
            for offset, proc, down in [(0, 'codex', 100), (20, 'Chrome', 200)]:
                now = NOW+dt.timedelta(seconds=offset)
                window = history.Window(now)
                window.add(now, [flow('same.test', down, process=proc)], {}, Resolver())
                store.append(window.record(), now=now.timestamp())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            result = history.query(path, NOW.timestamp()-1, NOW.timestamp()+25,
                                   now=NOW.timestamp()+25)
            self.assertEqual(result['windows'], 2)
            self.assertEqual([r['process'] for r in result['top']], ['Chrome', 'codex'])
            narrow = history.query(path, NOW.timestamp()+10, NOW.timestamp()+25,
                                   now=NOW.timestamp()+25)
            self.assertEqual(narrow['observed_download'], 200)
            store.close()
            self.assertEqual(history.query(path, NOW.timestamp()-1,
                             NOW.timestamp()+25, now=NOW.timestamp()+25)['windows'], 2)

    def test_72_hour_retention_deletes_old_records_and_filters_unpruned_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'history.sqlite3'
            store = history.HistoryStore(path)
            for offset in [0, history.RETENTION+1]:
                now = NOW+dt.timedelta(seconds=offset)
                window = history.Window(now)
                window.add(now, [flow('test', 123)], {}, Resolver())
                store.append(window.record(), now=now.timestamp())
            self.assertEqual(store.db.execute('SELECT count(*) FROM windows').fetchone()[0], 1)
            result = history.query(path, NOW.timestamp()-100, NOW.timestamp()+history.RETENTION+2,
                                   now=NOW.timestamp()+history.RETENTION+2)
            self.assertEqual(result['windows'], 1)
            self.assertEqual(result['observed_download'], 123)
            store.close()

    def test_only_new_proxy_deltas_enter_history_not_lifetime_or_direct_bytes(self):
        old = dict(id='old', download=9000, upload=0, chains=['node'],
                   start=(NOW-dt.timedelta(hours=1)).isoformat(),
                   metadata={'host': 'old.test', 'process': 'codex', 'sourcePort': '123'})
        first = dict(upload=0, download=10000, generation='a', connections=[old])
        meter = accounting.new_meter(first, NOW)
        old = dict(old, download=9100)
        direct = dict(id='direct', download=500, upload=0, chains=['DIRECT'],
                      start=(NOW+dt.timedelta(milliseconds=1)).isoformat(),
                      metadata={'host': 'direct.test'})
        second = dict(upload=0, download=10600, generation='a', connections=[old, direct])
        observations = accounting.update(meter, second, NOW+dt.timedelta(seconds=1))
        self.assertEqual(sum(r['download'] for r in observations), 100)
        window = history.Window(NOW)
        window.add(NOW, observations, {}, Resolver())
        self.assertEqual(window.record()['top'][0]['download'], 100)
        self.assertEqual(window.record()['top'][0]['process'], 'codex')
        self.assertNotIn('direct.test', json.dumps(window.record()))
        self.assertNotIn('old.test', json.dumps(meter))

    def test_unknown_process_is_explicit_and_errors_are_retained(self):
        resolver = history.ProcessResolver('/tmp')
        window = history.Window(NOW)
        window.add(NOW, [flow('x.test', 123, process='')], {}, resolver,
                   error='TimeoutError', dropped=3)
        record = window.record()
        self.assertEqual(record['top'][0]['process'], 'unknown')
        self.assertEqual(record['sampler_errors'], ['TimeoutError'])
        self.assertEqual(record['dropped_samples_total'], 3)

    def test_background_worker_flushes_partial_window_on_graceful_shutdown(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(history, 'ProcessResolver', Resolver):
            path = Path(temp)/'history.sqlite3'
            recorder = history.HistoryRecorder(path)
            now = dt.datetime.now(history.ZONE)
            recorder.submit(now, [flow('persist.test', 123)], {'proxy_bytes': 123})
            recorder.close()
            self.assertFalse(recorder.thread.is_alive())
            self.assertIsNone(recorder.last_error)
            result = history.query(path, now.timestamp()-1, now.timestamp()+1)
            self.assertEqual(result['observed_download'], 123)

    def test_background_write_failure_is_visible_and_does_not_raise_in_sampler(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(history, 'ProcessResolver', Resolver):
            recorder = history.HistoryRecorder(Path(temp)/'history.sqlite3')
            now = dt.datetime.now(history.ZONE)
            with patch.object(history.HistoryStore, 'append', side_effect=OSError('full')):
                recorder.submit(now, [flow('test', 1)], {})
                recorder.close()
            self.assertEqual(recorder.last_error, 'OSError')
            self.assertFalse(recorder.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
