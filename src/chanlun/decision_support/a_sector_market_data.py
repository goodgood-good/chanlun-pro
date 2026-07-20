from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.exchange.exchange_qmt import QmtDailyAmount, QmtResearchTick


ASectorFrequency = Literal["1m", "5m", "30m"]

_CODE_PATTERN = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
_UTC = dt.timezone.utc
_UNIX_EPOCH_UTC = dt.datetime(1970, 1, 1, tzinfo=_UTC)
_FREQUENCY_DURATION = {
    "1m": dt.timedelta(minutes=1),
    "5m": dt.timedelta(minutes=5),
    "30m": dt.timedelta(minutes=30),
}


def _validated_codes(codes: object) -> tuple[str, ...]:
    if type(codes) is not tuple:
        raise TypeError("codes must be an exact tuple")
    if any(
        type(code) is not str or _CODE_PATTERN.fullmatch(code) is None
        for code in codes
    ):
        raise ValueError("codes must contain exact normalized A-share codes")
    if len(set(codes)) != len(codes):
        raise ValueError("codes must not contain duplicates")
    return codes


def _validated_decimal(value: object, field_name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    return value


def _validated_fingerprint(value: object, field_name: str) -> str:
    if type(value) is not str or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
    return value


def _native_ms_from_datetime(value: dt.datetime) -> int:
    normalized = normalize_datetime(value, "quote_timestamp")
    utc_value = normalized.astimezone(_UTC)
    if utc_value.microsecond % 1_000 != 0:
        raise ValueError("quote_timestamp must use native millisecond precision")
    elapsed = utc_value - _UNIX_EPOCH_UTC
    return (
        elapsed.days * 86_400_000
        + elapsed.seconds * 1_000
        + elapsed.microseconds // 1_000
    )


def _datetime_from_native_ms(native_time_ms: int) -> dt.datetime:
    if (
        type(native_time_ms) is not int
        or isinstance(native_time_ms, bool)
        or native_time_ms < 0
    ):
        raise ValueError("native_time_ms must be an exact non-negative int")
    return (
        _UNIX_EPOCH_UTC + dt.timedelta(milliseconds=native_time_ms)
    ).astimezone(_MARKET_TIMEZONE)


def physical_bar_source_fingerprint(
    *,
    code: str,
    frequency: ASectorFrequency,
    source_timestamp: dt.datetime,
    physical_open: dt.datetime,
    physical_close: dt.datetime,
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
) -> str:
    return sha256_json(
        {
            "schema": "a-sector-physical-bar-source-v1",
            "code": code,
            "frequency": frequency,
            "source_timestamp": source_timestamp,
            "physical_open": physical_open,
            "physical_close": physical_close,
            "open": open,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def tick_snapshot_source_fingerprint(
    *,
    code: str,
    native_time_ms: int,
    last_price: Decimal,
    previous_close: Decimal,
    session_volume: Decimal,
    stock_status: int,
) -> str:
    return sha256_json(
        {
            "schema": "a-sector-native-tick-source-v1",
            "code": code,
            "native_time_ms": native_time_ms,
            "last_price": last_price,
            "previous_close": previous_close,
            "session_volume": session_volume,
            "stock_status": stock_status,
        }
    )


@dataclass(frozen=True, slots=True)
class PhysicalBar:
    code: str
    frequency: ASectorFrequency
    source_timestamp: dt.datetime
    physical_open: dt.datetime
    physical_close: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_bar_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError("code must be an exact normalized A-share code")
        if self.frequency not in _FREQUENCY_DURATION:
            raise ValueError("frequency must be one of 1m, 5m, or 30m")
        source_timestamp = normalize_datetime(
            self.source_timestamp, "source_timestamp"
        )
        physical_open = normalize_datetime(self.physical_open, "physical_open")
        physical_close = normalize_datetime(self.physical_close, "physical_close")
        object.__setattr__(self, "source_timestamp", source_timestamp)
        object.__setattr__(self, "physical_open", physical_open)
        object.__setattr__(self, "physical_close", physical_close)
        if physical_close - physical_open != _FREQUENCY_DURATION[self.frequency]:
            raise ValueError("physical bar duration must exactly match frequency")
        if source_timestamp != physical_close:
            raise ValueError("source_timestamp must equal physical_close")
        for field_name in ("open", "high", "low", "close", "volume"):
            _validated_decimal(getattr(self, field_name), field_name)
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        _validated_fingerprint(
            self.source_bar_fingerprint, "source_bar_fingerprint"
        )
        expected = physical_bar_source_fingerprint(
            code=self.code,
            frequency=self.frequency,
            source_timestamp=source_timestamp,
            physical_open=physical_open,
            physical_close=physical_close,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )
        if self.source_bar_fingerprint != expected:
            raise ValueError("source_bar_fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class TickSnapshot:
    code: str
    quote_timestamp: dt.datetime
    batch_captured_at: dt.datetime
    last_price: Decimal
    previous_close: Decimal
    session_volume: Decimal
    stock_status: int
    tradable: bool
    source_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError("code must be an exact normalized A-share code")
        quote_timestamp = normalize_datetime(
            self.quote_timestamp, "quote_timestamp"
        )
        batch_captured_at = normalize_datetime(
            self.batch_captured_at, "batch_captured_at"
        )
        object.__setattr__(self, "quote_timestamp", quote_timestamp)
        object.__setattr__(self, "batch_captured_at", batch_captured_at)
        for field_name in ("last_price", "previous_close", "session_volume"):
            _validated_decimal(getattr(self, field_name), field_name)
        if type(self.stock_status) is not int or isinstance(self.stock_status, bool):
            raise ValueError("stock_status must be an exact int")
        if type(self.tradable) is not bool:
            raise ValueError("tradable must be an exact bool")
        expected_tradable = (
            self.stock_status == 3
            and self.last_price > 0
            and self.previous_close > 0
            and self.session_volume > 0
        )
        if self.tradable is not expected_tradable:
            raise ValueError("tradable must match exact native tick invariants")
        _validated_fingerprint(self.source_fingerprint, "source_fingerprint")
        native_time_ms = _native_ms_from_datetime(quote_timestamp)
        expected_fingerprint = tick_snapshot_source_fingerprint(
            code=self.code,
            native_time_ms=native_time_ms,
            last_price=self.last_price,
            previous_close=self.previous_close,
            session_volume=self.session_volume,
            stock_status=self.stock_status,
        )
        if self.source_fingerprint != expected_fingerprint:
            raise ValueError("source_fingerprint mismatch")

    @property
    def usable_for_breadth(self) -> bool:
        return (
            self.tradable
            and self.quote_timestamp <= self.batch_captured_at
            and self.batch_captured_at - self.quote_timestamp
            <= dt.timedelta(seconds=90)
        )


class ASectorMarketData(Protocol):
    def closed_bars(
        self,
        codes: tuple[str, ...],
        frequency: ASectorFrequency,
        *,
        start: dt.datetime,
        end: dt.datetime,
        as_of: dt.datetime,
    ) -> Mapping[str, tuple[PhysicalBar, ...]]: ...

    def prior_complete_daily_amounts(
        self,
        codes: tuple[str, ...],
        *,
        before_session: dt.date,
        trading_days: int = 20,
    ) -> Mapping[str, tuple[Decimal, ...]]: ...

    def capture_ticks(
        self,
        codes: tuple[str, ...],
        *,
        as_of: dt.datetime,
    ) -> Mapping[str, TickSnapshot]: ...


class QmtASectorMarketData:
    def __init__(self, exchange: object, *, trading_calendar: object) -> None:
        if getattr(exchange, "kline_time_label", None) != "end":
            raise ValueError("QMT historical reader must be explicitly end-labeled")
        if not callable(getattr(trading_calendar, "session_for", None)):
            raise TypeError("trading_calendar must expose session_for")
        self._exchange = exchange
        self._trading_calendar = trading_calendar

    def closed_bars(
        self,
        codes: tuple[str, ...],
        frequency: ASectorFrequency,
        *,
        start: dt.datetime,
        end: dt.datetime,
        as_of: dt.datetime,
    ) -> Mapping[str, tuple[PhysicalBar, ...]]:
        normalized_codes = _validated_codes(codes)
        if frequency not in _FREQUENCY_DURATION:
            raise ValueError("frequency must be one of 1m, 5m, or 30m")
        normalized_start = normalize_datetime(start, "start")
        normalized_end = normalize_datetime(end, "end")
        normalized_as_of = normalize_datetime(as_of, "as_of")
        if normalized_start > normalized_end:
            raise ValueError("start must not be after end")
        if normalized_end > normalized_as_of:
            raise ValueError("end must not be after as_of")

        result: dict[str, tuple[PhysicalBar, ...]] = {}
        duration = _FREQUENCY_DURATION[frequency]
        start_text = normalized_start.strftime("%Y-%m-%d %H:%M:%S")
        end_text = normalized_end.strftime("%Y-%m-%d %H:%M:%S")
        for code in normalized_codes:
            frame = self._exchange.klines(
                code,
                frequency,
                start_date=start_text,
                end_date=end_text,
                args={"research_exact_end": True},
            )
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                result[code] = ()
                continue
            bars: list[PhysicalBar] = []
            previous_close: dt.datetime | None = None
            invalid = False
            for _, native_row in frame.iterrows():
                try:
                    native_timestamp = native_row["date"]
                    if isinstance(native_timestamp, pd.Timestamp):
                        native_timestamp = native_timestamp.to_pydatetime()
                    physical_close = normalize_datetime(
                        native_timestamp, "source_timestamp"
                    )
                except (KeyError, OverflowError, TypeError, ValueError):
                    invalid = True
                    break
                if not (
                    normalized_start < physical_close <= normalized_end
                    and physical_close <= normalized_as_of
                ):
                    continue
                native_code = native_row.get("code", code)
                if native_code != code:
                    invalid = True
                    break
                try:
                    values: dict[str, Decimal] = {}
                    for field_name in ("open", "high", "low", "close", "volume"):
                        native_value = native_row[field_name]
                        if isinstance(native_value, bool):
                            raise ValueError("bar values cannot be boolean")
                        values[field_name] = Decimal(str(native_value))
                    if any(not value.is_finite() for value in values.values()):
                        raise ValueError("bar values must be finite")
                    physical_open = physical_close - duration
                    facts = {
                        "code": code,
                        "frequency": frequency,
                        "source_timestamp": physical_close,
                        "physical_open": physical_open,
                        "physical_close": physical_close,
                        **values,
                    }
                    bar = PhysicalBar(
                        **facts,
                        source_bar_fingerprint=physical_bar_source_fingerprint(
                            **facts
                        ),
                    )
                except (
                    InvalidOperation,
                    KeyError,
                    OverflowError,
                    TypeError,
                    ValueError,
                ):
                    invalid = True
                    break
                if previous_close is not None and physical_close <= previous_close:
                    invalid = True
                    break
                bars.append(bar)
                previous_close = physical_close
            result[code] = () if invalid else tuple(bars)
        return result

    def prior_complete_daily_amounts(
        self,
        codes: tuple[str, ...],
        *,
        before_session: dt.date,
        trading_days: int = 20,
    ) -> Mapping[str, tuple[Decimal, ...]]:
        normalized_codes = _validated_codes(codes)
        if type(before_session) is not dt.date:
            raise TypeError("before_session must be an exact date")
        if type(trading_days) is not int or isinstance(trading_days, bool) or trading_days != 20:
            raise ValueError("trading_days must be exactly 20")
        if not normalized_codes:
            return {}

        expected_descending: list[dt.date] = []
        cursor = before_session
        for _ in range(trading_days):
            session = self._trading_calendar.session_for(cursor)
            if session is None or getattr(session, "trading_day", None) != cursor:
                raise ValueError("before_session must be an audited trading session")
            previous = getattr(session, "previous_trading_day", None)
            if type(previous) is not dt.date:
                raise ValueError("calendar does not provide exactly 20 prior sessions")
            expected_descending.append(previous)
            cursor = previous
        oldest = self._trading_calendar.session_for(expected_descending[-1])
        if (
            oldest is None
            or getattr(oldest, "trading_day", None) != expected_descending[-1]
        ):
            raise ValueError("calendar does not provide exactly 20 prior sessions")
        expected_sessions = tuple(reversed(expected_descending))
        native_result = self._exchange.research_daily_amounts(
            normalized_codes,
            start_session=expected_sessions[0],
            end_session=expected_sessions[-1],
        )
        if not isinstance(native_result, Mapping):
            return {code: () for code in normalized_codes}
        unknown_codes = [code for code in native_result if code not in normalized_codes]
        if unknown_codes:
            raise RuntimeError(f"daily source returned unknown code: {unknown_codes[0]!r}")

        result: dict[str, tuple[Decimal, ...]] = {}
        for code in normalized_codes:
            rows = native_result.get(code, ())
            if type(rows) is not tuple or len(rows) != trading_days:
                result[code] = ()
                continue
            valid = all(
                type(row) is QmtDailyAmount
                and row.code == code
                and row.session == expected_session
                and type(row.amount) is Decimal
                and row.amount.is_finite()
                and row.amount > 0
                for row, expected_session in zip(rows, expected_sessions)
            )
            result[code] = tuple(row.amount for row in rows) if valid else ()
        return result

    def capture_ticks(
        self,
        codes: tuple[str, ...],
        *,
        as_of: dt.datetime,
    ) -> Mapping[str, TickSnapshot]:
        normalized_codes = _validated_codes(codes)
        normalized_as_of = normalize_datetime(as_of, "as_of")
        if not normalized_codes:
            return {}
        native_result = self._exchange.research_tick_snapshots(normalized_codes)
        if not isinstance(native_result, Mapping):
            return {}
        unknown_codes = [code for code in native_result if code not in normalized_codes]
        if unknown_codes:
            raise RuntimeError(f"tick source returned unknown code: {unknown_codes[0]!r}")

        result: dict[str, TickSnapshot] = {}
        for code in normalized_codes:
            native_tick = native_result.get(code)
            if type(native_tick) is not QmtResearchTick or native_tick.code != code:
                continue
            try:
                quote_timestamp = _datetime_from_native_ms(
                    native_tick.native_time_ms
                )
            except (OverflowError, ValueError):
                continue
            tradable = (
                native_tick.stock_status == 3
                and native_tick.last_price > 0
                and native_tick.last_close > 0
                and native_tick.volume > 0
            )
            source_fingerprint = tick_snapshot_source_fingerprint(
                code=code,
                native_time_ms=native_tick.native_time_ms,
                last_price=native_tick.last_price,
                previous_close=native_tick.last_close,
                session_volume=native_tick.volume,
                stock_status=native_tick.stock_status,
            )
            result[code] = TickSnapshot(
                code=code,
                quote_timestamp=quote_timestamp,
                batch_captured_at=normalized_as_of,
                last_price=native_tick.last_price,
                previous_close=native_tick.last_close,
                session_volume=native_tick.volume,
                stock_status=native_tick.stock_status,
                tradable=tradable,
                source_fingerprint=source_fingerprint,
            )
        return result
