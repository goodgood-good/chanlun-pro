from cl_app.services.signal_analytics import SignalAnalytics
import sqlite3
import pytest
from chanlun.screening.worker_transport import TIMING_SCOPE


def event(identity, stage, stamp, cycle=0, **values):
    return {
        "id": identity,
        "market": "a",
        "code": "SH.600000",
        "frequency": "5m",
        "point_type": "consolidation_up",
        "signal_type": "consolidation_divergence",
        "stage": stage,
        "change": "changed",
        "recorded_at": stamp,
        "source_closed_at": stamp - 10,
        "lifecycle_cycle": cycle,
        **values,
    }


def test_quality_is_durable_deduplicated_and_does_not_count_missing_historical_warnings(
    tmp_path,
):
    clock = [1000.0]
    store = SignalAnalytics(tmp_path, now=lambda: clock[0])
    rows = [
        event("prior", "warning", 900),
        event("prior", "invalidated", 1001),
        event("new", "warning", 1002, external_notifications=False),
        event("new", "warning", 1003),
        event("new", "confirmed", 1004),
    ]
    store.record(rows)
    store.record(rows)
    restarted = SignalAnalytics(tmp_path, now=lambda: 1010)
    restarted.record(rows)
    result = restarted.summary()
    row = result["groups"][0]
    assert (
        result["events"] == 4
        and row["warnings"] == 1
        and row["warning_confirmed"] == 1
        and row["warning_invalidated"] == 0
    )


def test_performance_aggregation_deduplicates_replayed_batches(tmp_path):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    period = {
        "market": "a",
        "code": "SH.600000",
        "frequency": "5m",
        "replays_reused": 2,
        "timings_ms": {
            "total": 100,
            "fetch": 20,
            "structure": 30,
            "validation": 40,
            "persistence": 10,
        },
    }
    store.record([], [period], run_id="one", revision="r")
    store.record([], [period], run_id="one", revision="r")
    store.record(
        [],
        [{**period, "timings_ms": {**period["timings_ms"], "total": 200}}],
        run_id="two",
        revision="r",
    )
    row = store.summary()["timings"][0]
    assert (
        row["samples"] == 2
        and row["total_ms"] == 150
        and row["max_ms"] == 200
        and row["proofs_reused"] == 4
    )


def test_preview_warnings_count_cycles_without_inventing_formal_confirmation_links(tmp_path):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    preview = dict(point_type='1buy', signal_type='native_point_preview')
    rows = [event('preview', 'approaching', 1001, **preview),
            event('preview', 'approaching', 1002, **preview),
            event('preview', 'invalidated', 1003, **preview),
            event('preview', 'approaching', 1004, cycle=1, **preview),
            event('old', 'approaching', 900, **preview),
            event('old', 'invalidated', 1005, **preview),
            event('formal', 'confirmed', 1006, point_type='1buy', signal_type='buy_sell_point')]
    store.record(rows)
    restarted = SignalAnalytics(tmp_path, now=lambda: 1010)
    restarted.record(rows)
    groups = {row['signal_type']: row for row in restarted.summary()['groups']}
    assert groups['native_point_preview']['samples'] == 3
    assert groups['native_point_preview']['warnings'] == 2
    assert groups['native_point_preview']['warning_invalidated'] == 1
    assert groups['native_point_preview']['warning_confirmed'] is None
    assert groups['buy_sell_point']['confirmations'] == 1
    assert groups['buy_sell_point']['warnings'] == 0


def timed_period(total=12000):
    return {'market': 'a', 'code': 'SH.600088', 'frequency': '5m', 'replays_reused': 1,
            'processing_timeline': {'measurement_scope': TIMING_SCOPE},
            'timings_ms': {'total': total, 'fetch': 1000., 'structure': 1000.,
                           'validation': 500., 'persistence': 500., 'coordination': 9000.}}


def test_complete_measurements_do_not_mix_with_legacy_primary_averages(tmp_path):
    store = SignalAnalytics(tmp_path, now=lambda: 1000.)
    legacy = timed_period(3000.)
    legacy.pop('processing_timeline')
    store.record([], [legacy], run_id='old', revision='r')
    with sqlite3.connect(store.path) as db:
        db.execute('DROP TABLE task_timings')  # Simulate an existing database.
    old = SignalAnalytics(tmp_path, now=lambda: 1000.).summary()['timings']
    assert old[0]['measurement_scope'] == 'primary-period-v1' and old[0]['total_ms'] == 3000.
    store.record([], [timed_period()], run_id='new', revision='r')
    restored = SignalAnalytics(tmp_path, now=lambda: 1000.)
    restored.record([], [timed_period()], run_id='new', revision='r')
    rows = {row['measurement_scope']: row for row in restored.summary()['timings']}
    assert rows[TIMING_SCOPE]['samples'] == rows['primary-period-v1']['samples'] == 1
    assert rows[TIMING_SCOPE]['total_ms'] == 12000. and rows[TIMING_SCOPE]['coordination_ms'] == 9000.
    assert rows['primary-period-v1']['total_ms'] == 3000.


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, None, True])
def test_invalid_timing_does_not_consume_the_deduplication_key(tmp_path, bad):
    store = SignalAnalytics(tmp_path, now=lambda: 1000.)
    store.record([], [timed_period(bad)], run_id='one', revision='r')
    assert store.summary()['timings'] == []
    store.record([], [timed_period()], run_id='one', revision='r')
    assert store.summary()['timings'][0]['samples'] == 1
