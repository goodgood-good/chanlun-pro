"""Web 与原生工作进程共享的 A 股实时行情值对象。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
import math
import re

from chanlun.exchange.exchange import Tick


_A_STOCK_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")


def normalized_a_share_codes(codes: object) -> tuple[str, ...]:
    """返回严格、去重并排序的 A 股代码。"""

    if isinstance(codes, (str, bytes)) or not isinstance(codes, Sequence):
        raise TypeError("A-share quote codes must be a sequence")
    values = tuple(codes)
    if any(
        type(code) is not str or _A_STOCK_CODE.fullmatch(code) is None
        for code in values
    ):
        raise ValueError("A-share quote codes must be normalized")
    return tuple(sorted(set(values)))


@dataclass(frozen=True, slots=True)
class AShareRealtimeQuote:
    """不携带任何 QMT 原生对象的单标的实时行情。"""

    code: str
    last: float
    buy1: float
    sell1: float
    high: float
    low: float
    open: float
    volume: float
    rate: float

    def __post_init__(self) -> None:
        if type(self.code) is not str or _A_STOCK_CODE.fullmatch(self.code) is None:
            raise ValueError("realtime quote code must be normalized")
        values = (
            self.last,
            self.buy1,
            self.sell1,
            self.high,
            self.low,
            self.open,
            self.volume,
            self.rate,
        )
        if any(
            type(value) is not float or not math.isfinite(value) for value in values
        ):
            raise ValueError("realtime quote values must be finite floats")
        if self.last <= 0 or self.volume < 0:
            raise ValueError("realtime quote price and volume are invalid")

    def to_tick(self) -> Tick:
        return Tick(
            code=self.code,
            last=self.last,
            buy1=self.buy1,
            sell1=self.sell1,
            high=self.high,
            low=self.low,
            open=self.open,
            volume=self.volume,
            rate=self.rate,
        )


@dataclass(frozen=True, slots=True)
class AShareRealtimeQuoteBatch:
    """一次已隔离 A 股实时行情调用的完整结果。"""

    requested_codes: tuple[str, ...]
    market_open: bool
    quotes: tuple[AShareRealtimeQuote, ...]
    tick_data_used: bool
    schema: str = "chanlun-native-a-share-realtime-quotes"
    real_account_access: bool = False
    real_order_transport: bool = False

    def __post_init__(self) -> None:
        if normalized_a_share_codes(self.requested_codes) != self.requested_codes:
            raise ValueError("requested quote codes must be unique and sorted")
        if type(self.market_open) is not bool or type(self.tick_data_used) is not bool:
            raise TypeError("quote batch flags must be booleans")
        if self.schema != "chanlun-native-a-share-realtime-quotes":
            raise ValueError("unsupported quote batch schema")
        if (
            self.real_account_access is not False
            or self.real_order_transport is not False
        ):
            raise ValueError("quote batch crossed the read-only boundary")
        quote_codes = tuple(quote.code for quote in self.quotes)
        if quote_codes != tuple(sorted(set(quote_codes))):
            raise ValueError("quote rows must be unique and sorted")
        if not set(quote_codes).issubset(self.requested_codes):
            raise ValueError("quote batch returned an unrequested code")
        expected_tick_use = bool(self.market_open and self.requested_codes)
        if self.tick_data_used is not expected_tick_use:
            raise ValueError("quote batch tick-use flag is inconsistent")
        if not self.market_open and self.quotes:
            raise ValueError("closed-market quote batch must be empty")

    def ticks(self) -> dict[str, Tick]:
        return {quote.code: quote.to_tick() for quote in self.quotes}


@dataclass(frozen=True, slots=True)
class AShareInstrumentSessionStatus:
    """One same-session QMT instrument-status fact."""

    code: str
    trading_day: date
    instrument_name: str
    instrument_status: int
    is_trading: bool

    def __post_init__(self) -> None:
        if type(self.code) is not str or _A_STOCK_CODE.fullmatch(self.code) is None:
            raise ValueError("instrument-status code must be normalized")
        if type(self.trading_day) is not date:
            raise TypeError("instrument-status trading_day must be an exact date")
        if not isinstance(self.instrument_name, str) or not self.instrument_name.strip():
            raise ValueError("instrument-status name must be non-empty")
        if type(self.instrument_status) is not int or self.instrument_status < 0:
            raise ValueError("instrument status must be a non-negative int")
        if type(self.is_trading) is not bool:
            raise TypeError("instrument is_trading must be a bool")

    @property
    def suspended(self) -> bool:
        return self.instrument_status >= 1


@dataclass(frozen=True, slots=True)
class AShareInstrumentSessionStatusBatch:
    """Read-only, same-session QMT status facts for a requested symbol set."""

    requested_codes: tuple[str, ...]
    session: date
    facts: tuple[AShareInstrumentSessionStatus, ...]
    schema: str = "chanlun-native-a-share-instrument-session-status"
    real_account_access: bool = False
    real_order_transport: bool = False

    def __post_init__(self) -> None:
        if normalized_a_share_codes(self.requested_codes) != self.requested_codes:
            raise ValueError("requested status codes must be unique and sorted")
        if type(self.session) is not date:
            raise TypeError("instrument-status session must be an exact date")
        if self.schema != "chanlun-native-a-share-instrument-session-status":
            raise ValueError("unsupported instrument-status batch schema")
        if (
            self.real_account_access is not False
            or self.real_order_transport is not False
        ):
            raise ValueError("instrument-status batch crossed the read-only boundary")
        fact_codes = tuple(fact.code for fact in self.facts)
        if fact_codes != tuple(sorted(set(fact_codes))):
            raise ValueError("instrument-status facts must be unique and sorted")
        if not set(fact_codes).issubset(self.requested_codes):
            raise ValueError("instrument-status batch returned an unrequested code")
        if any(fact.trading_day != self.session for fact in self.facts):
            raise ValueError("instrument-status fact belongs to another session")

    def statuses(self) -> dict[str, AShareInstrumentSessionStatus]:
        return {fact.code: fact for fact in self.facts}


@dataclass(frozen=True, slots=True)
class AShareDisplayQuoteBatch:
    """页面行情展示用快照；休市时也允许携带最近一笔有效报价。"""

    requested_codes: tuple[str, ...]
    market_open: bool
    quotes: tuple[AShareRealtimeQuote, ...]
    tick_data_used: bool
    schema: str = "chanlun-native-a-share-display-quotes"
    real_account_access: bool = False
    real_order_transport: bool = False

    def __post_init__(self) -> None:
        if normalized_a_share_codes(self.requested_codes) != self.requested_codes:
            raise ValueError("requested display quote codes must be unique and sorted")
        if type(self.market_open) is not bool or type(self.tick_data_used) is not bool:
            raise TypeError("display quote batch flags must be booleans")
        if self.schema != "chanlun-native-a-share-display-quotes":
            raise ValueError("unsupported display quote batch schema")
        if (
            self.real_account_access is not False
            or self.real_order_transport is not False
        ):
            raise ValueError("display quote batch crossed the read-only boundary")
        quote_codes = tuple(quote.code for quote in self.quotes)
        if quote_codes != tuple(sorted(set(quote_codes))):
            raise ValueError("display quote rows must be unique and sorted")
        if not set(quote_codes).issubset(self.requested_codes):
            raise ValueError("display quote batch returned an unrequested code")
        if self.tick_data_used is not bool(self.requested_codes):
            raise ValueError("display quote tick-use flag is inconsistent")

    def ticks(self) -> dict[str, Tick]:
        return {quote.code: quote.to_tick() for quote in self.quotes}


def quote_from_exchange_tick(code: str, tick: object) -> AShareRealtimeQuote | None:
    """把交易所 Tick 转成可安全跨进程传输的有限浮点值。"""

    def number(name: str, *, default: float | None = None) -> float | None:
        raw = getattr(tick, name, default)
        if raw is None:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None

    last = number("last")
    if last is None or last <= 0:
        return None
    values = {
        "buy1": number("buy1", default=0.0),
        "sell1": number("sell1", default=0.0),
        "high": number("high", default=last),
        "low": number("low", default=last),
        "open": number("open", default=last),
        "volume": number("volume", default=0.0),
        "rate": number("rate", default=0.0),
    }
    if any(value is None for value in values.values()) or float(values["volume"]) < 0:
        return None
    return AShareRealtimeQuote(
        code=code,
        last=float(last),
        **{name: float(value) for name, value in values.items()},
    )


def validated_quote_batch(
    value: object,
    *,
    requested_codes: tuple[str, ...],
) -> AShareRealtimeQuoteBatch:
    """重建并校验来自认证子进程的行情结果。"""

    if not isinstance(value, AShareRealtimeQuoteBatch):
        raise TypeError("native quote result must be an AShareRealtimeQuoteBatch")
    quotes = tuple(
        AShareRealtimeQuote(
            code=quote.code,
            last=float(quote.last),
            buy1=float(quote.buy1),
            sell1=float(quote.sell1),
            high=float(quote.high),
            low=float(quote.low),
            open=float(quote.open),
            volume=float(quote.volume),
            rate=float(quote.rate),
        )
        for quote in value.quotes
        if isinstance(quote, AShareRealtimeQuote)
    )
    if len(quotes) != len(value.quotes):
        raise TypeError("native quote result contains an invalid row")
    rebuilt = AShareRealtimeQuoteBatch(
        requested_codes=tuple(value.requested_codes),
        market_open=value.market_open,
        quotes=quotes,
        tick_data_used=value.tick_data_used,
        schema=value.schema,
        real_account_access=value.real_account_access,
        real_order_transport=value.real_order_transport,
    )
    if rebuilt.requested_codes != requested_codes:
        raise ValueError("native quote result identity is inconsistent")
    return rebuilt


def validated_instrument_session_status_batch(
    value: object,
    *,
    requested_codes: tuple[str, ...],
    session: date,
) -> AShareInstrumentSessionStatusBatch:
    """Rebuild and validate same-session status evidence from a native worker."""

    if not isinstance(value, AShareInstrumentSessionStatusBatch):
        raise TypeError(
            "native status result must be an AShareInstrumentSessionStatusBatch"
        )
    facts = tuple(
        AShareInstrumentSessionStatus(
            code=fact.code,
            trading_day=fact.trading_day,
            instrument_name=fact.instrument_name,
            instrument_status=fact.instrument_status,
            is_trading=fact.is_trading,
        )
        for fact in value.facts
        if isinstance(fact, AShareInstrumentSessionStatus)
    )
    if len(facts) != len(value.facts):
        raise TypeError("native status result contains an invalid fact")
    rebuilt = AShareInstrumentSessionStatusBatch(
        requested_codes=tuple(value.requested_codes),
        session=value.session,
        facts=facts,
        schema=value.schema,
        real_account_access=value.real_account_access,
        real_order_transport=value.real_order_transport,
    )
    if rebuilt.requested_codes != requested_codes or rebuilt.session != session:
        raise ValueError("native status result identity is inconsistent")
    return rebuilt


def validated_display_quote_batch(
    value: object,
    *,
    requested_codes: tuple[str, ...],
) -> AShareDisplayQuoteBatch:
    """重建并校验来自认证子进程的页面展示行情快照。"""

    if not isinstance(value, AShareDisplayQuoteBatch):
        raise TypeError("native display quote result must be an AShareDisplayQuoteBatch")
    quotes = tuple(
        AShareRealtimeQuote(
            code=quote.code,
            last=float(quote.last),
            buy1=float(quote.buy1),
            sell1=float(quote.sell1),
            high=float(quote.high),
            low=float(quote.low),
            open=float(quote.open),
            volume=float(quote.volume),
            rate=float(quote.rate),
        )
        for quote in value.quotes
        if isinstance(quote, AShareRealtimeQuote)
    )
    if len(quotes) != len(value.quotes):
        raise TypeError("native display quote result contains an invalid row")
    rebuilt = AShareDisplayQuoteBatch(
        requested_codes=tuple(value.requested_codes),
        market_open=value.market_open,
        quotes=quotes,
        tick_data_used=value.tick_data_used,
        schema=value.schema,
        real_account_access=value.real_account_access,
        real_order_transport=value.real_order_transport,
    )
    if rebuilt.requested_codes != requested_codes:
        raise ValueError("native display quote result identity is inconsistent")
    return rebuilt


def isolated_a_share_quote_batch(
    app: object, codes: object
) -> AShareDisplayQuoteBatch | AShareRealtimeQuoteBatch | None:
    """正式隔离模式读取统一行情提供器；非隔离模式由测试或显式适配器处理。"""

    config = getattr(app, "config", {})
    if not bool(config.get("TRADING_SCREENING_NATIVE_PROCESS_ISOLATION", True)):
        return None
    requested = normalized_a_share_codes(codes)
    extensions = getattr(app, "extensions", {})
    provider = extensions.get("a_share_realtime_quotes")
    if not callable(provider):
        raise RuntimeError("isolated A-share realtime quote provider is unavailable")
    value = provider(requested)
    if isinstance(value, AShareDisplayQuoteBatch):
        return validated_display_quote_batch(value, requested_codes=requested)
    # 保留测试或显式旧适配器的实时批次兼容；生产扩展使用展示快照批次。
    return validated_quote_batch(value, requested_codes=requested)


__all__ = [
    "AShareDisplayQuoteBatch",
    "AShareInstrumentSessionStatus",
    "AShareInstrumentSessionStatusBatch",
    "AShareRealtimeQuote",
    "AShareRealtimeQuoteBatch",
    "isolated_a_share_quote_batch",
    "normalized_a_share_codes",
    "quote_from_exchange_tick",
    "validated_display_quote_batch",
    "validated_instrument_session_status_batch",
    "validated_quote_batch",
]
