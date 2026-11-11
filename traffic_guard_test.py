"""Threshold, acknowledgment, persistence and real local-HTTP contract tests."""
import contextlib
import datetime as dt
import http.server
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch
import traffic_guard as tg

NOW = dt.datetime(2026, 11, 10, 12, tzinfo=tg.ZONE)
GB = 1_000_000_000


def counters(total, generation='a'):
    return dict(upload=0, download=total, generation=generation)


def started():
    state = tg.initial_state(NOW)
    tg.advance(state, counters(1000 * GB), NOW)
    return state


class ThresholdChecks(unittest.TestCase):
    def test_existing_core_lifetime_usage_is_only_a_baseline(self):
        state = started()
        self.assertEqual(state['daily_bytes'], 0)
        self.assertFalse(state['blocked'])
        self.assertIsNone(state['pending'])

    def test_10gb_warns_once_until_next_tier(self):
        state = started()
        self.assertEqual(tg.advance(state, counters(1009 * GB), NOW), [])
        events = tg.advance(state, counters(1010 * GB), NOW)
        self.assertEqual([x[0] for x in events], ['warning'])
        self.assertEqual(tg.advance(state, counters(1019 * GB), NOW), [])
        self.assertFalse(state['blocked'])

    def test_second_tier_without_ack_trips_latch(self):
        state = started()
        tg.advance(state, counters(1010 * GB), NOW)
        events = tg.advance(state, counters(1020 * GB), NOW)
        self.assertEqual(events[0][0], 'block')
        self.assertTrue(state['blocked'])
        self.assertEqual(tg.advance(state, counters(1030 * GB), NOW), [])

    def test_closing_first_warning_allows_second_warning(self):
        state = started()
        tg.advance(state, counters(1010 * GB), NOW)
        with patch.object(tg, 'acknowledged', return_value=True):
            tg.read_acknowledgments(state)
        events = tg.advance(state, counters(1020 * GB), NOW)
        self.assertEqual(events[0][0], 'warning')
        self.assertFalse(state['blocked'])

    def test_ack_after_cutoff_does_not_resume(self):
        state = started()
        tg.advance(state, counters(1020 * GB), NOW)
        with patch.object(tg, 'acknowledged', return_value=True):
            tg.read_acknowledgments(state)
        self.assertTrue(state['blocked'])

    def test_new_day_resets_usage_but_not_shutdown_latch(self):
        state = started()
        tg.advance(state, counters(1020 * GB), NOW)
        tomorrow = NOW + dt.timedelta(days=1)
        tg.advance(state, counters(1020 * GB + 123), tomorrow)
        self.assertEqual(state['daily_bytes'], 123)
        self.assertEqual(state['last_bucket'], 0)
        self.assertTrue(state['blocked'])

    def test_new_day_has_its_own_10gb_threshold(self):
        state = started()
        tg.advance(state, counters(1009 * GB), NOW)
        tomorrow = NOW + dt.timedelta(days=1)
        tg.advance(state, counters(1009 * GB), tomorrow)
        events = tg.advance(state, counters(1019 * GB), tomorrow)
        self.assertEqual(state['daily_bytes'], 10 * GB)
        self.assertEqual(events[0][0], 'warning')

    def test_core_counter_reset_is_added_without_negative_usage(self):
        state = started()
        tg.advance(state, counters(1001 * GB), NOW)
        tg.advance(state, counters(2 * GB), NOW)
        self.assertEqual(state['daily_bytes'], 3 * GB)

    def test_new_core_generation_counts_even_if_new_total_is_larger(self):
        state = started()
        tg.advance(state, counters(1001 * GB), NOW)
        tg.advance(state, counters(1002 * GB, generation='b'), NOW)
        self.assertEqual(state['daily_bytes'], 1003 * GB)
        self.assertTrue(state['blocked'])

    def test_persistence_does_not_forgive_an_unclosed_window(self):
        state = started()
        tg.advance(state, counters(1010 * GB), NOW)
        restored = json.loads(json.dumps(state))
        tg.advance(restored, counters(1020 * GB), NOW)
        self.assertTrue(restored['blocked'])

    def test_upload_and_download_are_both_counted(self):
        state = tg.initial_state(NOW)
        tg.advance(state, dict(upload=0, download=0, generation='a'), NOW)
        tg.advance(state, dict(upload=4*GB, download=6*GB, generation='a'), NOW)
        self.assertEqual(state['daily_bytes'], 10 * GB)
        self.assertIsNotNone(state['pending'])

    def test_only_matching_ack_file_counts_as_dismissal(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(tg, 'DATA', Path(temp)):
            alert = {'id': 'right'}
            tg.atomic_json(tg.ack_path(alert), {'id': 'wrong', 'acknowledged': True})
            self.assertFalse(tg.acknowledged(alert))
            tg.atomic_json(tg.ack_path(alert), {'id': 'right', 'acknowledged': True})
            self.assertTrue(tg.acknowledged(alert))
            self.assertEqual(tg.ack_path(alert).stat().st_mode & 0o777, 0o600)

    def test_dead_popup_is_not_an_acknowledgment(self):
        state = started()
        tg.advance(state, counters(1010 * GB), NOW)
        state['pending']['popup_pid'] = 99999999
        with patch.object(tg, 'acknowledged', return_value=False):
            tg.read_acknowledgments(state)
        self.assertIsNotNone(state['pending'])


class TickChecks(unittest.TestCase):
    def test_close_during_controller_lookup_prevents_false_cutoff(self):
        state = started()
        tg.advance(state, counters(1010 * GB), NOW)
        ack = {'ready': False}
        class Controller:
            def counters(self): return counters(1020 * GB)
            def snapshot(self):
                ack['ready'] = True
                return {'mode': 'global', 'selection': 'original'}
            def block(self): raise AssertionError('Window was closed before cutoff')
        with tempfile.TemporaryDirectory() as temp, patch.object(tg, 'DATA', Path(temp)), patch.object(tg, 'acknowledged', side_effect=lambda _: ack['ready']), patch.object(tg, 'show_popup'):
            tg.tick(state, Controller(), NOW)
        self.assertFalse(state['blocked'])
        self.assertEqual(state['pending']['bucket'], 2)

    def test_shutdown_latch_is_durable_before_controller_mutation(self):
        state = started()
        tg.advance(state, counters(1010 * GB), NOW)
        class Controller:
            def counters(self): return counters(1020 * GB)
            def snapshot(self): return {'mode': 'global', 'selection': 'original'}
            def block(self):
                persisted = json.loads((tg.DATA / 'state.json').read_text())
                assert persisted['blocked']
                assert persisted['restore']['selection'] == 'original'
                raise RuntimeError('Simulated controller disconnect during cutoff')
        with tempfile.TemporaryDirectory() as temp, patch.object(tg, 'DATA', Path(temp)), patch.object(tg, 'acknowledged', return_value=False), patch.object(tg, 'show_popup'):
            with self.assertRaises(RuntimeError):
                tg.tick(state, Controller(), NOW)
            self.assertTrue(tg.load_state()['blocked'])

    def test_manual_resume_keeps_usage_and_records_acknowledgment(self):
        state = started()
        tg.advance(state, counters(1020 * GB), NOW)
        state['restore'] = {'mode': 'global', 'selection': 'original'}
        with tempfile.TemporaryDirectory() as temp, patch.object(tg, 'DATA', Path(temp)), patch.object(tg, 'Controller') as controller, patch.object(tg, 'ROOT', Path(temp)):
            (Path(temp) / 'config.json').write_text('{}')
            tg.atomic_json(Path(temp) / 'state.json', state)
            with contextlib.redirect_stdout(__import__('io').StringIO()):
                tg.run_locked('resume')
            resumed = tg.load_state()
            self.assertFalse(resumed['blocked'])
            self.assertEqual(resumed['daily_bytes'], 20 * GB)
            controller.return_value.restore.assert_called_once_with(state['restore'])
            self.assertTrue(tg.acknowledged(state['pending']))


class ClassifiedGuardChecks(unittest.TestCase):
    def sample(self, down, rows=(), generation='a'):
        return dict(upload=0, download=down, generation=generation, connections=list(rows))

    def flow(self, name, amount, leaf):
        return dict(id=name, upload=0, download=amount, chains=[leaf],
                    start=(NOW+dt.timedelta(milliseconds=1)).isoformat(), metadata={})

    def test_upgrade_preserves_old_usage_and_latch(self):
        state = started()
        tg.advance(state, counters(1003*GB), NOW)
        tg.advance(state, self.sample(1003*GB), NOW)
        self.assertEqual(state['version'], 2)
        self.assertEqual(state['meter']['legacy_bytes'], 3*GB)
        self.assertEqual(state['daily_bytes'], 3*GB)
        state['blocked'] = True
        tg.advance(state, self.sample(1004*GB), NOW+dt.timedelta(seconds=1))
        self.assertTrue(state['blocked'])

    def test_20gb_of_proven_direct_does_not_warn(self):
        state = tg.initial_state(NOW)
        tg.advance(state, self.sample(0), NOW)
        events = tg.advance(state, self.sample(20*GB, [self.flow('d', 20*GB, 'DIRECT')]), NOW+dt.timedelta(seconds=1))
        self.assertEqual(events, [])
        self.assertEqual(state['daily_bytes'], 0)

    def test_proxy_and_unclassified_share_the_conservative_warning_budget(self):
        state = tg.initial_state(NOW)
        tg.advance(state, self.sample(0), NOW)
        events = tg.advance(state, self.sample(10*GB, [self.flow('p', 9*GB, 'node')]), NOW+dt.timedelta(seconds=1))
        self.assertEqual(events[0][0], 'warning')
        self.assertEqual(events[0][1]['accounting']['proxy_bytes'], 9*GB)
        self.assertEqual(events[0][1]['accounting']['unknown_bytes'], GB)

    def test_reclassification_can_lower_budget_without_repeating_a_tier(self):
        state = tg.initial_state(NOW)
        tg.advance(state, self.sample(0), NOW)
        tg.advance(state, self.sample(10*GB), NOW+dt.timedelta(seconds=1))
        events = tg.advance(state, self.sample(10*GB, [self.flow('d', 10*GB, 'DIRECT')]), NOW+dt.timedelta(seconds=2))
        self.assertEqual(state['daily_bytes'], 0)
        self.assertEqual(events, [])
        events = tg.advance(state, self.sample(20*GB, [self.flow('p', 10*GB, 'node'), self.flow('d', 10*GB, 'DIRECT')]), NOW+dt.timedelta(seconds=3))
        self.assertEqual(state['daily_bytes'], 10*GB)
        self.assertEqual(events, [])

    def test_new_day_drops_historical_usage_and_keeps_block_latch(self):
        state = started()
        tg.advance(state, counters(1002*GB), NOW)
        tg.advance(state, self.sample(1002*GB), NOW)
        state['blocked'] = True
        tg.advance(state, self.sample(1002*GB), NOW+dt.timedelta(days=1))
        self.assertEqual(state['daily_bytes'], 0)
        self.assertEqual(state['meter']['legacy_bytes'], 0)
        self.assertTrue(state['blocked'])

    def test_checkpoint_contains_no_connection_hostnames(self):
        state = tg.initial_state(NOW)
        tg.advance(state, self.sample(0), NOW)
        row = self.flow('p', 123, 'node')
        row['metadata'] = {'host': 'private-target.invalid', 'processPath': '/private/application'}
        tg.advance(state, self.sample(123, [row]), NOW+dt.timedelta(seconds=1))
        persisted = json.dumps(state)
        self.assertNotIn('private-target.invalid', persisted)
        self.assertNotIn('/private/application', persisted)

    def test_public_status_reports_unavailable_and_confirmed_only_speed(self):
        state = tg.initial_state(NOW)
        tg.advance(state, self.sample(0), NOW)
        tg.advance(state, self.sample(100, [self.flow('p', 80, 'node')]), NOW+dt.timedelta(seconds=1))
        state['last_error'] = 'ConnectionError'
        status = tg.public_status(state, [(1, 0, 0, []), (2, 0, 80, [])])
        self.assertEqual(status['proxy_bytes'], 80)
        self.assertEqual(status['download_bps'], 80)
        self.assertEqual(status['status'], 'unavailable')



class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ControllerContractChecks(unittest.TestCase):
    def test_loopback_http_controller_reads_real_counters(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                payload = json.dumps({'uploadTotal': 12, 'downloadTotal': 123,
                                      'connections': []}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        controller = tg.Controller({'controller_url': f'http://127.0.0.1:{server.server_port}'})
        try:
            self.assertEqual(controller.counters()['download'], 123)
            self.assertEqual(controller.counters()['upload'], 12)
        finally:
            controller.close()
            server.shutdown(); server.server_close(); thread.join()

    def test_controller_rejects_non_loopback_and_credential_urls(self):
        for url in ['http://example.test:9090', 'http://user:password@127.0.0.1:9090',
                    'http://127.0.0.1:9090/path']:
            with self.assertRaises(ValueError):
                tg.Controller({'controller_url': url})

    def test_cutoff_rejects_globally_closes_connections_and_restores_on_request(self):
        live = dict(mode='global', selection='original', all=['original', 'REJECT'])
        calls = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def handle_request(self):
                self.assert_secret = self.headers.get('Authorization') == 'Bearer test-secret'
                if not self.assert_secret:
                    self.send_error(401); return
                size = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(size)) if size else None
                calls.append((self.command, self.path, body))
                result = {}
                if self.path == '/configs':
                    if body: live['mode'] = body['mode']
                    result = {'mode': live['mode']}
                elif self.path == '/proxies/GLOBAL':
                    if body: live['selection'] = body['name']
                    result = {'now': live['selection'], 'all': live['all']}
                elif self.path == '/connections':
                    result = {'downloadTotal': 123, 'uploadTotal': 12}
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            do_GET = do_PUT = do_PATCH = do_DELETE = handle_request
        with tempfile.TemporaryDirectory(prefix='tg-', dir='/tmp') as temp:
            path = str(Path(temp) / 'controller.sock')
            with UnixServer(path, Handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                controller = tg.Controller(dict(controller_socket=path, controller_secret='test-secret'))
                original = controller.snapshot()
                controller.block()
                self.assertEqual(live['mode'], 'global')
                self.assertEqual(live['selection'], 'REJECT')
                mutations = [(m, p) for m, p, _ in calls if m != 'GET']
                self.assertEqual(mutations, [('PUT', '/proxies/GLOBAL'), ('PATCH', '/configs'), ('DELETE', '/connections')])
                controller.restore(original)
                self.assertEqual(live['selection'], 'original')
                self.assertEqual(controller.counters()['download'], 123)
                server.shutdown(); thread.join()


if __name__ == '__main__':
    unittest.main()
