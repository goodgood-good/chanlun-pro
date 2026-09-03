import time
from datetime import datetime

import pytest

from web.chanlun_chart.cl_app.services import candidate_chart_cache_warm as warm


_FIVE_MINUTE_ONLY = (("5m", warm._MAX_TARGETS),)


@pytest.fixture(autouse=True)
def _stable_market_state(monkeypatch):
    monkeypatch.setattr(warm, "market_now_trading", lambda _market: True)


def _fresh_entry():
    now = time.time()
    return {
        "is_full_snapshot": True,
        "validated_at": now,
        "max_time": now,
    }


def test_candidate_targets_follow_review_order_and_deduplicate_symbols():
    snapshot = {
        "signals": [
            {
                "code": "AAPL.US",
                "market": "us",
                "execution_profile": {"context_grade": "B"},
                "review_priority": 99,
                "lifecycle_stage": "observed",
            },
            {
                "code": "SZ.000001",
                "market": "a",
                "execution_profile": {"context_grade": "A"},
                "review_priority": 40,
                "lifecycle_stage": "triggered",
                "point_type": "2buy",
            },
            {
                "code": "SH.600000",
                "market": "a",
                "execution_profile": {"context_grade": "A"},
                "review_priority": 40,
                "lifecycle_stage": "executable",
                "point_type": "1buy",
            },
            {
                "code": "sh.600000",
                "market": "a",
                "execution_profile": {"context_grade": "C"},
            },
        ]
    }

    assert warm.candidate_chart_targets(snapshot) == (
        ("a", "SH.600000"),
        ("a", "SZ.000001"),
        ("us", "AAPL.US"),
    )


def test_candidate_targets_prioritize_the_account_visible_point_filter():
    snapshot = {
        "signals": [
            {
                "market": "a",
                "code": "SH.600001",
                "lifecycle_stage": "triggered",
                "point_type": "1buy",
                "execution_profile": {"context_grade": "A"},
                "review_priority": 90,
            },
            {
                "market": "a",
                "code": "SZ.300001",
                "lifecycle_stage": "triggered",
                "point_type": "3buy",
                "execution_profile": {"context_grade": "B"},
                "review_priority": 40,
            },
            {
                "market": "a",
                "code": "SZ.300002",
                "lifecycle_stage": "observed",
                "point_type": "3buy",
                "execution_profile": {"context_grade": "C"},
                "review_priority": 20,
            },
        ]
    }

    assert warm.candidate_chart_targets(
        snapshot,
        preferred_view={"pointType": "3buy"},
    ) == (
        ("a", "SZ.300001"),
        ("a", "SZ.300002"),
        ("a", "SH.600001"),
    )


def test_candidate_targets_keep_review_order_inside_account_filter():
    shared = {
        "market": "a",
        "lifecycle_stage": "triggered",
        "point_type": "3buy",
        "execution_profile": {"context_grade": "A"},
    }
    snapshot = {
        "signals": [
            {**shared, "code": "SZ.300002", "review_priority": 20},
            {**shared, "code": "SZ.300001", "review_priority": 80},
            {
                **shared,
                "code": "SZ.300003",
                "review_priority": 100,
                "lifecycle_stage": "observed",
            },
        ]
    }

    assert warm.candidate_chart_targets(
        snapshot,
        preferred_view={"pointType": "3buy", "lifecycle": "triggered"},
    ) == (
        ("a", "SZ.300001"),
        ("a", "SZ.300002"),
        ("a", "SZ.300003"),
    )


def test_candidate_targets_prioritize_account_source_and_market_filters():
    snapshot = {
        "signals": [
            {
                "market": "a",
                "code": "SH.600001",
                "lifecycle_stage": "triggered",
                "selection_sources": ["QMT_SECTOR_TRIGGER"],
            },
            {
                "market": "a",
                "code": "SZ.300001",
                "lifecycle_stage": "triggered",
                "selection_sources": ["ACTIVE_WATCHLIST_MONITOR"],
            },
            {
                "market": "us",
                "code": "AAPL.US",
                "lifecycle_stage": "triggered",
                "selection_sources": ["ACTIVE_WATCHLIST_MONITOR"],
            },
        ]
    }

    assert warm.candidate_chart_targets(
        snapshot,
        preferred_view={"signalSource": "watchlist", "market": "a"},
    )[0] == ("a", "SZ.300001")


def test_warming_restores_persisted_entries_without_building_non_a_markets(monkeypatch):
    calls = []
    monkeypatch.setattr(
        warm,
        "query_cl_chart_config",
        lambda market, code: {"market": market, "code": code},
    )
    monkeypatch.setattr(
        warm,
        "_build_cache_key",
        lambda market, code, frequency, config: f"{market}|{code}|{frequency}",
    )
    monkeypatch.setattr(
        warm,
        "_get_chart_cache_entry",
        lambda key: calls.append(key) or (_fresh_entry() if "600000" in key else None),
    )
    monkeypatch.setattr(warm, "chart_cache_metrics", lambda: {"entries": 1})
    monkeypatch.setattr(warm.LogUtil, "info", lambda _message: None)

    warm._warm_targets(
        (("a", "SH.600000"), ("us", "AAPL.US")),
        _FIVE_MINUTE_ONLY,
    )

    assert sorted(calls) == ["a|SH.600000|5m", "us|AAPL.US|5m"]


def test_warming_builds_missing_a_share_from_local_store_only(monkeypatch):
    calls = []
    warmed = {"ready": False}
    monkeypatch.setattr(
        warm,
        "query_cl_chart_config",
        lambda market, code: {"market": market, "code": code},
    )
    monkeypatch.setattr(
        warm,
        "_build_cache_key",
        lambda market, code, frequency, config: f"{market}|{code}|{frequency}",
    )
    monkeypatch.setattr(warm, "_get_chart_cache_entry", lambda _key: None)
    monkeypatch.setattr(
        warm,
        "_get_chart_cache_entry_ram_only",
        lambda _key: _fresh_entry() if warmed["ready"] else None,
    )

    def compute(market, code, frequency, config, *, skip_download):
        calls.append((market, code, frequency, config, skip_download))
        warmed["ready"] = True
        return True

    monkeypatch.setattr(warm, "compute_and_cache_chart_data", compute)
    monkeypatch.setattr(warm, "_get_last_user_request_time", lambda: 0.0)
    monkeypatch.setattr(warm, "chart_cache_metrics", lambda: {"entries": 1})
    monkeypatch.setattr(warm.LogUtil, "info", lambda _message: None)

    warm._warm_targets((("a", "SH.600001"),), _FIVE_MINUTE_ONLY)

    assert calls == [
        (
            "a",
            "SH.600001",
            "5m",
            {"market": "a", "code": "SH.600001"},
            True,
        )
    ]


def test_default_plan_warms_all_periods_for_complete_candidate_set(monkeypatch):
    built_frequencies = []
    targets = tuple(("a", f"SH.60000{index}") for index in range(6))
    monkeypatch.setattr(warm, "query_cl_chart_config", lambda _market, _code: {})

    def cache_key(_market, _code, frequency, _config):
        built_frequencies.append(frequency)
        return f"{_code}|{frequency}"

    monkeypatch.setattr(warm, "_build_cache_key", cache_key)
    monkeypatch.setattr(
        warm,
        "_get_chart_cache_entry",
        lambda _key: _fresh_entry(),
    )
    monkeypatch.setattr(warm, "chart_cache_metrics", lambda: {})
    monkeypatch.setattr(warm.LogUtil, "info", lambda _message: None)

    warm._warm_targets(targets)

    assert built_frequencies.count("5m") == 6
    assert built_frequencies.count("1m") == 6
    assert built_frequencies.count("30m") == 6
    assert built_frequencies.count("d") == 6
    assert built_frequencies[:4] == ["1m", "5m", "30m", "d"]


def test_failed_local_build_remains_eligible_for_a_later_retry(monkeypatch):
    monkeypatch.setattr(warm, "query_cl_chart_config", lambda _market, _code: {})
    monkeypatch.setattr(warm, "_build_cache_key", lambda *_args: "candidate-key")
    monkeypatch.setattr(warm, "_get_chart_cache_entry", lambda _key: None)
    monkeypatch.setattr(warm, "_get_chart_cache_entry_ram_only", lambda _key: None)
    monkeypatch.setattr(warm, "compute_and_cache_chart_data", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(warm, "_get_last_user_request_time", lambda: 0.0)
    monkeypatch.setattr(warm, "chart_cache_metrics", lambda: {})
    monkeypatch.setattr(warm.LogUtil, "info", lambda _message: None)
    monkeypatch.setattr(warm, "_last_completed", (("a", "OLD"),))

    warm._warm_targets((("a", "SH.600002"),), _FIVE_MINUTE_ONLY)

    assert warm._last_completed is None


def test_stale_candidate_snapshot_is_rebuilt_from_local_store(monkeypatch):
    calls = []
    now = time.time()
    refreshed = {"ready": False}
    stale = {
        "is_full_snapshot": True,
        "validated_at": now - warm._TRADING_REFRESH_AFTER_SECONDS["5m"] - 1,
        "max_time": now,
    }
    monkeypatch.setattr(warm, "query_cl_chart_config", lambda _market, _code: {})
    monkeypatch.setattr(warm, "_build_cache_key", lambda *_args: "stale-key")
    monkeypatch.setattr(warm, "_get_chart_cache_entry", lambda _key: stale)
    monkeypatch.setattr(
        warm,
        "_get_chart_cache_entry_ram_only",
        lambda _key: _fresh_entry() if refreshed["ready"] else stale,
    )
    monkeypatch.setattr(warm, "_get_last_user_request_time", lambda: 0.0)
    monkeypatch.setattr(warm, "chart_cache_metrics", lambda: {})
    monkeypatch.setattr(warm.LogUtil, "info", lambda _message: None)

    def compute(market, code, frequency, config, *, skip_download):
        calls.append((market, code, frequency, config, skip_download))
        refreshed["ready"] = True
        return True

    monkeypatch.setattr(warm, "compute_and_cache_chart_data", compute)

    warm._warm_targets((("a", "SH.600010"),), _FIVE_MINUTE_ONLY)

    assert calls == [("a", "SH.600010", "5m", {}, True)]
    assert warm._metrics["refreshes"] >= 1


def test_entry_refresh_due_uses_period_and_market_cadence():
    now = 10_000.0
    five_minute = warm._TRADING_REFRESH_AFTER_SECONDS["5m"]
    entry = {
        "is_full_snapshot": True,
        "validated_at": now - five_minute + 1,
        "max_time": now,
    }

    assert warm._entry_refresh_due(
        entry,
        "5m",
        market_is_trading=True,
        now=now,
    ) is False
    entry["validated_at"] = now - five_minute
    assert warm._entry_refresh_due(
        entry,
        "5m",
        market_is_trading=True,
        now=now,
    ) is True
    assert warm._entry_refresh_due(
        entry,
        "5m",
        market_is_trading=False,
        now=now,
    ) is False


def test_expected_completed_bar_time_respects_publication_grace_and_sessions():
    observed = datetime.fromisoformat("2026-09-01T10:26:00+08:00").timestamp()

    assert warm._expected_latest_completed_bar_time("1m", now=observed) == int(
        datetime.fromisoformat("2026-09-01T10:25:00+08:00").timestamp()
    )
    assert warm._expected_latest_completed_bar_time("5m", now=observed) == int(
        datetime.fromisoformat("2026-09-01T10:25:00+08:00").timestamp()
    )
    assert warm._expected_latest_completed_bar_time("30m", now=observed) == int(
        datetime.fromisoformat("2026-09-01T10:00:00+08:00").timestamp()
    )


def test_lagging_completed_bar_uses_bounded_incremental_download(monkeypatch):
    now = datetime.fromisoformat("2026-09-01T10:26:00+08:00").timestamp()
    latest = int(datetime.fromisoformat("2026-09-01T10:25:00+08:00").timestamp())
    stale = {
        "is_full_snapshot": True,
        "validated_at": now,
        "max_time": latest - 300,
    }
    refreshed = {"ready": False}
    calls = []
    monkeypatch.setattr(warm.time, "time", lambda: now)
    monkeypatch.setattr(warm, "query_cl_chart_config", lambda _market, _code: {})
    monkeypatch.setattr(warm, "_build_cache_key", lambda *_args: "lagging-key")
    monkeypatch.setattr(warm, "_get_chart_cache_entry", lambda _key: stale)
    monkeypatch.setattr(
        warm,
        "_get_chart_cache_entry_ram_only",
        lambda _key: (
            {
                "is_full_snapshot": True,
                "validated_at": now,
                "max_time": latest,
            }
            if refreshed["ready"]
            else stale
        ),
    )
    monkeypatch.setattr(warm, "_get_last_user_request_time", lambda: 0.0)
    monkeypatch.setattr(warm, "chart_cache_metrics", lambda: {})
    monkeypatch.setattr(warm.LogUtil, "info", lambda _message: None)
    monkeypatch.setattr(warm, "_local_candidate_scopes", frozenset())
    monkeypatch.setattr(warm, "_local_candidate_updated_at", 0.0)

    def compute(_market, _code, _frequency, _config, **kwargs):
        calls.append(kwargs)
        refreshed["ready"] = True
        return True

    monkeypatch.setattr(warm, "compute_and_cache_chart_data", compute)

    warm._warm_targets((("a", "SH.600011"),), _FIVE_MINUTE_ONLY)

    assert calls == [
        {"incremental_refresh_days": warm._INCREMENTAL_REFRESH_DAYS},
    ]
    assert warm.candidate_local_history_ready("a", "SH.600011", "5m") is True


def test_worker_drops_duplicate_queued_while_successful_pass_runs(monkeypatch):
    targets = (("a", "SH.600003"),)
    calls = []

    def complete_and_enqueue_duplicate(current_targets):
        calls.append(current_targets)
        with warm._state_lock:
            warm._last_completed = current_targets
            warm._last_completed_at = time.time()
            warm._pending = current_targets

    monkeypatch.setattr(warm, "_warm_targets", complete_and_enqueue_duplicate)
    monkeypatch.setattr(warm, "_pending", targets)
    monkeypatch.setattr(warm, "_last_completed", None)
    monkeypatch.setattr(warm, "_last_completed_at", 0.0)
    monkeypatch.setattr(warm, "_worker_running", True)

    warm._worker_loop()

    assert calls == [targets]
    assert warm._pending is None
    assert warm._worker_running is False


def test_candidate_targets_use_snapshot_sector_ranks_for_equal_signals():
    shared = {
        "market": "a",
        "execution_profile": {"context_grade": "A"},
        "review_priority": 50,
        "lifecycle_stage": "triggered",
        "point_type": "2buy",
    }
    snapshot = {
        "sectors": [
            {"sector_id": "first", "rank": 1},
            {"sector_id": "second", "rank": 2},
        ],
        "signals": [
            {**shared, "code": "SH.600001", "sector": {"sector_id": "second"}},
            {**shared, "code": "SH.600002", "sector": {"sector_id": "first"}},
        ],
    }

    assert warm.candidate_chart_targets(snapshot) == (
        ("a", "SH.600002"),
        ("a", "SH.600001"),
    )


def test_candidate_targets_derive_the_same_position_priority_as_the_ui():
    shared = {
        "market": "a",
        "lifecycle_stage": "observed",
        "higher_timeframe_risk": {
            "market_gate": "GREEN",
            "sector_gate": "GREEN",
            "symbol_gate": "GREEN",
        },
        "warmup": {},
        "execution_profile": {"context_grade": "A"},
    }
    snapshot = {
        "signals": [
            {
                **shared,
                "code": "SH.600001",
                "position_recommendation": {"status": "BLOCKED"},
            },
            {
                **shared,
                "code": "SZ.300949",
                "position_recommendation": {"status": "NOT_ACTIONABLE"},
            },
        ]
    }

    assert warm.candidate_chart_targets(snapshot) == (
        ("a", "SZ.300949"),
        ("a", "SH.600001"),
    )


def test_candidate_targets_match_the_visible_current_selection_lifecycle():
    snapshot = {
        "signals": [
            {"market": "a", "code": "SH.600001", "lifecycle_stage": "formed"},
            {"market": "a", "code": "SH.600002", "lifecycle_stage": "observed"},
            {"market": "a", "code": "SH.600003", "lifecycle_stage": "closed"},
        ]
    }

    assert warm.candidate_chart_targets(snapshot) == (("a", "SH.600002"),)


def test_scheduler_defers_disk_warming_off_the_response_path(monkeypatch):
    created = []

    class FakeTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.name = ""
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(warm.threading, "Timer", FakeTimer)
    monkeypatch.setattr(warm, "_pending", None)
    monkeypatch.setattr(warm, "_last_completed", None)
    monkeypatch.setattr(warm, "_last_completed_at", 0.0)
    monkeypatch.setattr(warm, "_worker_running", False)
    monkeypatch.setattr(warm, "_local_candidate_scopes", frozenset())
    monkeypatch.setattr(warm, "_local_candidate_updated_at", 0.0)

    scheduled = warm.schedule_candidate_chart_cache_warm(
        {
            "signals": [
                {"market": "a", "code": "SH.600000", "lifecycle_stage": "observed"},
                {"market": "us", "code": "AAPL.US", "lifecycle_stage": "observed"},
            ]
        }
    )

    assert scheduled is True
    assert len(created) == 1
    assert created[0].interval == warm._START_DELAY_SECONDS
    assert created[0].function is warm._worker_loop
    assert created[0].daemon is True
    assert created[0].started is True
    assert warm.candidate_local_history_ready("A", "sh.600000") is False
    assert warm.candidate_local_history_ready("us", "AAPL.US") is False


def test_scheduler_rechecks_same_targets_after_refresh_interval(monkeypatch):
    created = []

    class FakeTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.name = ""
            created.append(self)

        def start(self):
            pass

    now = [10_000.0]
    targets = (("a", "SH.600000"),)
    monkeypatch.setattr(warm.time, "time", lambda: now[0])
    monkeypatch.setattr(warm.threading, "Timer", FakeTimer)
    monkeypatch.setattr(warm, "_pending", None)
    monkeypatch.setattr(warm, "_last_completed", targets)
    monkeypatch.setattr(warm, "_last_completed_at", now[0])
    monkeypatch.setattr(warm, "_worker_running", False)

    snapshot = {
        "signals": [
            {"market": "a", "code": "SH.600000", "lifecycle_stage": "observed"},
        ]
    }
    assert warm.schedule_candidate_chart_cache_warm(snapshot) is False
    now[0] += warm._REFRESH_CHECK_INTERVAL_SECONDS
    assert warm.schedule_candidate_chart_cache_warm(snapshot) is True
    assert len(created) == 1


def test_scheduler_defers_warming_until_live_capacity_is_stable(monkeypatch):
    created = []

    class FakeTimer:
        def __init__(self, interval, function):
            created.append((interval, function))

        def start(self):
            pass

    now = [20_000.0]
    monkeypatch.setattr(warm.time, "time", lambda: now[0])
    monkeypatch.setattr(warm.threading, "Timer", FakeTimer)
    monkeypatch.setattr(warm, "_pending", None)
    monkeypatch.setattr(warm, "_last_completed", None)
    monkeypatch.setattr(warm, "_last_completed_at", 0.0)
    monkeypatch.setattr(warm, "_worker_running", False)
    monkeypatch.setattr(warm, "_background_work_allowed", True)
    monkeypatch.setattr(warm, "_realtime_capacity_ready_since", 0.0)
    monkeypatch.setattr(warm, "_next_retry_at", 0.0)
    snapshot = {
        "signals": [
            {"market": "a", "code": "SH.600000", "lifecycle_stage": "observed"}
        ],
        "runtime_health": {
            "priority_monitor_session_open": True,
            "realtime_alert_capacity_ready": False,
        },
    }

    assert warm.schedule_candidate_chart_cache_warm(snapshot) is False
    assert warm._background_work_allowed is False
    snapshot["runtime_health"]["realtime_alert_capacity_ready"] = True
    assert warm.schedule_candidate_chart_cache_warm(snapshot) is False
    now[0] += warm._REALTIME_CAPACITY_STABILITY_SECONDS - 0.1
    assert warm.schedule_candidate_chart_cache_warm(snapshot) is False
    now[0] += 0.1
    assert warm.schedule_candidate_chart_cache_warm(snapshot) is True
    assert warm._background_work_allowed is True
    assert created == [(warm._START_DELAY_SECONDS, warm._worker_loop)]


def test_incomplete_warm_pass_uses_retry_backoff(monkeypatch):
    monkeypatch.setattr(warm, "query_cl_chart_config", lambda _market, _code: {})
    monkeypatch.setattr(warm, "_build_cache_key", lambda *_args: "candidate-key")
    monkeypatch.setattr(warm, "_get_chart_cache_entry", lambda _key: None)
    monkeypatch.setattr(warm, "_get_chart_cache_entry_ram_only", lambda _key: None)
    monkeypatch.setattr(warm, "compute_and_cache_chart_data", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(warm, "_get_last_user_request_time", lambda: 0.0)
    monkeypatch.setattr(warm, "chart_cache_metrics", lambda: {})
    monkeypatch.setattr(warm.LogUtil, "info", lambda _message: None)
    monkeypatch.setattr(warm, "_background_work_allowed", True)
    monkeypatch.setattr(warm, "_next_retry_at", 0.0)
    before = time.time()

    warm._warm_targets((('a', 'SH.600020'),), _FIVE_MINUTE_ONLY)

    assert warm._next_retry_at >= before + warm._INCOMPLETE_RETRY_SECONDS


def test_live_capacity_drop_interrupts_before_local_build(monkeypatch):
    calls = []
    monkeypatch.setattr(warm, "query_cl_chart_config", lambda _market, _code: {})
    monkeypatch.setattr(warm, "_build_cache_key", lambda *_args: "candidate-key")
    monkeypatch.setattr(warm, "_get_chart_cache_entry", lambda _key: None)
    monkeypatch.setattr(warm, "compute_and_cache_chart_data", lambda *_args, **_kwargs: calls.append(True))
    monkeypatch.setattr(warm, "chart_cache_metrics", lambda: {})
    monkeypatch.setattr(warm.LogUtil, "info", lambda _message: None)
    monkeypatch.setattr(warm, "_background_work_allowed", False)
    monkeypatch.setattr(warm, "_next_retry_at", 0.0)

    warm._warm_targets((('a', 'SH.600021'),), _FIVE_MINUTE_ONLY)

    assert calls == []
    assert warm._last_completed is None


def test_local_candidate_hint_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(warm.time, "time", lambda: now[0])
    monkeypatch.setattr(
        warm,
        "_local_candidate_scopes",
        frozenset({("a", "SH.600004", "5m")}),
    )
    monkeypatch.setattr(warm, "_local_candidate_updated_at", now[0])

    assert warm.candidate_local_history_ready("A", "sh.600004") is True
    assert warm.candidate_local_history_ready("a", "SH.600004", "1m") is False
    now[0] += warm._LOCAL_CANDIDATE_TTL_SECONDS + 0.1
    assert warm.candidate_local_history_ready("a", "SH.600004") is False
