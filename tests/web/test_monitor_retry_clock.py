"""A symbol's due retry is independent of another batch's idle deadline."""

from datetime import datetime, timedelta

import pytest

from chanlun.screening.markets import closed_bar_cutoff
from chanlun.screening.rules import CN
from tests.web.test_monitor_clock_wake import waiting_monitor


def retry_due_in_twenty_seconds(service, clock, key):
    state = service._load()
    cutoff = state["attempted_cutoffs"][key]
    state["active_cutoffs"] = {key: cutoff}
    interval = state["settings"]["interval_seconds"]
    service._record_failures({key}, clock[0].timestamp() + 20 - interval, "previous-failure")
    assert state["retry_symbols"][key]["retry_at"] == clock[0].timestamp() + 20
    return cutoff


@pytest.mark.parametrize("market,code,frequency,hour,minute", [
    ("a", "SH.600000", "5m", 10, 4),
    ("us", "AMD.US", "5m", 21, 39),
    ("hk", "00700", "30m", 10, 4),
    ("currency_spot", "BTC/USDT", "5m", 10, 4),
])
def test_due_retry_bypasses_idle_wait_without_retrying_early(
    tmp_path, market, code, frequency, hour, minute
):
    service, live, clock, key = waiting_monitor(
        tmp_path, market, code, frequency,
        datetime(2026, 9, 21, hour, minute, tzinfo=CN),
    )
    cutoff = retry_due_in_twenty_seconds(service, clock, key)
    clock[0] += timedelta(seconds=19)
    service._schedule_monitor(clock[0], None)
    assert not live.started
    clock[0] += timedelta(seconds=1)
    assert clock[0].timestamp() < service._load()["next_check_at"]
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1, "the global idle deadline delayed a due symbol retry"
    assert service._load()["active_cutoffs"][key] == cutoff
    assert service._load()["retry_symbols"][key]["failures"] == 1
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1, "a due retry cannot overlap its own active worker"


@pytest.mark.parametrize("interval", [60, 180])
def test_shorter_period_batch_cannot_postpone_another_symbols_retry(tmp_path, interval):
    service, live, clock, key = waiting_monitor(
        tmp_path, "a", "SH.600000", "30m",
        datetime(2026, 9, 21, 10, 4, tzinfo=CN), interval=interval,
    )
    cutoff = retry_due_in_twenty_seconds(service, clock, key)
    state = service._load()
    fast = {"market": "currency_spot", "code": "BTC/USDT", "name": "BTC/USDT"}
    fast_key = "currency_spot:BTC/USDT"
    state["seed"]["codes"].append(fast_key)
    state["seed"]["symbols"].append(fast)
    state["holding_risk_plans"] = {fast_key + ":1m": {**fast, "frequency": "1m", "active": True}}
    state["attempted_cutoffs"][fast_key] = closed_bar_cutoff(
        fast["market"], fast["code"], clock[0], "1m") - 60
    state["next_check_at"] = clock[0].timestamp() + 5
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1 and live.started[0]["symbols"] == [fast]
    assert state["next_check_at"] == clock[0].timestamp() + interval
    # The short batch finishes; its next idle check remains in the future.
    live.finish(state["active_cutoffs"][fast_key])
    state["processed_run"] = state["active_monitor_run"]
    clock[0] += timedelta(seconds=20)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2, "the completed short-period batch postponed a different retry"
    assert [symbol["code"] for symbol in live.started[-1]["symbols"]] == ["SH.600000"]
    assert state["active_cutoffs"][key] == cutoff


@pytest.mark.parametrize("blocked_by", ["paused", "exhausted"])
def test_due_retry_keeps_pause_and_attempt_limit_enforced(tmp_path, blocked_by):
    service, live, clock, key = waiting_monitor(
        tmp_path, "a", "SH.600000", "5m", datetime(2026, 9, 21, 10, 4, tzinfo=CN)
    )
    retry_due_in_twenty_seconds(service, clock, key)
    state = service._load()
    if blocked_by == "paused":
        state["enabled"] = False
    else:
        state["retry_symbols"][key]["failures"] = 3
    clock[0] += timedelta(seconds=20)
    service._schedule_monitor(clock[0], None)
    assert not live.started
