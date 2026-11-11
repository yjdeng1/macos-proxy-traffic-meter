import copy
import datetime as dt
import json
import unittest
import proxy_accounting as pa

NOW = dt.datetime(2026, 11, 10, 14, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def flow(identifier, amount, chain='node', start=None, up=0):
    return dict(id=identifier, upload=up, download=amount, chains=[chain],
                start=(start or NOW + dt.timedelta(milliseconds=10)).isoformat(),
                metadata={'host': 'example.test'})


def snap(down, *flows, up=0, generation='a'):
    return dict(upload=up, download=down, generation=generation, connections=list(flows))


class AccountingChecks(unittest.TestCase):
    def test_old_core_and_active_flow_are_baselines(self):
        meter = pa.new_meter(snap(1000, flow('old', 900, start=NOW-dt.timedelta(hours=1))), NOW)
        pa.update(meter, snap(1100, flow('old', 1000, start=NOW-dt.timedelta(hours=1))), NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 100)

    def test_direct_is_excluded_but_proxy_is_counted(self):
        meter = pa.new_meter(snap(0), NOW)
        pa.update(meter, snap(1000, flow('p', 400), flow('d', 600, 'DIRECT')), NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 400)
        self.assertEqual(pa.totals(meter)['direct_bytes'], 600)
        self.assertEqual(pa.totals(meter)['budget_bytes'], 400)

    def test_closed_short_connection_remains_unclassified(self):
        meter = pa.new_meter(snap(0), NOW)
        pa.update(meter, snap(999), NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 0)
        self.assertEqual(pa.totals(meter)['unknown_bytes'], 999)
        self.assertEqual(pa.totals(meter)['budget_bytes'], 999)

    def test_final_closed_tail_is_not_silently_lost(self):
        meter = pa.new_meter(snap(0), NOW)
        pa.update(meter, snap(100, flow('p', 100)), NOW+dt.timedelta(seconds=1))
        pa.update(meter, snap(160), NOW+dt.timedelta(seconds=2))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 100)
        self.assertEqual(pa.totals(meter)['unknown_bytes'], 60)
        self.assertEqual(pa.totals(meter)['budget_bytes'], 160)

    def test_duplicate_snapshot_and_reappearing_uuid_are_not_double_counted(self):
        meter = pa.new_meter(snap(0), NOW)
        pa.update(meter, snap(100, flow('p', 100), flow('p', 100)), NOW+dt.timedelta(seconds=1))
        pa.update(meter, snap(100), NOW+dt.timedelta(seconds=2))
        pa.update(meter, snap(100, flow('p', 100)), NOW+dt.timedelta(seconds=3))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 100)

    def test_non_atomic_snapshot_is_reconciled_when_total_catches_up(self):
        meter = pa.new_meter(snap(0), NOW)
        pa.update(meter, snap(80, flow('p', 100)), NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 80)
        pa.update(meter, snap(100, flow('p', 100)), NOW+dt.timedelta(seconds=2))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 100)
        self.assertEqual(pa.totals(meter)['unknown_bytes'], 0)

    def test_restart_counts_new_core_without_negative_deltas(self):
        meter = pa.new_meter(snap(1000), NOW)
        pa.update(meter, snap(1100, flow('p', 100)), NOW+dt.timedelta(seconds=1))
        pa.update(meter, snap(20, flow('q', 20), generation='b'), NOW+dt.timedelta(seconds=2))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 120)
        self.assertEqual(meter['reconnects'], 1)

    def test_reset_of_global_counters_does_not_recount_live_flow(self):
        meter = pa.new_meter(snap(1000, flow('p', 800, start=NOW-dt.timedelta(hours=1))), NOW)
        pa.update(meter, snap(20, flow('p', 820, start=NOW-dt.timedelta(hours=1))), NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 20)

    def test_midnight_clears_legacy_but_keeps_live_watermarks(self):
        near_midnight = NOW.replace(hour=23, minute=59, second=59)
        meter = pa.new_meter(snap(100, flow('p', 100)), near_midnight, legacy_bytes=500)
        pa.update(meter, snap(130, flow('p', 130)), near_midnight+dt.timedelta(seconds=2))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 30)
        self.assertEqual(pa.totals(meter)['historical_bytes'], 0)
        self.assertEqual(pa.totals(meter)['budget_bytes'], 30)

    def test_checkpoint_reload_preserves_classification_and_budget(self):
        meter = pa.new_meter(snap(0), NOW, legacy_bytes=1234)
        pa.update(meter, snap(100, flow('p', 40), flow('d', 60, 'DIRECT')), NOW+dt.timedelta(seconds=1))
        meter = json.loads(json.dumps(meter))
        pa.update(meter, snap(200, flow('p', 80), flow('d', 120, 'DIRECT')), NOW+dt.timedelta(seconds=2))
        self.assertEqual(pa.totals(meter)['budget_bytes'], 1234+80)

    def test_missing_route_and_missing_start_time_are_not_assumed_proxy(self):
        meter = pa.new_meter(snap(0), NOW)
        a = flow('p', 80); a['chains'] = []
        b = flow('q', 20); b.pop('start')
        pa.update(meter, snap(100, a, b), NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 0)
        self.assertEqual(pa.totals(meter)['unknown_bytes'], 100)

    def test_both_directions_and_reject_are_accounted(self):
        meter = pa.new_meter(snap(0), NOW)
        pa.update(meter, snap(100, flow('p', 40, up=10), flow('r', 60, 'REJECT', up=20), up=30), NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 50)
        self.assertEqual(pa.totals(meter)['excluded_bytes'], 80)
        self.assertEqual(pa.totals(meter)['budget_bytes'], 50)

    def test_long_gap_is_visible_in_status(self):
        meter = pa.new_meter(snap(0), NOW)
        pa.update(meter, snap(100), NOW+dt.timedelta(seconds=60))
        self.assertEqual(meter['coverage_gaps'], 1)
        self.assertEqual(pa.totals(meter)['unknown_bytes'], 100)

    def test_controller_rebind_is_not_a_core_restart_when_pid_is_stable(self):
        first = snap(1000); first['core_pid'] = 123
        meter = pa.new_meter(first, NOW)
        second = snap(1100, flow('p', 100), generation='new-socket')
        second['core_pid'] = 123
        pa.update(meter, second, NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['budget_bytes'], 100)
        self.assertEqual(meter['reconnects'], 0)

    def test_offline_midnight_does_not_claim_old_day_bytes_as_confirmed_proxy(self):
        first = NOW.replace(hour=23, minute=59)
        meter = pa.new_meter(snap(0), first)
        old_flow = flow('p', 100, start=first+dt.timedelta(seconds=10))
        pa.update(meter, snap(100, old_flow), first+dt.timedelta(minutes=2))
        self.assertEqual(pa.totals(meter)['proxy_bytes'], 0)
        self.assertEqual(pa.totals(meter)['unknown_bytes'], 100)

    def test_null_connections_from_idle_core_are_valid(self):
        empty = snap(0)
        empty['connections'] = None
        meter = pa.new_meter(empty, NOW)
        pa.update(meter, empty, NOW+dt.timedelta(seconds=1))
        self.assertEqual(pa.totals(meter)['budget_bytes'], 0)

    def test_reserved_names_are_case_sensitive(self):
        self.assertEqual(pa.classify(['DIRECT', 'selector']), 'direct')
        self.assertEqual(pa.classify(['direct', 'selector']), 'proxy')

    def test_negative_counters_are_rejected(self):
        with self.assertRaises(ValueError):
            pa.new_meter(snap(-1), NOW)

if __name__ == '__main__':
    unittest.main()
