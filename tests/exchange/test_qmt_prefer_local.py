"""Explicit chart local-first reads need full, causally closed source coverage."""

from contextlib import contextmanager
import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.exchange.a_share_minute_grid import (
    a_share_completed_one_minute_closes,
)
from chanlun.exchange import trading_session
from chanlun.exchange import exchange_qmt


CN = ZoneInfo("Asia/Shanghai")


def _closes(frequency="1m", session=dt.date(2026, 9, 4)):
    size = {"1m": 1, "5m": 5, "30m": 30}[frequency]
    return a_share_completed_one_minute_closes(session)[size - 1::size]


def _raw(times):
    n = len(times)
    values = {
        "time": [int(pd.Timestamp(value).timestamp() * 1000) for value in times],
        "open": [10.0] * n,
        "high": [11.0] * n,
        "low": [9.0] * n,
        "close": [10.5] * n,
        "volume": [100.0] * n,
    }
    return {key: pd.DataFrame([value]) for key, value in values.items()}


@pytest.fixture
def qmt(monkeypatch):
    class Clock(dt.datetime):
        observed = dt.datetime(2026, 9, 6, 15, 0, tzinfo=CN)

        @classmethod
        def now(cls, tz=None):
            return cls.observed.astimezone(tz) if tz else cls.observed.replace(tzinfo=None)

    clock_module = SimpleNamespace(**vars(dt))
    clock_module.datetime = Clock
    monkeypatch.setattr(exchange_qmt, "datetime", clock_module)
    events = []
    responses = []
    reads = []

    @contextmanager
    def lane():
        events.append("lane_enter")
        try:
            yield
        finally:
            events.append("lane_exit")

    def read(**kwargs):
        assert exchange_qmt._XTDATA_NATIVE_LOCK._is_owned()
        events.append("read")
        reads.append(kwargs)
        return responses.pop(0)

    def factors(*_args):
        assert exchange_qmt._XTDATA_NATIVE_LOCK._is_owned()
        events.append("factors")
        return pd.DataFrame()

    monkeypatch.setattr(exchange_qmt, "_xtdata_download_interprocess_lock", lane)
    monkeypatch.setattr(exchange_qmt.xtdata, "get_market_data", read)
    monkeypatch.setattr(exchange_qmt.xtdata, "get_divid_factors", factors)
    monkeypatch.setattr(
        exchange_qmt.xtdata, "download_history_data",
        lambda **_kw: events.append("download"),
    )
    return SimpleNamespace(
        ex=exchange_qmt.ExchangeQMT(), events=events, responses=responses,
        reads=reads, clock=Clock,
    )


def _request(qmt, frequency="1m", *, args=None, start="2026-09-04", end="2026-09-06 15:00:00"):
    return qmt.ex.klines(
        "SZ.301004", frequency, start_date=start, end_date=end,
        args={"prefer_local": True} if args is None else args,
    )


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
def test_weekend_complete_local_grid_avoids_download_lane(qmt, frequency):
    times = _closes(frequency)
    qmt.responses.append(_raw(times))

    result = _request(qmt, frequency)

    assert qmt.events == ["read", "factors"]
    assert tuple(result.date) == times
    assert result.attrs["qmt_history_read_mode"] == "local_complete"
    assert result.attrs["qmt_local_history_verified_through"] == times[-1].isoformat()
    assert result.attrs["qmt_local_history_calendar_revision"].startswith("sha256:")
    assert result.attrs["price_basis_provider"] == "qmt"
    assert result.attrs["price_basis_adjustment"] == "front_ratio"
    assert result.attrs["price_basis_revision"].startswith("sha256:")
    assert "validated_at" not in result.attrs
    assert qmt.reads[0]["end_time"] == "20260906150000"
    assert qmt.reads[0]["fill_data"] is False


@pytest.mark.parametrize("missing_index", [0, 20, 239])
def test_missing_head_middle_or_tail_requires_locked_download(qmt, missing_index):
    complete = _closes()
    incomplete = complete[:missing_index] + complete[missing_index + 1:]
    qmt.responses.extend([_raw(incomplete), _raw(complete)])

    result = _request(qmt)

    assert qmt.events == ["read", "factors", "lane_enter", "download", "read", "factors", "lane_exit"]
    assert tuple(result.date) == complete
    assert "qmt_history_read_mode" not in result.attrs


def test_stale_friday_cannot_qualify_for_monday_completed_minutes(qmt):
    qmt.clock.observed = dt.datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    complete = _closes() + _closes(session=dt.date(2026, 9, 7))[:30]
    qmt.responses.extend([_raw(_closes()), _raw(complete)])

    result = _request(qmt, end="2026-09-07 10:00:00")

    assert qmt.events.count("download") == 1
    assert tuple(result.date) == complete
    assert "qmt_history_read_mode" not in result.attrs


def test_lunch_and_future_end_use_actual_observation_not_future_bars(qmt):
    qmt.clock.observed = dt.datetime(2026, 9, 4, 12, 15, tzinfo=CN)
    times = _closes()[:120]
    qmt.responses.append(_raw(times))

    result = _request(qmt, end="2026-09-04 15:00:00")

    assert qmt.events == ["read", "factors"]
    assert tuple(result.date) == times
    assert result.attrs["qmt_local_history_verified_through"] == "2026-09-04T11:30:00+08:00"
    assert qmt.reads[0]["end_time"] == "20260904121500"


def test_official_holiday_keeps_last_actual_session_without_weekday_guess(qmt):
    qmt.clock.observed = dt.datetime(2026, 10, 2, 15, 0, tzinfo=CN)
    times = _closes("5m", dt.date(2026, 9, 30))
    qmt.responses.append(_raw(times))

    result = _request(qmt, "5m", start="2026-09-30", end="2026-10-02 15:00:00")

    assert qmt.events == ["read", "factors"]
    assert tuple(result.date) == times


def test_explicit_count_proves_full_tail_even_when_query_starts_before_calendar(qmt):
    qmt.responses.append(_raw(_closes()))
    args = {"prefer_local": True, "req_counts": 100}

    result = _request(qmt, args=args, start="2025-09-01")

    assert qmt.events == ["read", "factors"]
    assert tuple(result.date) == _closes()[-100:]
    assert args == {"prefer_local": True, "req_counts": 100}
    assert qmt.reads[0]["start_time"] == "20250901"


def test_count_and_recent_tail_cannot_hide_a_missing_required_minute(qmt):
    times = _closes()
    tail = times[-100:]
    replaced = (times[-101], *tail[:20], *tail[21:])
    assert len(replaced) == 100 and replaced[-1] == times[-1]
    qmt.responses.extend([_raw(replaced), _raw(times)])

    result = _request(qmt, args={"prefer_local": True, "req_counts": 100})

    assert qmt.events.count("download") == 1
    assert tuple(result.date) == tail
    assert "qmt_history_read_mode" not in result.attrs


def test_local_and_downloaded_source_rows_and_price_basis_are_identical(qmt):
    times = _closes()
    qmt.responses.extend([_raw(times), _raw(times)])
    local = _request(qmt)
    downloaded = _request(qmt, args={"prefer_local": False})
    pd.testing.assert_frame_equal(local, downloaded)
    qualification_fields = {
        "qmt_history_read_mode", "qmt_local_history_verified_through",
        "qmt_local_history_calendar_revision",
    }
    assert {key: value for key, value in local.attrs.items() if key not in qualification_fields} == downloaded.attrs


@pytest.mark.parametrize("args", [{"prefer_local": True}, {"prefer_local": True, "req_counts": 100_000}])
def test_unproved_full_history_never_silently_shrinks_to_calendar(qmt, args):
    qmt.responses.append(_raw(_closes()))

    result = _request(qmt, args=args, start="2025-09-01")

    assert qmt.events == ["lane_enter", "download", "read", "factors", "lane_exit"]
    assert "qmt_history_read_mode" not in result.attrs


def test_opening_events_are_preserved_and_do_not_replace_missing_minutes(qmt):
    times = (
        dt.datetime(2026, 9, 4, 9, 25, tzinfo=CN),
        dt.datetime(2026, 9, 4, 9, 30, tzinfo=CN),
        *_closes(),
    )
    qmt.responses.append(_raw(times))
    result = _request(qmt)
    assert tuple(result.date) == times
    assert qmt.events == ["read", "factors"]

    qmt.events.clear()
    qmt.responses.extend([_raw(times[:-1]), _raw(times)])
    result = _request(qmt)
    assert qmt.events.count("download") == 1
    assert "qmt_history_read_mode" not in result.attrs


@pytest.mark.parametrize("defect", ["duplicate", "reversed", "unknown_event", "bad_price", "empty"])
def test_invalid_local_fact_cannot_qualify_even_with_recent_tail(qmt, defect):
    times = _closes()
    raw = _raw(times)
    if defect == "duplicate":
        raw = _raw((times[0], *times))
    elif defect == "reversed":
        raw = _raw((times[1], times[0], *times[2:]))
    elif defect == "unknown_event":
        raw = _raw((dt.datetime(2026, 9, 4, 9, 26, tzinfo=CN), *times))
    elif defect == "bad_price":
        raw["high"].iloc[0, 20] = 8.0
    else:
        raw = {}
    qmt.responses.extend([raw, _raw(times)])

    result = _request(qmt)

    assert qmt.events.count("download") == 1
    assert "qmt_history_read_mode" not in result.attrs


def test_corrupt_calendar_uses_original_download_instead_of_assuming_weekdays(qmt, monkeypatch):
    def corrupt(**_kwargs):
        raise ValueError("official calendar source hash is invalid")

    monkeypatch.setattr(trading_session, "official_trading_session_evidence", corrupt)
    qmt.responses.append(_raw(_closes()))
    result = _request(qmt)
    assert qmt.events == ["lane_enter", "download", "read", "factors", "lane_exit"]
    assert "qmt_history_read_mode" not in result.attrs


def test_local_factor_failure_does_not_launder_price_metadata(qmt, monkeypatch):
    calls = []

    def factors(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("local factor response unavailable")
        return pd.DataFrame()

    monkeypatch.setattr(exchange_qmt.xtdata, "get_divid_factors", factors)
    qmt.responses.extend([_raw(_closes()), _raw(_closes())])
    result = _request(qmt)
    assert qmt.events.count("download") == 1
    assert len(calls) == 2
    assert result.attrs["price_basis_revision"].startswith("sha256:")
    assert "qmt_history_read_mode" not in result.attrs


@pytest.mark.parametrize("args", [None, {"prefer_local": False}])
def test_default_download_contract_is_unchanged(qmt, args):
    qmt.responses.append(_raw(_closes()))
    qmt.ex.klines("SZ.301004", "1m", start_date="2026-09-04", end_date="2026-09-06", args=args)
    assert qmt.events == ["lane_enter", "download", "read", "factors", "lane_exit"]


def test_explicit_skip_download_remains_separate_caller_contract(qmt):
    qmt.responses.append(_raw(_closes()[:20]))
    result = _request(qmt, args={"skip_download": True, "prefer_local": True})
    assert qmt.events == ["read", "factors"]
    assert len(result) == 20
    assert "qmt_history_read_mode" not in result.attrs


@pytest.mark.parametrize("end", [None, "2023-09-06 15:00:00"])
def test_unbounded_or_uncovered_end_keeps_original_download(qmt, end):
    qmt.responses.append(_raw(_closes()))
    _request(qmt, start="2023-09-01", end=end)
    assert qmt.events == ["lane_enter", "download", "read", "factors", "lane_exit"]


@pytest.mark.parametrize("value", [1, "true", None])
def test_prefer_local_requires_exact_bool(qmt, value):
    with pytest.raises(ValueError, match="prefer_local must be an exact bool"):
        qmt.ex.klines.__wrapped__(qmt.ex, "SZ.301004", "1m", args={"prefer_local": value})
    assert qmt.events == []


def test_explicit_download_start_preserves_full_read_prefix(qmt, monkeypatch):
    downloaded = []
    monkeypatch.setattr(exchange_qmt.xtdata, "download_history_data", lambda **kw: downloaded.append(kw))
    qmt.responses.append(_raw(_closes()))
    _request(qmt, start="2026-08-01", end="2026-09-04 15:00:00",
             args={"download_start_date": "20260903", "exact_end": True})
    assert downloaded[0]["start_time"] == "20260903"
    assert qmt.reads[0]["start_time"] == "20260801"
    assert qmt.reads[0]["end_time"] == "20260904150000"
    assert downloaded[0]["incrementally"] is True


@pytest.mark.parametrize("options", [
    {"download_start_date": "20260999"}, {"download_start_date": "20260910"},
    {"download_start_date": "20260903", "incremental_refresh_days": 2},
])
def test_invalid_download_start_does_not_reach_qmt(qmt, options):
    with pytest.raises(ValueError):
        qmt.ex.klines.__wrapped__(qmt.ex, "SZ.301004", "1m", start_date="2026-08-01",
                                  end_date="2026-09-04 15:00:00", args={"exact_end": True, **options})
    assert qmt.events == []
