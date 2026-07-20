from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import chanlun.decision_support.a_sector_market_data as market_data_module
from chanlun.decision_support.a_sector_market_data import (
    PhysicalBar,
    QmtASectorMarketData,
    TickSnapshot,
    physical_bar_source_fingerprint,
)
from chanlun.exchange.exchange_qmt import QmtDailyAmount, QmtResearchTick


CN = ZoneInfo("Asia/Shanghai")


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 17, hour, minute, second, tzinfo=CN)


def _native_ms(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def _bar(
    *,
    code: str = "SH.600000",
    frequency: str = "1m",
    closed_at: datetime | None = None,
    volume: Decimal = Decimal("100"),
) -> PhysicalBar:
    physical_close = closed_at or _at(10, 1)
    minutes = {"1m": 1, "5m": 5, "30m": 30}[frequency]
    physical_open = physical_close - timedelta(minutes=minutes)
    values = {
        "code": code,
        "frequency": frequency,
        "source_timestamp": physical_close,
        "physical_open": physical_open,
        "physical_close": physical_close,
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9"),
        "close": Decimal("10.5"),
        "volume": volume,
    }
    return PhysicalBar(
        **values,
        source_bar_fingerprint=physical_bar_source_fingerprint(**values),
    )


@pytest.mark.parametrize(
    ("override", "message"),
    (
        ({"source_timestamp": datetime(2026, 7, 17, 10, 1)}, "timezone-aware"),
        ({"physical_open": _at(10, 0, 1)}, "duration"),
        ({"source_timestamp": _at(10, 0)}, "source_timestamp"),
        ({"close": Decimal("NaN")}, "finite Decimal"),
        ({"volume": Decimal("-1")}, "non-negative"),
        ({"open": 10.0}, "finite Decimal"),
    ),
)
def test_physical_bar_rejects_nonphysical_or_noncanonical_facts(override, message):
    closed_at = _at(10, 1)
    values = {
        "code": "SH.600000",
        "frequency": "1m",
        "source_timestamp": closed_at,
        "physical_open": _at(10, 0),
        "physical_close": closed_at,
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9"),
        "close": Decimal("10.5"),
        "volume": Decimal("100"),
    }
    values.update(override)

    with pytest.raises(ValueError, match=message):
        PhysicalBar(
            **values,
            source_bar_fingerprint="sha256:" + "0" * 64,
        )


def test_physical_bar_source_fingerprint_binds_exact_endpoint_and_ohlcv():
    first = _bar()
    second = _bar(volume=Decimal("101"))

    assert first.source_timestamp == first.physical_close
    assert first.physical_open == _at(10, 0)
    assert first.source_bar_fingerprint != second.source_bar_fingerprint
    with pytest.raises(ValueError, match="source_bar_fingerprint mismatch"):
        PhysicalBar(
            code=first.code,
            frequency=first.frequency,
            source_timestamp=first.source_timestamp,
            physical_open=first.physical_open,
            physical_close=first.physical_close,
            open=first.open,
            high=first.high,
            low=first.low,
            close=first.close,
            volume=first.volume,
            source_bar_fingerprint=second.source_bar_fingerprint,
        )


@dataclass(frozen=True)
class _Session:
    trading_day: date
    previous_trading_day: date | None


class _Calendar:
    def __init__(self, days: tuple[date, ...]) -> None:
        self.days = days
        self.positions = {day: index for index, day in enumerate(days)}

    def session_for(self, trading_day: date):
        position = self.positions.get(trading_day)
        if position is None:
            return None
        return _Session(
            trading_day=trading_day,
            previous_trading_day=(None if position == 0 else self.days[position - 1]),
        )


class _FakeExchange:
    kline_time_label = "end"

    def __init__(self) -> None:
        self.frames: dict[tuple[str, str], pd.DataFrame] = {}
        self.daily: dict[str, tuple[QmtDailyAmount, ...]] = {}
        self.ticks: dict[str, QmtResearchTick] = {}
        self.kline_calls: list[tuple[object, ...]] = []
        self.daily_calls: list[tuple[object, ...]] = []

    def klines(self, code, frequency, start_date=None, end_date=None, args=None):
        self.kline_calls.append((code, frequency, start_date, end_date, args))
        return self.frames.get((code, frequency), pd.DataFrame()).copy(deep=True)

    def research_daily_amounts(
        self,
        codes,
        *,
        start_session,
        end_session,
    ):
        self.daily_calls.append((codes, start_session, end_session))
        return {code: self.daily.get(code, ()) for code in codes}

    def research_tick_snapshots(self, codes):
        return {code: self.ticks[code] for code in codes if code in self.ticks}


def _frame(*closed_at: datetime) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["SH.600000"] * len(closed_at),
            "date": list(closed_at),
            "open": [10] * len(closed_at),
            "high": [11] * len(closed_at),
            "low": [9] * len(closed_at),
            "close": [10.5] * len(closed_at),
            "volume": [100] * len(closed_at),
        }
    )


def _calendar_days() -> tuple[date, ...]:
    return tuple(date(2026, 6, 1) + timedelta(days=value) for value in range(47))


def _daily_row(code: str, session: date, amount: object) -> QmtDailyAmount:
    return QmtDailyAmount(
        code=code,
        source_timestamp=datetime.combine(
            session,
            datetime.min.time(),
            tzinfo=CN,
        ),
        session=session,
        amount=Decimal(str(amount)),
    )


def test_qmt_sector_market_data_uses_physical_closed_1m_and_real_daily_amount():
    exchange = _FakeExchange()
    days = _calendar_days()
    calendar = _Calendar(days)
    exchange.frames[("SH.600000", "1m")] = _frame(_at(10, 1), _at(10, 2))
    prior = days[-21:-1]
    exchange.daily["SH.600000"] = tuple(
        _daily_row("SH.600000", session, Decimal(index + 1) * Decimal("10.25"))
        for index, session in enumerate(prior)
    )
    adapter = QmtASectorMarketData(exchange, trading_calendar=calendar)

    bars = adapter.closed_bars(
        ("SH.600000", "SZ.000001"),
        "1m",
        start=_at(10, 0),
        end=_at(10, 1),
        as_of=_at(10, 1, 30),
    )
    amounts = adapter.prior_complete_daily_amounts(
        ("SH.600000", "SZ.000001"),
        before_session=days[-1],
    )

    assert tuple(bars) == ("SH.600000", "SZ.000001")
    assert len(bars["SH.600000"]) == 1
    assert bars["SH.600000"][0].source_timestamp == _at(10, 1)
    assert bars["SH.600000"][0].physical_open == _at(10, 0)
    assert bars["SH.600000"][0].physical_close == _at(10, 1)
    assert bars["SZ.000001"] == ()
    assert amounts["SH.600000"] == tuple(
        Decimal(index + 1) * Decimal("10.25") for index in range(20)
    )
    assert amounts["SZ.000001"] == ()
    assert exchange.daily_calls == [
        (("SH.600000", "SZ.000001"), prior[0], prior[-1])
    ]


def test_closed_bars_normalizes_utc_and_omits_future_unclosed_or_outside_rows():
    exchange = _FakeExchange()
    days = _calendar_days()
    exchange.frames[("SH.600000", "5m")] = _frame(
        _at(9, 55),
        _at(10, 0),
        _at(10, 5),
    )
    adapter = QmtASectorMarketData(exchange, trading_calendar=_Calendar(days))

    result = adapter.closed_bars(
        ("SH.600000",),
        "5m",
        start=_at(9, 56).astimezone(timezone.utc),
        end=_at(10, 0).astimezone(timezone.utc),
        as_of=_at(10, 0, 30).astimezone(timezone.utc),
    )

    assert tuple(bar.physical_close for bar in result["SH.600000"]) == (_at(10, 0),)
    assert exchange.kline_calls[0][0:2] == ("SH.600000", "5m")
    assert exchange.kline_calls[0][3] == "2026-07-17 10:00:00"
    assert exchange.kline_calls[0][4] == {"research_exact_end": True}


def test_closed_bars_fails_only_the_malformed_code_on_boolean_native_value():
    exchange = _FakeExchange()
    malformed = _frame(_at(10, 1))
    malformed.loc[0, "open"] = True
    exchange.frames[("SH.600000", "1m")] = malformed
    exchange.frames[("SZ.000001", "1m")] = _frame(_at(10, 1)).assign(
        code="SZ.000001"
    )
    adapter = QmtASectorMarketData(
        exchange,
        trading_calendar=_Calendar(_calendar_days()),
    )

    result = adapter.closed_bars(
        ("SH.600000", "SZ.000001"),
        "1m",
        start=_at(10, 0),
        end=_at(10, 1),
        as_of=_at(10, 1, 30),
    )

    assert result["SH.600000"] == ()
    assert len(result["SZ.000001"]) == 1


def test_closed_bars_fails_closed_when_physical_open_underflows_datetime():
    exchange = _FakeExchange()
    start = datetime(1, 1, 1, 0, 0, tzinfo=CN)
    close = datetime(1, 1, 1, 0, 0, 30, tzinfo=CN)
    exchange.frames[("SH.600000", "1m")] = _frame(close)
    adapter = QmtASectorMarketData(
        exchange,
        trading_calendar=_Calendar(_calendar_days()),
    )

    result = adapter.closed_bars(
        ("SH.600000",),
        "1m",
        start=start,
        end=close,
        as_of=close,
    )

    assert result == {"SH.600000": ()}


def test_closed_bars_fails_closed_when_timestamp_normalization_underflows():
    exchange = _FakeExchange()
    start = datetime(1, 1, 1, 0, 0, tzinfo=CN)
    end = datetime(1, 1, 1, 0, 0, 30, tzinfo=CN)
    hostile_timestamp = datetime(
        1,
        1,
        1,
        0,
        0,
        tzinfo=timezone(timedelta(hours=14)),
    )
    exchange.frames[("SH.600000", "1m")] = _frame(hostile_timestamp)
    adapter = QmtASectorMarketData(
        exchange,
        trading_calendar=_Calendar(_calendar_days()),
    )

    result = adapter.closed_bars(
        ("SH.600000",),
        "1m",
        start=start,
        end=end,
        as_of=end,
    )

    assert result == {"SH.600000": ()}


def test_qmt_market_data_uses_only_explicit_times_and_injected_calendar(
    monkeypatch,
):
    exchange = _FakeExchange()
    days = _calendar_days()
    prior = days[-21:-1]
    exchange.frames[("SH.600000", "1m")] = _frame(_at(10, 1), _at(10, 2))
    exchange.daily["SH.600000"] = tuple(
        _daily_row("SH.600000", session, index + 1)
        for index, session in enumerate(prior)
    )
    exchange.ticks["SH.600000"] = QmtResearchTick(
        code="SH.600000",
        native_time_ms=_native_ms(_at(10, 1)),
        last_price=Decimal("10"),
        last_close=Decimal("9"),
        volume=Decimal("100"),
        stock_status=3,
    )
    adapter = QmtASectorMarketData(
        exchange,
        trading_calendar=_Calendar(days),
    )

    class _NoAmbientNow(datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("market data adapter must not read ambient now")

    monkeypatch.setattr(market_data_module.dt, "datetime", _NoAmbientNow)

    bars = adapter.closed_bars(
        ("SH.600000",),
        "1m",
        start=_at(10, 0),
        end=_at(10, 1),
        as_of=_at(10, 1, 30),
    )
    amounts = adapter.prior_complete_daily_amounts(
        ("SH.600000",),
        before_session=days[-1],
    )
    ticks = adapter.capture_ticks(("SH.600000",), as_of=_at(10, 1, 30))

    assert tuple(bar.physical_close for bar in bars["SH.600000"]) == (_at(10, 1),)
    assert len(amounts["SH.600000"]) == 20
    assert ticks["SH.600000"].batch_captured_at == _at(10, 1, 30)


@pytest.mark.parametrize(
    ("label", "message"),
    (("start", "end-labeled"), ("close", "end-labeled")),
)
def test_qmt_market_data_requires_explicit_end_labeled_reader(label, message):
    exchange = _FakeExchange()
    exchange.kline_time_label = label

    with pytest.raises(ValueError, match=message):
        QmtASectorMarketData(
            exchange,
            trading_calendar=_Calendar(_calendar_days()),
        )


def test_prior_daily_requires_exact_twenty_audited_sessions_without_short_fallback():
    exchange = _FakeExchange()
    days = _calendar_days()
    prior = days[-21:-1]
    exchange.daily["SH.600000"] = tuple(
        _daily_row("SH.600000", session, index + 1)
        for index, session in enumerate(prior[:-1])
    )
    adapter = QmtASectorMarketData(exchange, trading_calendar=_Calendar(days))

    result = adapter.prior_complete_daily_amounts(
        ("SH.600000",),
        before_session=days[-1],
    )

    assert result == {"SH.600000": ()}
    with pytest.raises(ValueError, match="exactly 20"):
        adapter.prior_complete_daily_amounts(
            ("SH.600000",),
            before_session=days[-1],
            trading_days=19,
        )


def test_prior_daily_audits_the_oldest_of_all_twenty_returned_sessions():
    exchange = _FakeExchange()
    days = _calendar_days()
    prior = days[-21:-1]
    exchange.daily["SH.600000"] = tuple(
        _daily_row("SH.600000", session, index + 1)
        for index, session in enumerate(prior)
    )

    class _OldestUnavailableCalendar(_Calendar):
        def __init__(self, values, unavailable):
            super().__init__(values)
            self.unavailable = unavailable
            self.calls: list[date] = []

        def session_for(self, trading_day):
            self.calls.append(trading_day)
            if trading_day == self.unavailable:
                return None
            return super().session_for(trading_day)

    calendar = _OldestUnavailableCalendar(days, prior[0])
    adapter = QmtASectorMarketData(exchange, trading_calendar=calendar)

    with pytest.raises(ValueError, match="20 prior sessions"):
        adapter.prior_complete_daily_amounts(
            ("SH.600000",),
            before_session=days[-1],
        )

    assert prior[0] in calendar.calls


def test_prior_daily_rejects_current_session_and_mismatched_calendar_sequence():
    exchange = _FakeExchange()
    days = _calendar_days()
    prior = days[-21:-1]
    rows = [
        _daily_row("SH.600000", session, index + 1)
        for index, session in enumerate(prior)
    ]
    rows[-1] = _daily_row("SH.600000", days[-1], 20)
    exchange.daily["SH.600000"] = tuple(rows)
    adapter = QmtASectorMarketData(exchange, trading_calendar=_Calendar(days))

    assert adapter.prior_complete_daily_amounts(
        ("SH.600000",),
        before_session=days[-1],
    ) == {"SH.600000": ()}


def test_capture_ticks_preserves_future_native_time_as_unusable_not_poll_time():
    exchange = _FakeExchange()
    days = _calendar_days()
    as_of = _at(10, 1, 30)
    exchange.ticks["SH.600000"] = QmtResearchTick(
        code="SH.600000",
        native_time_ms=_native_ms(as_of + timedelta(milliseconds=1)),
        last_price=Decimal("10"),
        last_close=Decimal("9"),
        volume=Decimal("100"),
        stock_status=3,
    )
    adapter = QmtASectorMarketData(exchange, trading_calendar=_Calendar(days))

    snapshot = adapter.capture_ticks(("SH.600000",), as_of=as_of)["SH.600000"]

    assert snapshot.quote_timestamp > snapshot.batch_captured_at
    assert snapshot.tradable is True
    assert snapshot.usable_for_breadth is False


def test_capture_ticks_omits_unrepresentable_native_time_without_crashing():
    exchange = _FakeExchange()
    exchange.ticks["SH.600000"] = QmtResearchTick(
        code="SH.600000",
        native_time_ms=10**30,
        last_price=Decimal("10"),
        last_close=Decimal("9"),
        volume=Decimal("100"),
        stock_status=3,
    )
    adapter = QmtASectorMarketData(
        exchange,
        trading_calendar=_Calendar(_calendar_days()),
    )

    assert adapter.capture_ticks(
        ("SH.600000",),
        as_of=_at(10, 1, 30),
    ) == {}


@pytest.mark.parametrize(
    ("last_price", "last_close", "volume", "status", "tradable"),
    (
        ("10", "9", "100", 3, True),
        ("0", "9", "100", 3, False),
        ("10", "0", "100", 3, False),
        ("10", "9", "0", 3, False),
        ("10", "9", "100", 7, False),
    ),
)
def test_capture_ticks_derives_tradable_only_from_native_status_price_and_volume(
    last_price,
    last_close,
    volume,
    status,
    tradable,
):
    exchange = _FakeExchange()
    days = _calendar_days()
    as_of = _at(10, 1, 30)
    exchange.ticks["SH.600000"] = QmtResearchTick(
        code="SH.600000",
        native_time_ms=_native_ms(as_of),
        last_price=Decimal(last_price),
        last_close=Decimal(last_close),
        volume=Decimal(volume),
        stock_status=status,
    )
    adapter = QmtASectorMarketData(exchange, trading_calendar=_Calendar(days))

    snapshot = adapter.capture_ticks(("SH.600000",), as_of=as_of)["SH.600000"]

    assert snapshot.tradable is tradable
    assert snapshot.usable_for_breadth is tradable


def test_tick_snapshot_source_fingerprint_binds_native_last_close_and_status():
    exchange = _FakeExchange()
    days = _calendar_days()
    as_of = _at(10, 1, 30)
    adapter = QmtASectorMarketData(exchange, trading_calendar=_Calendar(days))
    first = QmtResearchTick(
        code="SH.600000",
        native_time_ms=_native_ms(as_of),
        last_price=Decimal("10"),
        last_close=Decimal("9"),
        volume=Decimal("100"),
        stock_status=3,
    )
    second = QmtResearchTick(
        code="SH.600000",
        native_time_ms=_native_ms(as_of),
        last_price=Decimal("10"),
        last_close=Decimal("8.99"),
        volume=Decimal("100"),
        stock_status=7,
    )

    exchange.ticks[first.code] = first
    one = adapter.capture_ticks((first.code,), as_of=as_of)[first.code]
    exchange.ticks[second.code] = second
    two = adapter.capture_ticks((second.code,), as_of=as_of)[second.code]

    assert one.previous_close == Decimal("9")
    assert two.previous_close == Decimal("8.99")
    assert one.source_fingerprint != two.source_fingerprint
    assert type(one) is TickSnapshot


@pytest.mark.parametrize(
    "invalid",
    (
        None,
        ["SH.600000"],
        ("SH.600000", "SH.600000"),
        ("600000.SH",),
    ),
)
def test_market_data_rejects_invalid_code_batches_before_source_calls(invalid):
    exchange = _FakeExchange()
    adapter = QmtASectorMarketData(
        exchange,
        trading_calendar=_Calendar(_calendar_days()),
    )

    with pytest.raises((TypeError, ValueError), match="codes"):
        adapter.capture_ticks(invalid, as_of=_at(10, 1))

    assert exchange.kline_calls == []
