"""Native market-data adapters for the sole active trading-screening engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import math
import re
from threading import RLock
from typing import Protocol

import pandas as pd

from chanlun.core.cl import CL
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.context import classify_context
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.incremental_scan import BarKey
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    SectorAssessment,
    StructuralPoint,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.provisional import (
    ProvisionalCandidate,
    extract_provisional_candidates,
)
from chanlun.decision_support.trading_system.sector_policy import assess_sector
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)
from chanlun.recursive_bt.engine.engine import CL_CFG


_FREQUENCIES = ("30m", "5m", "1m")
_SECTOR_FREQUENCIES = ("30m", "5m")
_A_STOCK_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")


@dataclass(frozen=True, slots=True)
class FrameStructureAnalysis:
    closed_at: datetime
    direction: ContextDirection
    confirmed_points: tuple[StructuralPoint, ...]
    provisional_points: tuple[ProvisionalCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "closed_at",
            normalize_datetime(self.closed_at, "closed_at"),
        )
        if self.direction not in {"up", "down", "neutral"}:
            raise ValueError("invalid structure direction")


class StructureAnalyzer(Protocol):
    def __call__(
        self,
        *,
        code: str,
        frequency: str,
        frame: pd.DataFrame,
        as_of: datetime,
    ) -> FrameStructureAnalysis: ...


@dataclass(frozen=True, slots=True)
class NativeTradingGatewayConfig:
    request_bars_by_frequency: tuple[tuple[str, int], ...] = (
        ("30m", 240),
        ("5m", 480),
        ("1m", 1200),
    )
    minimum_bars_by_frequency: tuple[tuple[str, int], ...] = (
        ("30m", 40),
        ("5m", 240),
        ("1m", 480),
    )
    minimum_sector_members: int = 8
    current_setup_age_seconds: int = MAX_FIVE_MINUTE_SETUP_AGE_SECONDS

    def __post_init__(self) -> None:
        for field_name in (
            "request_bars_by_frequency",
            "minimum_bars_by_frequency",
        ):
            values = dict(getattr(self, field_name))
            if set(values) != set(_FREQUENCIES):
                raise ValueError(f"{field_name} must define 30m, 5m and 1m")
            if any(type(value) is not int or value <= 0 for value in values.values()):
                raise ValueError(f"{field_name} values must be positive integers")
        requests = dict(self.request_bars_by_frequency)
        minimums = dict(self.minimum_bars_by_frequency)
        if any(minimums[key] > requests[key] for key in _FREQUENCIES):
            raise ValueError("minimum bars cannot exceed requested bars")
        if type(self.minimum_sector_members) is not int or self.minimum_sector_members <= 0:
            raise ValueError("minimum_sector_members must be a positive integer")
        if (
            type(self.current_setup_age_seconds) is not int
            or self.current_setup_age_seconds <= 0
        ):
            raise ValueError("current_setup_age_seconds must be a positive integer")

    def request_bars(self, frequency: str) -> int:
        return dict(self.request_bars_by_frequency)[frequency]

    def minimum_bars(self, frequency: str) -> int:
        return dict(self.minimum_bars_by_frequency)[frequency]


def _market_datetime(value: object, field_name: str) -> datetime:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a datetime") from exc
    if pd.isna(timestamp):
        raise ValueError(f"{field_name} must be a datetime")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("Asia/Shanghai")
    else:
        timestamp = timestamp.tz_convert("Asia/Shanghai")
    return normalize_datetime(timestamp.to_pydatetime(), field_name)


def _closed_frame(
    value: object,
    *,
    not_after: datetime,
    minimum_bars: int,
) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame) or value.empty:
        raise ValueError("kline frame is unavailable")
    required = ("date", "open", "high", "low", "close", "volume")
    if any(column not in value.columns for column in required):
        raise ValueError("kline frame is missing required columns")
    result = value.loc[:, list(required)].copy()
    dates = tuple(_market_datetime(item, "kline.date") for item in result["date"])
    if any(right <= left for left, right in zip(dates, dates[1:])):
        raise ValueError("kline dates must be strictly chronological")
    cutoff = normalize_datetime(not_after, "not_after")
    positions = tuple(index for index, item in enumerate(dates) if item <= cutoff)
    if not positions:
        raise ValueError("kline frame has no closed bars")
    result = result.iloc[list(positions)].copy().reset_index(drop=True)
    result.loc[:, "date"] = [pd.Timestamp(dates[index]) for index in positions]
    numeric_columns = ("open", "high", "low", "close", "volume")
    for column in numeric_columns:
        result.loc[:, column] = pd.to_numeric(result[column], errors="coerce")
    numeric = result.loc[:, list(numeric_columns)].astype(float)
    prices = numeric.loc[:, ["open", "high", "low", "close"]]
    invalid = (
        numeric.isna().any(axis=1)
        | ~numeric.map(math.isfinite).all(axis=1)
        | (prices <= 0).any(axis=1)
        | (numeric["volume"] < 0)
        | (numeric["high"] < prices.max(axis=1))
        | (numeric["low"] > prices.min(axis=1))
    )
    if bool(invalid.any()):
        raise ValueError("kline frame contains invalid market facts")
    if len(result) < minimum_bars:
        raise ValueError("kline frame does not meet minimum history")
    result.loc[:, list(numeric_columns)] = numeric
    return result


def analyze_native_frame(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
) -> FrameStructureAnalysis:
    if frequency not in _FREQUENCIES:
        raise ValueError("unsupported trading frequency")
    closed_at = normalize_datetime(as_of, "as_of")
    cd = CL(code, frequency, dict(CL_CFG), market="a")
    cd.process_klines(frame)
    xds = tuple(cd.get_xds())
    bis = tuple(cd.get_bis())
    current = xds[-1] if xds else bis[-1] if bis else None
    raw_direction = getattr(current, "type", None)
    direction: ContextDirection = (
        raw_direction if raw_direction in {"up", "down"} else "neutral"
    )
    return FrameStructureAnalysis(
        closed_at=closed_at,
        direction=direction,
        confirmed_points=extract_confirmed_points(
            cd,
            code=code,
            source_frequency=frequency,
            as_of=closed_at,
        ),
        provisional_points=extract_provisional_candidates(
            cd,
            code=code,
            source_frequency=frequency,
            as_of=closed_at,
        ),
    )


def _stock_codes(raw: object) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise TypeError("stock scope provider must return a sequence")
    values: set[str] = set()
    for item in raw:
        code = item.get("code") if isinstance(item, Mapping) else item
        if isinstance(code, str) and _A_STOCK_CODE.fullmatch(code):
            values.add(code)
    return tuple(sorted(values))


def _universe_metadata(
    raw: object,
) -> tuple[dict[str, str], dict[str, str]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise TypeError("universe provider must return a sequence")
    result: dict[str, str] = {}
    names: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping) or item.get("type") != "stock_cn":
            continue
        code = item.get("code")
        if isinstance(code, str) and _A_STOCK_CODE.fullmatch(code):
            result.setdefault(code.split(".", 1)[1], code)
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                names.setdefault(code, name.strip())
    return result, names


class NativeTradingDataGateway:
    """Read native TDX sector bars, then build stock 30m/5m/1m structures."""

    def __init__(
        self,
        *,
        exchange_provider: Callable[[], object],
        sector_exchange_provider: Callable[[], object],
        universe_provider: Callable[[object], object],
        sector_provider: Callable[[], object],
        watchlist_provider: Callable[[], object] = lambda: (),
        holdings_provider: Callable[[], object] = lambda: (),
        analyzer: StructureAnalyzer = analyze_native_frame,
        config: NativeTradingGatewayConfig = NativeTradingGatewayConfig(),
    ) -> None:
        providers = (
            exchange_provider,
            sector_exchange_provider,
            universe_provider,
            sector_provider,
            watchlist_provider,
            holdings_provider,
            analyzer,
        )
        if any(not callable(provider) for provider in providers):
            raise TypeError("trading gateway providers must be callable")
        self._exchange_provider = exchange_provider
        self._sector_exchange_provider = sector_exchange_provider
        self._universe_provider = universe_provider
        self._sector_provider = sector_provider
        self._watchlist_provider = watchlist_provider
        self._holdings_provider = holdings_provider
        self._analyzer = analyzer
        self._config = config
        self._lock = RLock()
        self._members: dict[str, tuple[str, ...]] = {}
        self._symbol_names: dict[str, str] = {}
        self._latest_sector_bars: dict[tuple[str, str], datetime] = {}
        self._emitted_sector_bars: dict[tuple[str, str], datetime] = {}
        self._analysis_cache: dict[
            tuple[str, str], FrameStructureAnalysis
        ] = {}

    def _load_analysis(
        self,
        *,
        exchange: object,
        code: str,
        analysis_code: str,
        frequency: str,
        as_of: datetime,
    ) -> FrameStructureAnalysis:
        loader = getattr(exchange, "klines", None)
        if not callable(loader):
            raise TypeError("exchange must expose klines")
        frame = _closed_frame(
            loader(
                code,
                frequency,
                args={"req_counts": self._config.request_bars(frequency)},
            ),
            not_after=as_of,
            minimum_bars=self._config.minimum_bars(frequency),
        )
        closed_at = _market_datetime(frame["date"].iloc[-1], "bar close")
        cache_key = (analysis_code, frequency)
        with self._lock:
            cached = self._analysis_cache.get(cache_key)
        if cached is not None and cached.closed_at == closed_at:
            return cached
        analysis = self._analyzer(
            code=analysis_code,
            frequency=frequency,
            frame=frame,
            as_of=closed_at,
        )
        with self._lock:
            self._analysis_cache[cache_key] = analysis
        return analysis

    def _has_current_five_minute_setup(
        self,
        analysis: FrameStructureAnalysis,
    ) -> bool:
        cutoff = analysis.closed_at.timestamp() - self._config.current_setup_age_seconds
        return any(
            (
                point.observed_at
                if isinstance(point, ProvisionalCandidate)
                else point.confirmed_at or point.anchor_at
            ).timestamp()
            >= cutoff
            for point in (
                *analysis.confirmed_points,
                *analysis.provisional_points,
            )
        )

    def _cached_analysis(
        self,
        code: str,
        frequency: str,
    ) -> FrameStructureAnalysis | None:
        with self._lock:
            return self._analysis_cache.get((code, frequency))

    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
    ) -> tuple[SectorAssessment, ...]:
        observed_at = normalize_datetime(as_of, "as_of")
        raw = self._sector_provider()
        if not isinstance(raw, Mapping) or raw.get("source") != "tdx_880_industry_index":
            raise ValueError("sector catalog must expose native TDX 880 bars")
        rows = raw.get("sectors")
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise TypeError("sector catalog must expose a sectors sequence")
        stock_exchange = self._exchange_provider()
        digits, symbol_names = _universe_metadata(
            self._universe_provider(stock_exchange)
        )
        sector_exchange = self._sector_exchange_provider()
        assessments: list[SectorAssessment] = []
        members_by_sector: dict[str, tuple[str, ...]] = {}
        latest_bars: dict[tuple[str, str], datetime] = {}
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            sector_id = row.get("sector_id")
            sector_name = row.get("name")
            kline_code = row.get("kline_code")
            raw_members = row.get("member_codes")
            if (
                not isinstance(sector_id, str)
                or not sector_id.startswith("tdx-industry:")
                or sector_id in seen
                or not isinstance(sector_name, str)
                or not sector_name.strip()
                or not isinstance(kline_code, str)
                or re.fullmatch(r"SH\.880\d{3}", kline_code) is None
                or isinstance(raw_members, (str, bytes))
                or not isinstance(raw_members, Sequence)
            ):
                continue
            members = tuple(
                sorted(
                    {
                        digits[value]
                        for value in raw_members
                        if isinstance(value, str) and value in digits
                    }
                )
            )
            if len(members) < self._config.minimum_sector_members:
                continue
            seen.add(sector_id)
            members_by_sector[sector_id] = members
            try:
                analyses = {
                    frequency: self._load_analysis(
                        exchange=sector_exchange,
                        code=kline_code,
                        analysis_code=sector_id,
                        frequency=frequency,
                        as_of=observed_at,
                    )
                    for frequency in _SECTOR_FREQUENCIES
                }
                contexts = {
                    frequency: classify_context(
                        frequency=frequency,
                        current_direction=analyses[frequency].direction,
                        points=analyses[frequency].confirmed_points,
                        as_of=analyses[frequency].closed_at,
                    )
                    for frequency in _SECTOR_FREQUENCIES
                }
                context_time = max(
                    analysis.closed_at for analysis in analyses.values()
                )
                one = TimeframeContext(
                    frequency="1m",
                    direction="neutral",
                    disposition="neutral",
                    hard_block=False,
                    dominant_point_id=None,
                    dominant_point_type=None,
                    reason_codes=("stock_one_minute_trigger_only",),
                    observed_at=context_time,
                )
                assessments.append(
                    assess_sector(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        market_data_source="tdx_native_880_index",
                        thirty=contexts["30m"],
                        five=contexts["5m"],
                        one=one,
                        data_complete=True,
                    )
                )
                for frequency, analysis in analyses.items():
                    latest_bars[(sector_id, frequency)] = analysis.closed_at
            except Exception:
                assessments.append(
                    SectorAssessment(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        rank_components=(),
                        reason_codes=("sector_structure_unavailable",),
                    )
                )
        with self._lock:
            self._members = members_by_sector
            self._symbol_names = symbol_names
            self._latest_sector_bars = latest_bars
        return tuple(sorted(assessments, key=lambda item: item.sector_id))

    def members(self) -> Mapping[str, tuple[str, ...]]:
        with self._lock:
            return dict(self._members)

    def changed_bars(self, since: datetime | None) -> tuple[BarKey, ...]:
        del since
        with self._lock:
            changed = tuple(
                BarKey(code=code, frequency=frequency, closed_at=closed_at)
                for (code, frequency), closed_at in self._latest_sector_bars.items()
                if self._emitted_sector_bars.get((code, frequency)) != closed_at
            )
            for item in changed:
                self._emitted_sector_bars[(item.code, item.frequency)] = item.closed_at
        return tuple(
            sorted(changed, key=lambda item: (item.closed_at, item.code, item.frequency))
        )

    def active_watchlist(self) -> tuple[str, ...]:
        return _stock_codes(self._watchlist_provider())

    def holdings(self) -> tuple[str, ...]:
        return _stock_codes(self._holdings_provider())

    def symbol_name(self, code: str) -> str | None:
        with self._lock:
            return self._symbol_names.get(code)

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...] | None = None,
    ) -> SymbolStructureBundle:
        if _A_STOCK_CODE.fullmatch(code) is None:
            raise ValueError("invalid A-share code")
        observed_at = normalize_datetime(as_of, "as_of")
        exchange = self._exchange_provider()
        requested = set(_FREQUENCIES if frequencies is None else frequencies)
        if not requested or not requested.issubset(_FREQUENCIES):
            raise ValueError("frequencies must contain only 30m, 5m and 1m")
        analyses: dict[str, FrameStructureAnalysis] = {}
        for frequency in ("30m", "5m"):
            cached = self._cached_analysis(code, frequency)
            analyses[frequency] = (
                self._load_analysis(
                    exchange=exchange,
                    code=code,
                    analysis_code=code,
                    frequency=frequency,
                    as_of=observed_at,
                )
                if frequency in requested or cached is None
                else cached
            )
        if self._has_current_five_minute_setup(analyses["5m"]):
            cached_one = self._cached_analysis(code, "1m")
            if "1m" in requested:
                analyses["1m"] = self._load_analysis(
                    exchange=exchange,
                    code=code,
                    analysis_code=code,
                    frequency="1m",
                    as_of=observed_at,
                )
            elif cached_one is not None:
                analyses["1m"] = cached_one
        bundle_as_of = max(item.closed_at for item in analyses.values())
        confirmed = tuple(
            point
            for analysis in analyses.values()
            for point in analysis.confirmed_points
        )
        return SymbolStructureBundle(
            code=code,
            as_of=bundle_as_of,
            sector=sector,
            thirty_direction=analyses["30m"].direction,
            thirty_points=analyses["30m"].confirmed_points,
            five_points=(
                *analyses["5m"].confirmed_points,
                *analyses["5m"].provisional_points,
            ),
            one_points=(
                ()
                if "1m" not in analyses
                else analyses["1m"].confirmed_points
            ),
            opposite_points=confirmed,
        )


__all__ = (
    "FrameStructureAnalysis",
    "NativeTradingDataGateway",
    "NativeTradingGatewayConfig",
    "analyze_native_frame",
)
