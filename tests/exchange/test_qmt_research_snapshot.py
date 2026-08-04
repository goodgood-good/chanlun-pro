from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.a_sector_market_data import QmtASectorMarketData
from chanlun.exchange import exchange_qmt
from chanlun.exchange.exchange_qmt import (
    ExchangeQMT,
    QmtDailyAmount,
    QmtResearchTick,
)


CN = ZoneInfo("Asia/Shanghai")


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 17, hour, minute, second, tzinfo=CN)


def _native_ms(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


class _TrackingLock:
    def __init__(self) -> None:
        self.depth = 0
        self.enter_count = 0

    def __enter__(self):
        self.depth += 1
        self.enter_count += 1
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.depth -= 1


class _TickNative:
    def __init__(self, lock: _TrackingLock, payload: dict[str, object]) -> None:
        self.lock = lock
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []

    def get_full_tick(self, codes):
        assert self.lock.depth == 1
        self.calls.append(tuple(codes))
        return self.payload


def _install_tick_native(monkeypatch, payload):
    lock = _TrackingLock()
    native = _TickNative(lock, payload)
    monkeypatch.setattr(exchange_qmt, "_XTDATA_NATIVE_LOCK", lock)
    monkeypatch.setattr(exchange_qmt, "xtdata", native)
    return lock, native


def test_qmt_research_tick_snapshot_preserves_native_quote_time_last_close_and_status(
    monkeypatch,
):
    exchange = ExchangeQMT()
    quote_time = _at(10, 1)
    lock, native = _install_tick_native(
        monkeypatch,
        {
            "600000.SH": {
                "time": _native_ms(quote_time),
                "lastPrice": "10.25",
                "lastClose": "9.75",
                "volume": "123456",
                "stockStatus": 3,
            },
            "000001.SZ": {
                "time": _native_ms(quote_time - timedelta(seconds=1)),
                "lastPrice": Decimal("11.5"),
                "lastClose": Decimal("11.0"),
                "volume": 999,
                "stockStatus": 7,
            },
        },
    )

    snapshots = exchange.research_tick_snapshots(
        ("SH.600000", "SZ.000001")
    )

    assert tuple(snapshots) == ("SH.600000", "SZ.000001")
    assert snapshots["SH.600000"] == QmtResearchTick(
        code="SH.600000",
        native_time_ms=_native_ms(quote_time),
        last_price=Decimal("10.25"),
        last_close=Decimal("9.75"),
        volume=Decimal("123456"),
        stock_status=3,
    )
    assert snapshots["SZ.000001"].stock_status == 7
    assert native.calls == [("600000.SH", "000001.SZ")]
    assert lock.enter_count == 1
    assert lock.depth == 0


class _TickOnlyExchange:
    kline_time_label = "end"

    def __init__(self, tick: QmtResearchTick) -> None:
        self.tick = tick

    def research_tick_snapshots(self, codes):
        return {self.tick.code: self.tick}


class _UnusedCalendar:
    def session_for(self, trading_day):
        raise AssertionError("tick capture must not read the calendar")


def test_qmt_tick_adapter_uses_native_quote_time_not_poll_time_for_90_second_freshness():
    as_of = _at(10, 1, 30)
    stale_native_time = as_of - timedelta(seconds=91)
    adapter = QmtASectorMarketData(
        _TickOnlyExchange(
            QmtResearchTick(
                code="SH.600000",
                native_time_ms=_native_ms(stale_native_time),
                last_price=Decimal("10"),
                last_close=Decimal("9"),
                volume=Decimal("100"),
                stock_status=3,
            )
        ),
        trading_calendar=_UnusedCalendar(),
    )

    snapshot = adapter.capture_ticks(("SH.600000",), as_of=as_of)["SH.600000"]

    assert snapshot.quote_timestamp == stale_native_time
    assert snapshot.batch_captured_at == as_of
    assert snapshot.tradable is True
    assert snapshot.usable_for_breadth is False


class _DailyNative:
    def __init__(
        self,
        lock: _TrackingLock,
        raw: dict[str, pd.DataFrame],
    ) -> None:
        self.lock = lock
        self.raw = raw
        self.calls: list[tuple[str, object]] = []

    def download_history_data2(
        self,
        codes,
        period,
        *,
        start_time,
        end_time,
        incrementally,
    ):
        assert self.lock.depth == 1
        self.calls.append(
            (
                "download",
                (
                    tuple(codes),
                    period,
                    start_time,
                    end_time,
                    incrementally,
                ),
            )
        )

    def get_market_data(self, **kwargs):
        assert self.lock.depth == 1
        self.calls.append(("read", dict(kwargs)))
        return self.raw


def _daily_raw(
    codes: tuple[str, ...],
    timestamps: tuple[int, ...],
    amounts_by_code: dict[str, tuple[object, ...]],
) -> dict[str, pd.DataFrame]:
    return {
        "time": pd.DataFrame(
            [timestamps for _ in codes],
            index=list(codes),
        ),
        "amount": pd.DataFrame(
            [amounts_by_code[code] for code in codes],
            index=list(codes),
        ),
    }


def _install_daily_native(monkeypatch, raw):
    lock = _TrackingLock()
    native = _DailyNative(lock, raw)
    monkeypatch.setattr(exchange_qmt, "_XTDATA_NATIVE_LOCK", lock)
    monkeypatch.setattr(exchange_qmt, "xtdata", native)
    return lock, native


class _BoundedKlineNative:
    def __init__(self, lock: _TrackingLock, raw: dict[str, pd.DataFrame]) -> None:
        self.lock = lock
        self.raw = raw
        self.calls: list[tuple[str, dict[str, object]]] = []

    def download_history_data(self, **kwargs):
        assert self.lock.depth == 1
        self.calls.append(("download", dict(kwargs)))

    def get_market_data(self, **kwargs):
        assert self.lock.depth == 1
        self.calls.append(("read", dict(kwargs)))
        return self.raw


def test_qmt_research_exact_kline_bounds_native_download_and_read_without_ambient_now(
    monkeypatch,
):
    source_close = _at(10, 1)
    future_close = _at(10, 2)
    native_code = "600000.SH"
    raw = {
        "time": pd.DataFrame(
            [[_native_ms(source_close), _native_ms(future_close)]],
            index=[native_code],
        ),
        "open": pd.DataFrame([[10, 20]], index=[native_code]),
        "high": pd.DataFrame([[11, 21]], index=[native_code]),
        "low": pd.DataFrame([[9, 19]], index=[native_code]),
        "close": pd.DataFrame([[10.5, 20.5]], index=[native_code]),
        "volume": pd.DataFrame([[100, 200]], index=[native_code]),
    }
    lock = _TrackingLock()
    native = _BoundedKlineNative(lock, raw)
    monkeypatch.setattr(exchange_qmt, "_XTDATA_NATIVE_LOCK", lock)
    monkeypatch.setattr(exchange_qmt, "xtdata", native)

    class _NoAmbientNow(datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("research exact kline must not read ambient now")

    monkeypatch.setattr(exchange_qmt.datetime, "datetime", _NoAmbientNow)
    exchange = ExchangeQMT()

    frame = exchange.klines(
        "SH.600000",
        "1m",
        start_date="2026-07-17 10:00:00",
        end_date="2026-07-17 10:01:00",
        args={"research_exact_end": True},
    )

    assert tuple(frame["date"]) == (source_close,)
    # QMT's download boundary is exclusive, so transport advances one second
    # to fetch the exact 10:01 completion.  The read and dataframe visibility
    # remain frozen at 10:01; the fake provider deliberately returns 10:02 as
    # well and the assertion above proves it cannot leak through.
    assert [call[1]["end_time"] for call in native.calls] == [
        "20260717100101",
        "20260717100100",
    ]
    assert lock.enter_count == 1
    assert lock.depth == 0


def _twenty_sessions() -> tuple[date, ...]:
    return tuple(date(2026, 6, 16) + timedelta(days=value) for value in range(20))


def test_qmt_research_daily_amounts_returns_exact_twenty_native_complete_values(
    monkeypatch,
):
    exchange = ExchangeQMT()
    sessions = _twenty_sessions()
    timestamps = tuple(
        _native_ms(datetime.combine(day, datetime.min.time(), tzinfo=CN))
        for day in sessions
    )
    amounts = tuple(Decimal(index + 1) * Decimal("1000.125") for index in range(20))
    lock, native = _install_daily_native(
        monkeypatch,
        _daily_raw(
            ("600000.SH",),
            timestamps,
            {"600000.SH": amounts},
        ),
    )

    rows = exchange.research_daily_amounts(
        ("SH.600000",),
        start_session=sessions[0],
        end_session=sessions[-1],
    )["SH.600000"]

    assert len(rows) == 20
    assert tuple(row.amount for row in rows) == amounts
    assert tuple(row.session for row in rows) == sessions
    assert all(row.source_timestamp.tzinfo is not None for row in rows)
    assert all(type(row) is QmtDailyAmount for row in rows)
    assert lock.enter_count == 1
    assert [call[0] for call in native.calls] == ["download", "read"]
    download = native.calls[0][1]
    assert download == (
        ("600000.SH",),
        "1d",
        sessions[0].strftime("%Y%m%d"),
        sessions[-1].strftime("%Y%m%d"),
        True,
    )
    read = native.calls[1][1]
    assert read["field_list"] == ["time", "amount"]
    assert read["stock_list"] == ["600000.SH"]
    assert read["period"] == "1d"
    assert read["fill_data"] is False


@pytest.mark.parametrize(
    "codes",
    (
        ["SH.600000"],
        ("600000.SH",),
        ("sh.600000",),
        ("SH.600000", "SH.600000"),
    ),
)
def test_qmt_research_methods_reject_nonexact_or_duplicate_codes_before_native(
    monkeypatch,
    codes,
):
    exchange = ExchangeQMT()
    lock, native = _install_tick_native(monkeypatch, {})

    with pytest.raises((TypeError, ValueError), match="codes"):
        exchange.research_tick_snapshots(codes)

    assert lock.enter_count == 0
    assert native.calls == []


def test_qmt_research_tick_rejects_unknown_native_code(monkeypatch):
    exchange = ExchangeQMT()
    _install_tick_native(
        monkeypatch,
        {
            "000001.SZ": {
                "time": _native_ms(_at(10, 1)),
                "lastPrice": 10,
                "lastClose": 9,
                "volume": 100,
                "stockStatus": 3,
            }
        },
    )

    with pytest.raises(RuntimeError, match="unknown native code"):
        exchange.research_tick_snapshots(("SH.600000",))


@pytest.mark.parametrize(
    "bad_field",
    ("time", "lastPrice", "lastClose", "volume", "stockStatus"),
)
def test_qmt_research_tick_omits_missing_native_fact(monkeypatch, bad_field):
    exchange = ExchangeQMT()
    payload = {
        "time": _native_ms(_at(10, 1)),
        "lastPrice": 10,
        "lastClose": 9,
        "volume": 100,
        "stockStatus": 3,
    }
    payload.pop(bad_field)
    _install_tick_native(monkeypatch, {"600000.SH": payload})

    assert exchange.research_tick_snapshots(("SH.600000",)) == {}


@pytest.mark.parametrize("bad_amount", (float("nan"), 0, -1))
def test_qmt_research_daily_invalid_native_amount_invalidates_code(
    monkeypatch,
    bad_amount,
):
    exchange = ExchangeQMT()
    sessions = _twenty_sessions()
    timestamps = tuple(
        _native_ms(datetime.combine(day, datetime.min.time(), tzinfo=CN))
        for day in sessions
    )
    amounts = [index + 1 for index in range(20)]
    amounts[7] = bad_amount
    _install_daily_native(
        monkeypatch,
        _daily_raw(
            ("600000.SH",),
            timestamps,
            {"600000.SH": tuple(amounts)},
        ),
    )

    result = exchange.research_daily_amounts(
        ("SH.600000",),
        start_session=sessions[0],
        end_session=sessions[-1],
    )

    assert result == {"SH.600000": ()}


def test_qmt_research_daily_ohlcv_only_has_no_amount_fallback(monkeypatch):
    exchange = ExchangeQMT()
    sessions = _twenty_sessions()
    timestamps = tuple(
        _native_ms(datetime.combine(day, datetime.min.time(), tzinfo=CN))
        for day in sessions
    )
    raw = {
        "time": pd.DataFrame([timestamps], index=["600000.SH"]),
        "close": pd.DataFrame([[10] * 20], index=["600000.SH"]),
        "volume": pd.DataFrame([[100] * 20], index=["600000.SH"]),
    }
    _install_daily_native(monkeypatch, raw)

    result = exchange.research_daily_amounts(
        ("SH.600000",),
        start_session=sessions[0],
        end_session=sessions[-1],
    )

    assert result == {"SH.600000": ()}


def test_qmt_research_daily_rejects_misaligned_native_field_columns(monkeypatch):
    exchange = ExchangeQMT()
    sessions = _twenty_sessions()
    timestamps = tuple(
        _native_ms(datetime.combine(day, datetime.min.time(), tzinfo=CN))
        for day in sessions
    )
    raw = _daily_raw(
        ("600000.SH",),
        timestamps,
        {"600000.SH": tuple(range(1, 21))},
    )
    raw["amount"].columns = tuple(reversed(raw["amount"].columns))
    _install_daily_native(monkeypatch, raw)

    result = exchange.research_daily_amounts(
        ("SH.600000",),
        start_session=sessions[0],
        end_session=sessions[-1],
    )

    assert result == {"SH.600000": ()}


def test_qmt_research_daily_filters_current_or_future_native_row(monkeypatch):
    exchange = ExchangeQMT()
    sessions = _twenty_sessions()
    current = sessions[-1] + timedelta(days=1)
    all_sessions = (*sessions, current)
    timestamps = tuple(
        _native_ms(datetime.combine(day, datetime.min.time(), tzinfo=CN))
        for day in all_sessions
    )
    amounts = tuple(range(1, 22))
    _install_daily_native(
        monkeypatch,
        _daily_raw(
            ("600000.SH",),
            timestamps,
            {"600000.SH": amounts},
        ),
    )

    rows = exchange.research_daily_amounts(
        ("SH.600000",),
        start_session=sessions[0],
        end_session=sessions[-1],
    )["SH.600000"]

    assert tuple(row.session for row in rows) == sessions
    assert tuple(row.amount for row in rows) == tuple(Decimal(value) for value in range(1, 21))


def test_qmt_research_daily_rejects_unknown_native_code(monkeypatch):
    exchange = ExchangeQMT()
    session = date(2026, 7, 16)
    timestamp = _native_ms(
        datetime.combine(session, datetime.min.time(), tzinfo=CN)
    )
    _install_daily_native(
        monkeypatch,
        _daily_raw(
            ("000001.SZ",),
            (timestamp,),
            {"000001.SZ": (100,)},
        ),
    )

    with pytest.raises(RuntimeError, match="unknown native code"):
        exchange.research_daily_amounts(
            ("SH.600000",),
            start_session=session,
            end_session=session,
        )


@pytest.mark.parametrize("mutation", ("duplicate", "reverse"))
def test_qmt_research_daily_rejects_non_strict_native_order(
    monkeypatch,
    mutation,
):
    exchange = ExchangeQMT()
    sessions = _twenty_sessions()
    timestamps = [
        _native_ms(datetime.combine(day, datetime.min.time(), tzinfo=CN))
        for day in sessions
    ]
    if mutation == "duplicate":
        timestamps[10] = timestamps[9]
    else:
        timestamps[9], timestamps[10] = timestamps[10], timestamps[9]
    _install_daily_native(
        monkeypatch,
        _daily_raw(
            ("600000.SH",),
            tuple(timestamps),
            {"600000.SH": tuple(range(1, 21))},
        ),
    )

    result = exchange.research_daily_amounts(
        ("SH.600000",),
        start_session=sessions[0],
        end_session=sessions[-1],
    )

    assert result == {"SH.600000": ()}


def test_qmt_research_values_require_exact_native_integer_and_session_facts():
    common_tick = {
        "code": "SH.600000",
        "native_time_ms": _native_ms(_at(10, 1)),
        "last_price": Decimal("10"),
        "last_close": Decimal("9"),
        "volume": Decimal("100"),
        "stock_status": 3,
    }
    with pytest.raises(ValueError, match="native_time_ms"):
        QmtResearchTick(**{**common_tick, "native_time_ms": True})
    with pytest.raises(ValueError, match="stock_status"):
        QmtResearchTick(**{**common_tick, "stock_status": True})

    session = date(2026, 7, 16)
    with pytest.raises(ValueError, match="timezone-aware"):
        QmtDailyAmount(
            code="SH.600000",
            source_timestamp=datetime.combine(session, datetime.min.time()),
            session=session,
            amount=Decimal("100"),
        )
    with pytest.raises(ValueError, match="Shanghai date"):
        QmtDailyAmount(
            code="SH.600000",
            source_timestamp=datetime.combine(
                session,
                datetime.min.time(),
                tzinfo=CN,
            ),
            session=session + timedelta(days=1),
            amount=Decimal("100"),
        )
