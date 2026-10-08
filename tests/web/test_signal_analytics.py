from cl_app.services.signal_analytics import SignalAnalytics


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
