"""Cache freshness cannot be inferred from future or invalid wall clocks."""

import pytest

from cl_app.services import chart_cache as cache


def entry(stamp=100_000.0):
    return cache._build_chart_cache_entry(
        {"t": [1000, 5000], "c": [10., 11.]}, is_full_snapshot=True, validated_at=stamp
    )


@pytest.mark.parametrize("trading", [False, True])
@pytest.mark.parametrize("stamp", [100_001.0, 186_400.0, float("inf")])
def test_first_request_revalidates_future_or_infinite_validation_times(trading, stamp):
    hit, data, reason, refresh = cache.evaluate_cache_for_tv_history(
        entry(stamp), 0, 0, False, market_is_trading=trading, now=100_000.0
    )
    assert (hit, data, reason, refresh) == (False, None, "cache_stale_snapshot", False)


@pytest.mark.parametrize("stamp", [100_001.0, float("inf"), float("nan"), True])
def test_polling_never_calls_invalid_clock_metadata_recent(monkeypatch, stamp):
    monkeypatch.setattr(cache.time, "time", lambda: 100_000.0)
    assert cache._entry_freshness(entry(stamp), "polling") == "unknown"
    assert not cache._cache_entry_recently_validated(entry(stamp))


@pytest.mark.parametrize("stamp", [float("nan"), float("inf"), -float("inf"), True])
def test_invalid_validation_time_does_not_enter_the_normalized_cache(stamp):
    assert cache._normalize_cache_entry(entry(stamp)) is None


def test_clock_regression_does_not_make_a_missing_tail_look_current(monkeypatch):
    monkeypatch.setattr(cache.time, "time", lambda: 100_000.0)
    assert cache.evaluate_cache_for_tv_history(entry(100_010.0), 2000, 9000, True) == (
        False, None, "cache_tail_gap", False
    )


@pytest.mark.parametrize("deadline", [None, "unknown", float("inf"), float("nan"), True])
def test_declared_invalid_source_deadline_cannot_extend_cache_lifetime(monkeypatch, deadline):
    monkeypatch.setattr(cache.time, "time", lambda: 100_000.0)
    value = entry()
    value["data"]["_history_source_valid_until"] = deadline
    assert cache.evaluate_cache_for_tv_history(value, 0, 0, False, now=100_000.0)[0] is False
    assert not cache._cache_entry_recently_validated(value)


def test_valid_boundary_and_missing_legacy_deadline_keep_existing_behavior(monkeypatch):
    monkeypatch.setattr(cache.time, "time", lambda: 100_000.0)
    assert cache._cache_entry_recently_validated(entry(99_999.0))
    assert not cache._cache_entry_recently_validated(entry(99_970.0))
    assert cache.evaluate_cache_for_tv_history(entry(), 0, 0, False, now=100_000.0)[:2] == (
        True, entry()["data"]
    )
    value = entry()
    value["data"]["_history_source_valid_until"] = 100_001.0
    assert cache.evaluate_cache_for_tv_history(value, 0, 0, False, now=100_000.0)[0] is True
    assert cache.evaluate_cache_for_tv_history(value, 0, 0, False, now=100_001.0)[0] is False
