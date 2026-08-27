"""固定策略年度 QMT 回放使用的稀疏因果事实。

回放只在进出场结论实际可能改变的时刻评估固定生产策略：

* 已确认的五分钟正式点在下一根可见一分钟柱开始接受成交观察；
* 一分钟买卖点只记录段差证据，不创建或阻断五分钟正式信号；
* 设置仍有效时，后续三十分钟收盘改变了环境闸门。

已确认点写入只追加事件账本。全历史严格快照并不等同于该账本：后续线段递归可以合法
替换实时尾部，并移除早期前缀曾经可见的点。因此，下方回放在线段首次可由锁定笔观察
时将其冻结，并记录每个点首次可见的时刻。非永久冻结的三十分钟方向仍在各稀疏评估
时点按前缀重新回放。
"""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
import math
import time as wall_time
from typing import Iterable, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.evidence_assembler import (
    StrictEvidenceAssembler,
)
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.formal_state import (
    current_formal_direction,
    current_formal_direction_from_components,
)
from chanlun.core.strict_structure.identity import build_center_id
from chanlun.core.strict_structure.level_catalog import recursive_level_labels
from chanlun.core.strict_structure.models import (
    CenterState,
    ConstituentUnit,
    SourceKind,
    TrendKind,
    TrendType,
)
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.strength import MacdStrengthProvider
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from chanlun.core.strict_structure.errors import StrictStructureContractError
from chanlun.core.xd_calculator import XdCalculator
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)
from chanlun.decision_support.trading_system.backtest.models import MinuteBar
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    QmtFactorAt,
    SecurityMasterRecord,
    SectorMembershipChange,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (
    QMTLocalKlineAudit,
    derive_completed_30m_with_audit,
    read_qmt_local_derived_30m,
    read_qmt_local_kline,
    resolve_qmt_local_data_dir,
)
from chanlun.decision_support.trading_system.context import (
    classify_context,
    context_point_max_age,
)
from chanlun.decision_support.trading_system.context_evidence import (
    SamePeriodTechnicalContext,
    build_same_period_technical_context,
)
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeGateBundle,
    QmtHigherTimeframeGateSource,
    unresolved_higher_timeframe_gates,
)
from chanlun.decision_support.trading_system.lifecycle import (
    five_minute_segment_difference_window_start,
    five_minute_setup_expires_at,
    five_minute_setup_family_lane,
    five_minute_setup_is_executable,
    five_minute_setup_is_in_policy_scope,
    is_one_minute_segment_difference,
    match_one_minute_nesting_witness_for_point,
    structural_point_occurrence_id,
)
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    EntryExecutionBoundary,
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    SectorAssessment,
    StructuralPoint,
    StructureTower,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)
from chanlun.decision_support.trading_system.runtime_config import (
    strict_cl_config,
)
from chanlun.decision_support.trading_system.screening_runtime import (
    ScreeningRuntimeState,
    screening_evidence_from_frame,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_CANONICAL_REQUEST_BARS,
    SCREENING_MINIMUM_BARS_BY_FREQUENCY,
    SCREENING_WARMUP_DIFFERENCE_CODES,
    SCREENING_WARMUP_REQUIRED_BARS,
    screening_warmup_reason_code,
    screening_warmup_tail_signature,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    convert_current_confirmed_point_evidence,
    extract_one_minute_segment_difference_points,
)
from chanlun.decision_support.trading_system.sector_policy import assess_sector
from chanlun.decision_support.trading_system.selection import (
    SelectionResearchSnapshot,
    visible_selection_research,
)
from chanlun.exchange.kline_precision import (
    normalize_kline_precision,
    resolve_structure_price_quantum,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
    copy_price_basis_metadata,
)


CN = ZoneInfo("Asia/Shanghai")
FREQUENCIES = ("30m", "5m", "1m")
FACT_FREQUENCIES = ("d", *FREQUENCIES)
FRAME_COLUMNS = (
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
)


def _segment_difference_jointly_known_at(
    setup: StructuralPoint,
    witness: StructuralPoint,
) -> datetime | None:
    """Return the first causal close at which one exact nesting pair is known."""

    decision_at = max(
        normalize_datetime(setup.available_at, "setup available_at"),
        normalize_datetime(witness.available_at, "witness available_at"),
    )
    if not five_minute_setup_is_executable(setup, as_of=decision_at):
        return None
    if (
        match_one_minute_nesting_witness_for_point(
            setup,
            (witness,),
            as_of=decision_at,
        )
        is not witness
    ):
        return None
    return decision_at


def _buy_segment_difference_boundary_times(
    five_points: Sequence[StructuralPoint],
    one_points: Sequence[StructuralPoint],
    *,
    eligible_times: set[datetime] | None = None,
) -> set[datetime]:
    """Return unique first joint-knowledge closes for tradable buy pairs."""

    output: set[datetime] = set()
    for setup in five_points:
        if (
            setup.side != "buy"
            or not is_five_minute_trade_level(
                setup.source_frequency,
                setup.recursive_level,
            )
            or not five_minute_setup_is_in_policy_scope(setup)
        ):
            continue
        candidates = tuple(point for point in one_points if point.side == "buy")
        if not candidates:
            continue
        witness = match_one_minute_nesting_witness_for_point(
            setup,
            candidates,
            as_of=max(
                setup.available_at,
                *(point.available_at for point in candidates),
            ),
        )
        if witness is None:
            continue
        jointly_known_at = _segment_difference_jointly_known_at(setup, witness)
        if jointly_known_at is not None and (
            eligible_times is None or jointly_known_at in eligible_times
        ):
            output.add(jointly_known_at)
    return output


BASE_FRAME_COLUMNS = FRAME_COLUMNS[:7]
FACT_SCHEMA = "chanlun-fixed-year-symbol-facts-v15"
SECTOR_FACT_SCHEMA = "chanlun-fixed-year-sector-facts-v2"
CAUSAL_CENTER_COMPLETION_CONTRACT = (
    "chanlun-causal-center-completion-v3-lifecycle-replay"
)


@dataclass(frozen=True, slots=True)
class FiveMinuteWarmupFact:
    observed_at: datetime
    source_closed_at: datetime
    converged: bool
    full_bar_count: int
    suffix_bar_count: int
    reason_code: str
    difference_codes: tuple[str, ...] = ()
    production_five_points: tuple[StructuralPoint, ...] = ()
    production_one_points: tuple[StructuralPoint, ...] = ()
    one_minute_bar_count: int = 0

    def __post_init__(self) -> None:
        observed = normalize_datetime(self.observed_at, "warmup observed_at")
        source_closed = normalize_datetime(
            self.source_closed_at,
            "warmup source_closed_at",
        )
        if source_closed > observed:
            raise ValueError("warmup source close cannot follow its decision")
        expected_reason = screening_warmup_reason_code(
            frequency="5m",
            converged=self.converged,
            full_bar_count=self.full_bar_count,
            suffix_bar_count=self.suffix_bar_count,
        )
        if self.reason_code != expected_reason:
            raise ValueError("5m warmup reason contradicts its measurement")
        if (
            type(self.difference_codes) is not tuple
            or len(self.difference_codes) != len(set(self.difference_codes))
            or not set(self.difference_codes).issubset(
                SCREENING_WARMUP_DIFFERENCE_CODES
            )
            or (self.converged and self.difference_codes)
        ):
            raise ValueError("5m warmup difference codes are inconsistent")
        if type(self.one_minute_bar_count) is not int or self.one_minute_bar_count < 0:
            raise ValueError("1m production history count is invalid")
        point_groups = (
            ("5m", self.production_five_points),
            ("1m", self.production_one_points),
        )
        for frequency, points in point_groups:
            if type(points) is not tuple:
                raise ValueError("production execution points must be tuples")
            if len({point.point_id for point in points}) != len(points):
                raise ValueError("production execution points must be unique")
            if any(
                not isinstance(point, StructuralPoint)
                or not point.confirmed
                or point.source_frequency != frequency
                or point.available_at > observed
                for point in points
            ):
                raise ValueError("production execution point is invalid")
        if any(point.available_at != observed for point in self.production_one_points):
            raise ValueError("production current 1m point must become available now")
        if (
            self.one_minute_bar_count < SCREENING_MINIMUM_BARS_BY_FREQUENCY["1m"]
            and self.production_one_points
        ):
            raise ValueError("production current 1m point lacks minimum history")
        codes = {point.code for _frequency, points in point_groups for point in points}
        if len(codes) > 1:
            raise ValueError("production execution points mix symbols")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "source_closed_at", source_closed)


@dataclass(frozen=True, slots=True)
class SparseEvaluationFact:
    observed_at: datetime
    thirty_direction: ContextDirection
    bar: MinuteBar
    sector_id: str | None = None
    daily_direction: ContextDirection = "neutral"
    higher_timeframe_gates: HigherTimeframeGateBundle | None = None
    one_minute_bar_count: int = SCREENING_MINIMUM_BARS_BY_FREQUENCY["1m"]
    daily_technical_context: SamePeriodTechnicalContext | None = None
    thirty_technical_context: SamePeriodTechnicalContext | None = None

    def __post_init__(self) -> None:
        observed = normalize_datetime(self.observed_at, "observed_at")
        if self.bar.closed_at != observed:
            raise ValueError("evaluation bar must close at observed_at")
        if self.daily_direction not in {"up", "down", "neutral"}:
            raise ValueError("daily direction is invalid")
        if type(self.one_minute_bar_count) is not int or self.one_minute_bar_count < 0:
            raise ValueError("evaluation 1m history count is invalid")
        gates = self.higher_timeframe_gates
        if gates is not None and any(
            value.observed_at != observed
            for value in (gates.market, gates.sector, gates.symbol)
        ):
            raise ValueError("higher-timeframe gate must match the evaluation time")
        for frequency, context in (
            ("d", self.daily_technical_context),
            ("30m", self.thirty_technical_context),
        ):
            if context is not None and (
                context.frequency != frequency or context.observed_at != observed
            ):
                raise ValueError(
                    "same-period technical context must match its evaluation"
                )
        object.__setattr__(self, "observed_at", observed)


@dataclass(frozen=True, slots=True)
class PointVisibilityInterval:
    """A half-open interval in which one causal structural point is current."""

    point_id: str
    visible_from: datetime
    visible_until: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.point_id, str) or not self.point_id:
            raise ValueError("point visibility requires a point id")
        start = normalize_datetime(self.visible_from, "point visible_from")
        end = (
            None
            if self.visible_until is None
            else normalize_datetime(self.visible_until, "point visible_until")
        )
        if end is not None and end <= start:
            raise ValueError("point visibility interval must be non-empty")
        object.__setattr__(self, "visible_from", start)
        object.__setattr__(self, "visible_until", end)

    def contains(self, observed_at: datetime) -> bool:
        value = normalize_datetime(observed_at, "point visibility observation")
        return self.visible_from <= value and (
            self.visible_until is None or value < self.visible_until
        )


@dataclass(frozen=True, slots=True)
class SymbolResearchFacts:
    schema: str
    algorithm_revision: str
    source_revision: str
    code: str
    sector_id: str
    requested_start: date
    requested_end: date
    effective_start: date
    row_counts: tuple[tuple[str, int], ...]
    daily_points: tuple[StructuralPoint, ...]
    thirty_points: tuple[StructuralPoint, ...]
    five_points: tuple[StructuralPoint, ...]
    one_points: tuple[StructuralPoint, ...]
    evaluations: tuple[SparseEvaluationFact, ...]
    daily_point_visibility: tuple[PointVisibilityInterval, ...] = ()
    thirty_point_visibility: tuple[PointVisibilityInterval, ...] = ()
    five_point_visibility: tuple[PointVisibilityInterval, ...] = ()
    one_point_visibility: tuple[PointVisibilityInterval, ...] = ()
    five_minute_warmup: tuple[FiveMinuteWarmupFact, ...] = ()
    direction_unavailable_count: int = 0
    security_master: SecurityMasterRecord | None = None
    memberships: tuple[SectorMembershipChange, ...] = ()
    factors: tuple[QmtFactorAt, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != FACT_SCHEMA:
            raise ValueError("unsupported fixed-year fact schema")
        for value, label in (
            (self.algorithm_revision, "algorithm_revision"),
            (self.source_revision, "source_revision"),
        ):
            if not (
                isinstance(value, str)
                and value.startswith("sha256:")
                and len(value) == 71
                and all(char in "0123456789abcdef" for char in value[7:])
            ):
                raise ValueError(f"{label} must be a sha256 fingerprint")
        if not self.code or not self.sector_id:
            raise ValueError("symbol and sector identity are required")
        if not self.requested_start <= self.effective_start <= self.requested_end:
            raise ValueError("invalid fixed-year fact range")
        names = tuple(name for name, _count in self.row_counts)
        if names != FACT_FREQUENCIES or any(
            count < 0 for _name, count in self.row_counts
        ):
            raise ValueError("row counts must cover d, 30m, 5m and 1m")
        times = tuple(row.observed_at for row in self.evaluations)
        if times != tuple(sorted(set(times))):
            raise ValueError("evaluation facts must be unique and chronological")
        warmup_times = tuple(row.observed_at for row in self.five_minute_warmup)
        if warmup_times != tuple(sorted(set(warmup_times))) or not set(
            warmup_times
        ).issubset(times):
            raise ValueError("5m warmup facts must match unique evaluation times")
        boundary_times = _buy_segment_difference_boundary_times(
            self.five_points,
            self.one_points,
            eligible_times=set(times),
        )
        if set(warmup_times) != boundary_times:
            raise ValueError(
                "every exact 1m nesting boundary requires a production snapshot"
            )
        for frequency, points, visibility in (
            ("d", self.daily_points, self.daily_point_visibility),
            ("30m", self.thirty_points, self.thirty_point_visibility),
            ("5m", self.five_points, self.five_point_visibility),
            ("1m", self.one_points, self.one_point_visibility),
        ):
            point_ids = {point.point_id for point in points}
            if any(interval.point_id not in point_ids for interval in visibility):
                raise ValueError(f"{frequency} visibility references an unknown point")
            interval_order = tuple(
                (interval.visible_from, interval.point_id) for interval in visibility
            )
            if interval_order != tuple(sorted(interval_order)):
                raise ValueError(
                    f"{frequency} visibility intervals must be chronological"
                )
            intervals_by_point: dict[str, list[PointVisibilityInterval]] = {}
            for interval in visibility:
                intervals_by_point.setdefault(interval.point_id, []).append(interval)
            for intervals in intervals_by_point.values():
                for previous, current in zip(intervals, intervals[1:]):
                    if previous.visible_until is None or (
                        previous.visible_until > current.visible_from
                    ):
                        raise ValueError(
                            f"{frequency} visibility intervals cannot overlap"
                        )
        if self.direction_unavailable_count < 0:
            raise ValueError("direction unavailable count cannot be negative")
        if self.security_master is not None and self.security_master.code != self.code:
            raise ValueError("security master does not match symbol facts")
        if any(row.code != self.code for row in self.memberships):
            raise ValueError("sector membership does not match symbol facts")
        if any(row.code != self.code for row in self.factors):
            raise ValueError("QMT factor does not match symbol facts")
        membership_order = tuple(
            (row.known_at, row.sector_id) for row in self.memberships
        )
        if membership_order != tuple(sorted(set(membership_order))):
            raise ValueError("symbol memberships must be unique and chronological")
        factor_dates = tuple(row.effective_on for row in self.factors)
        if factor_dates != tuple(sorted(set(factor_dates))):
            raise ValueError("symbol factors must be unique and chronological")


@dataclass(frozen=True, slots=True)
class SectorResearchFacts:
    schema: str
    algorithm_revision: str
    source_revision: str
    sector_id: str
    sector_name: str
    member_count: int
    row_count: int
    thirty_points: tuple[StructuralPoint, ...]
    assessments: tuple[tuple[datetime, SectorAssessment], ...]
    thirty_point_visibility: tuple[PointVisibilityInterval, ...] = ()
    direction_unavailable_count: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        if self.schema != SECTOR_FACT_SCHEMA:
            raise ValueError("unsupported fixed-year sector fact schema")
        for value, label in (
            (self.algorithm_revision, "algorithm_revision"),
            (self.source_revision, "source_revision"),
        ):
            if not (
                isinstance(value, str)
                and value.startswith("sha256:")
                and len(value) == 71
                and all(char in "0123456789abcdef" for char in value[7:])
            ):
                raise ValueError(f"{label} must be a sha256 fingerprint")
        if not self.sector_id or not self.sector_name or self.member_count < 0:
            raise ValueError("invalid sector identity")
        if self.row_count < 0 or self.direction_unavailable_count < 0:
            raise ValueError("invalid sector coverage counts")
        times = tuple(observed_at for observed_at, _row in self.assessments)
        if times != tuple(sorted(set(times))):
            raise ValueError("sector assessments must be unique and chronological")
        point_ids = {point.point_id for point in self.thirty_points}
        if len(point_ids) != len(self.thirty_points):
            raise ValueError("sector point identities must be unique")
        visibility_ids = {
            interval.point_id for interval in self.thirty_point_visibility
        }
        if visibility_ids != point_ids:
            raise ValueError("every sector point requires a visibility interval")
        visibility_by_point: dict[str, list[PointVisibilityInterval]] = {}
        for interval in self.thirty_point_visibility:
            visibility_by_point.setdefault(interval.point_id, []).append(interval)
        for point_id, intervals in visibility_by_point.items():
            point = next(
                candidate
                for candidate in self.thirty_points
                if candidate.point_id == point_id
            )
            if any(
                interval.visible_from < point.available_at for interval in intervals
            ):
                raise ValueError("sector point cannot be visible before availability")
            ordered = tuple(
                sorted(intervals, key=lambda interval: interval.visible_from)
            )
            if tuple(intervals) != ordered:
                raise ValueError("sector point visibility must be chronological")
            for previous, current in zip(ordered, ordered[1:]):
                if previous.visible_until is None or (
                    previous.visible_until > current.visible_from
                ):
                    raise ValueError("sector point visibility cannot overlap")


def qmt_native_code(code: str) -> str:
    market, digits = code.split(".", 1)
    if market not in {"SH", "SZ", "BJ"} or len(digits) != 6 or not digits.isdigit():
        raise ValueError(f"invalid normalized A-share code: {code!r}")
    return f"{digits}.{market}"


def _empty_frame(code: str) -> pd.DataFrame:
    return pd.DataFrame(columns=FRAME_COLUMNS).assign(code=code)


def qmt_factor_frame(factors: Sequence[QmtFactorAt]) -> pd.DataFrame:
    return pd.DataFrame(
        (
            {
                "effective_on": row.effective_on,
                "interest": str(row.interest),
                "stockBonus": str(row.stock_bonus),
                "stockGift": str(row.stock_gift),
                "allotNum": str(row.allot_num),
                "allotPrice": str(row.allot_price),
                "gugai": str(row.gugai),
                "dr": str(row.raw_price_divisor),
            }
            for row in factors
        ),
        columns=(
            "effective_on",
            "interest",
            "stockBonus",
            "stockGift",
            "allotNum",
            "allotPrice",
            "gugai",
            "dr",
        ),
    )


def load_qmt_frame(
    code: str,
    frequency: str,
    *,
    start_at: datetime,
    end_at: datetime,
    factors: pd.DataFrame | None = None,
    _allow_native_daily: bool = False,
    _local_five_snapshot: tuple[pd.DataFrame, QMTLocalKlineAudit] | None = None,
    _history_bars_before_start: int | None = None,
) -> pd.DataFrame:
    """读取原始 QMT 行，并只用除权日当时已知信息构建因果分析价格基准。"""

    if frequency not in FREQUENCIES and not (_allow_native_daily and frequency == "1d"):
        raise ValueError("frequency must be 30m, 5m or 1m")
    start = normalize_datetime(start_at, "start_at")
    end = normalize_datetime(end_at, "end_at")
    if start > end:
        raise ValueError("start_at cannot follow end_at")
    if _history_bars_before_start is not None and (
        type(_history_bars_before_start) is not int or _history_bars_before_start <= 0
    ):
        raise ValueError("history bars before start must be a positive integer")
    if _local_five_snapshot is not None and _history_bars_before_start is not None:
        raise ValueError("shared QMT snapshots cannot apply a history lookback")
    native = qmt_native_code(code)
    fields = ("time", "open", "high", "low", "close", "volume")
    frame = pd.DataFrame()
    provider_error: Exception | None = None
    local_directory = resolve_qmt_local_data_dir()
    if _local_five_snapshot is not None:
        if frequency not in {"30m", "5m"}:
            raise ValueError("a shared 5m snapshot only supports 5m or derived 30m")
        source_frame, source_audit = _local_five_snapshot
        if source_audit.code != code or source_audit.frequency != "5m":
            raise ValueError("shared QMT 5m snapshot identity does not match request")
        if frequency == "30m":
            frame, local_audit = derive_completed_30m_with_audit(
                source_frame,
                source_audit,
            )
        else:
            frame = source_frame.copy()
            frame.attrs = dict(source_frame.attrs)
            local_audit = source_audit
        if not frame.empty:
            frame.attrs.update(
                qmt_transport=(
                    "LOCAL_5M_DERIVED_30M_READ_ONLY"
                    if frequency == "30m"
                    else "LOCAL_FIXED_RECORD_READ_ONLY"
                ),
                qmt_local_cache_audit_id=local_audit.audit_id,
                qmt_local_cache_source_sha256=local_audit.source_sha256,
            )
    elif local_directory is not None:
        reader = (
            read_qmt_local_derived_30m if frequency == "30m" else read_qmt_local_kline
        )
        reader_arguments: dict[str, object] = {
            "data_dir": local_directory,
            "code": code,
            "start_at": start,
            "end_at": end,
        }
        if frequency != "30m":
            reader_arguments["frequency"] = frequency
            reader_arguments["history_bars_before_start"] = _history_bars_before_start
        frame, local_audit = reader(**reader_arguments)  # type: ignore[arg-type]
        if not frame.empty:
            frame.attrs.update(
                qmt_transport=(
                    "LOCAL_5M_DERIVED_30M_READ_ONLY"
                    if frequency == "30m"
                    else "LOCAL_FIXED_RECORD_READ_ONLY"
                ),
                qmt_local_cache_audit_id=local_audit.audit_id,
                qmt_local_cache_source_sha256=local_audit.source_sha256,
            )
    else:
        try:
            from xtquant import xtdata

            xtdata.enable_hello = False
            for attempt in range(3):
                try:
                    raw = xtdata.get_market_data(
                        field_list=list(fields),
                        stock_list=[native],
                        period=frequency,
                        start_time=(
                            ""
                            if _history_bars_before_start is not None
                            else start.strftime("%Y%m%d%H%M%S")
                        ),
                        end_time=end.strftime("%Y%m%d%H%M%S"),
                        count=-1,
                        dividend_type="none",
                        fill_data=False,
                    )
                except Exception as exc:  # 数据提供方可能在遍历标的范围期间中断
                    provider_error = exc
                    raw = {}
                columns: dict[str, pd.Series] = {}
                if isinstance(raw, Mapping):
                    for field in fields:
                        matrix = raw.get(field)
                        if (
                            not isinstance(matrix, pd.DataFrame)
                            or native not in matrix.index
                        ):
                            columns = {}
                            break
                        columns[field] = matrix.loc[native]
                if columns:
                    candidate = pd.DataFrame(columns)
                    for field in fields:
                        candidate[field] = pd.to_numeric(
                            candidate[field], errors="coerce"
                        )
                    candidate = candidate.dropna(subset=list(fields))
                    candidate = candidate[
                        (candidate["time"] > 0)
                        & (candidate["open"] > 0)
                        & (candidate["high"] > 0)
                        & (candidate["low"] > 0)
                        & (candidate["close"] > 0)
                        & (candidate["volume"] >= 0)
                    ].copy()
                    candidate = candidate[
                        candidate["time"] <= math.floor(end.timestamp() * 1000)
                    ].sort_values("time", kind="stable")
                    if _history_bars_before_start is not None:
                        start_ms = math.floor(start.timestamp() * 1000)
                        preceding = candidate[candidate["time"] < start_ms].tail(
                            _history_bars_before_start
                        )
                        current = candidate[candidate["time"] >= start_ms]
                        candidate = pd.concat(
                            (preceding, current),
                            ignore_index=True,
                        )
                    if not candidate.empty:
                        frame = candidate
                        frame.attrs["qmt_transport"] = "RPC"
                        break
                if attempt < 2:
                    wall_time.sleep(0.05 * (attempt + 1))
        except (ImportError, ModuleNotFoundError) as exc:
            provider_error = exc
    transport_metadata = dict(frame.attrs)
    if frame.empty:
        if provider_error is not None and local_directory is None:
            raise RuntimeError(
                "QMT RPC is unavailable and CHANLUN_QMT_LOCAL_DATA_DIR is not set"
            ) from provider_error
        return _empty_frame(code)
    frame["date"] = pd.to_datetime(
        frame.pop("time"), unit="ms", utc=True
    ).dt.tz_convert(CN)
    if frequency == "1d":
        # 行情终端 QMT 的固定日线记录标记在午夜；日级事实直到 A 股收盘才可知，
        # 在 15:00 暴露，而不是在该交易日开始时暴露。
        frame["date"] = frame["date"].dt.normalize() + pd.Timedelta(hours=15)
    frame.insert(0, "code", code)
    frame = frame.loc[:, list(BASE_FRAME_COLUMNS)]
    frame = frame.sort_values("date", kind="stable").drop_duplicates(
        "date", keep="last"
    )
    frame = frame.reset_index(drop=True)
    normalized = normalize_kline_precision(frame, "a", code)
    if normalized is None:
        raise ValueError("QMT precision normalization returned no frame")
    for field in ("open", "high", "low", "close"):
        normalized[f"raw_{field}"] = normalized[field]
    multiplier = pd.Series(1.0, index=normalized.index, dtype="float64")
    if factors is not None and not factors.empty:
        required = {"effective_on", "dr"}
        if not required.issubset(factors.columns):
            raise ValueError("causal QMT factor ledger is malformed")
        for row in factors.sort_values("effective_on").itertuples(index=False):
            effective_on = row.effective_on
            if not isinstance(effective_on, date):
                effective_on = date.fromisoformat(str(effective_on))
            divisor = float(row.dr)
            if not math.isfinite(divisor) or divisor <= 0:
                raise ValueError("causal QMT factor divisor must be positive")
            sessions = normalized["date"].map(lambda value: value.date())
            multiplier.loc[sessions >= effective_on] *= divisor
    for field in ("open", "high", "low", "close"):
        normalized[field] = normalized[f"raw_{field}"] * multiplier
    quantum = resolve_structure_price_quantum("a", code)
    if quantum is None:
        raise ValueError("A-share structure price quantum is unavailable")
    metadata = build_provider_price_basis_metadata(
        provider="qmt",
        market="a",
        code=code,
        adjustment="causal-forward-ex-date",
        structure_price_quantum=quantum,
    )
    normalized = normalized.loc[:, list(FRAME_COLUMNS)]
    result = attach_price_basis_metadata(normalized, metadata)
    result.attrs.update(transport_metadata)
    return result


def load_qmt_daily_frame(
    code: str,
    *,
    start_at: datetime,
    end_at: datetime,
    factors: pd.DataFrame | None = None,
    history_bars_before_start: int | None = None,
) -> pd.DataFrame:
    """读取 QMT 原生日线，并保持与 1 分钟线相同的因果价格基准。"""

    return load_qmt_frame(
        code,
        "1d",
        start_at=start_at,
        end_at=end_at,
        factors=factors,
        _allow_native_daily=True,
        _history_bars_before_start=history_bars_before_start,
    )


def strict_state(code: str, frequency: str, frame: pd.DataFrame) -> CL:
    if frame.empty:
        raise ValueError(f"{frequency} frame is empty")
    quantum = Decimal(str(frame.attrs["structure_price_quantum"]))
    revision = cast(str, frame.attrs["price_basis_revision"])
    config = strict_cl_config(
        structure_price_quantum=quantum,
        price_basis_revision=revision,
    )
    # 严格背驰使用因果局部高周期 MACD；每个源 K 线样本冻结于自身收盘，使认证回放与
    # 实时计算共享同一前缀稳定力度证据。
    return CL(code, frequency, config, market="a")


def _symbol_source_revision(
    frames: Mapping[str, pd.DataFrame],
    factors: pd.DataFrame,
    *,
    security_master: SecurityMasterRecord | None = None,
    memberships: Sequence[SectorMembershipChange] = (),
) -> str:
    """为事实实际使用的 QMT 行和复权因子账本生成精确指纹。

    检查点属于派生结果，只记录行数无法证明由哪些历史值生成。因此需要纳入三个周期
    的内容哈希、各自价格基准元数据以及复权因子账本。
    """

    digest = hashlib.sha256()
    for name in (*FACT_FREQUENCIES, "factors"):
        frame = factors if name == "factors" else frames[name]
        digest.update(name.encode("utf-8"))
        digest.update(len(frame).to_bytes(8, "big"))
        digest.update(
            pd.util.hash_pandas_object(
                frame.reset_index(drop=True),
                index=False,
                categorize=False,
            )
            .to_numpy(dtype="uint64", copy=False)
            .tobytes()
        )
        if name != "factors":
            metadata = {
                key: frame.attrs.get(key)
                for key in sorted(frame.attrs)
                if key
                in {
                    "adjustment",
                    "price_basis_revision",
                    "qmt_local_cache_audit_id",
                    "qmt_local_cache_source_sha256",
                    "qmt_transport",
                    "structure_price_quantum",
                }
            }
            digest.update(
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    digest.update(repr(security_master).encode("utf-8"))
    digest.update(repr(tuple(memberships)).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def final_confirmed_points(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
) -> tuple[StructuralPoint, ...]:
    return _causal_confirmed_points(code, frequency, frame)


@dataclass(frozen=True, slots=True)
class CausalCenterCompletionFact:
    """在因果检查点首次可见的只读完成几何事实。

    严格中枢实现仍是唯一权威。本事实只复制不可变的完成离开段和返回段，避免下游策略
    适配器再从买卖点锚定时间推断历史窗口。
    """

    center_id: str
    source_frequency: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    body_revision: int
    available_at: datetime
    completed_at: datetime
    zd_tick: int
    zg_tick: int
    entry_unit_id: str | None
    core_unit_ids: tuple[str, ...]
    establishment_leave_unit_id: str | None
    establishment_unit_ids: tuple[str, ...]
    leave_unit_id: str
    leave_direction: str
    leave_market_start: datetime
    leave_market_end: datetime
    leave_available_at: datetime
    leave_start_tick: int
    leave_end_tick: int
    leave_low_tick: int
    leave_high_tick: int
    return_unit_id: str
    return_direction: str
    return_market_start: datetime
    return_market_end: datetime
    return_available_at: datetime
    return_start_tick: int
    return_end_tick: int
    return_low_tick: int
    return_high_tick: int
    contract: str = CAUSAL_CENTER_COMPLETION_CONTRACT

    def __post_init__(self) -> None:
        for field in (
            "available_at",
            "completed_at",
            "leave_market_start",
            "leave_market_end",
            "leave_available_at",
            "return_market_start",
            "return_market_end",
            "return_available_at",
        ):
            object.__setattr__(
                self, field, normalize_datetime(getattr(self, field), field)
            )
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        if (
            not self.center_id
            or not self.source_frequency
            or not self.price_basis_revision
        ):
            raise ValueError("causal center identity is required")
        if self.contract != CAUSAL_CENTER_COMPLETION_CONTRACT:
            raise ValueError("causal center completion contract changed")
        if not self.leave_unit_id or not self.return_unit_id:
            raise ValueError("causal center completion unit ids are required")
        if self.structural_level < 0 or self.body_revision < 0:
            raise ValueError("causal center revisions and levels cannot be negative")
        # Recursive centers retain their inclusive three-trend overlap. Physical
        # segment/stroke centers require a strictly positive five-role overlap.
        if self.zd_tick > self.zg_tick or (
            self.source_kind is not SourceKind.TREND_TYPE
            and self.zd_tick == self.zg_tick
        ):
            raise ValueError("causal center core must be a non-empty interval")
        core = tuple(self.core_unit_ids)
        establishment = tuple(self.establishment_unit_ids)
        if len(core) != 3 or len(core) != len(set(core)):
            raise ValueError(
                "causal center core must reference exactly three unique units"
            )
        role_ids = (
            *(() if self.entry_unit_id is None else (self.entry_unit_id,)),
            *core,
            *(
                ()
                if self.establishment_leave_unit_id is None
                else (self.establishment_leave_unit_id,)
            ),
        )
        if any(not isinstance(value, str) or not value for value in role_ids):
            raise ValueError("causal center establishment unit id is invalid")
        if self.source_kind is SourceKind.TREND_TYPE:
            if self.establishment_leave_unit_id is not None:
                raise ValueError(
                    "recursive causal center cannot carry a physical leave role"
                )
            if self.entry_unit_id is not None and self.entry_unit_id in core:
                raise ValueError(
                    "recursive causal center entry must stay outside its core"
                )
            if establishment != core:
                raise ValueError(
                    "recursive causal center establishment must equal its core"
                )
        else:
            if self.entry_unit_id is None or self.establishment_leave_unit_id is None:
                raise ValueError(
                    "physical causal center requires entry and establishment leave"
                )
            if establishment != role_ids or len(set(establishment)) != 5:
                raise ValueError(
                    "physical causal center requires five unique establishment roles"
                )
        expected_center_id = build_center_id(
            price_basis_revision=self.price_basis_revision,
            structural_level=self.structural_level,
            source_kind=self.source_kind.value,
            entry_unit_id=(
                None
                if self.source_kind is SourceKind.TREND_TYPE
                else self.entry_unit_id
            ),
            initial_unit_ids=core,
            establishment_leave_unit_id=(
                None
                if self.source_kind is SourceKind.TREND_TYPE
                else self.establishment_leave_unit_id
            ),
            zd_tick=self.zd_tick,
            zg_tick=self.zg_tick,
        )
        if self.center_id != expected_center_id:
            raise ValueError(
                "causal center id does not match its immutable establishment roles"
            )
        establishment_id_set = set(establishment)
        if self.return_unit_id in establishment_id_set:
            raise ValueError("causal center return must be outside establishment roles")
        if (
            self.leave_unit_id in establishment_id_set
            and self.leave_unit_id != self.establishment_leave_unit_id
        ):
            raise ValueError(
                "causal completion leave can reuse only the establishment leave"
            )
        object.__setattr__(self, "core_unit_ids", core)
        object.__setattr__(self, "establishment_unit_ids", establishment)
        if self.leave_direction != "up" or self.return_direction != "down":
            # 严格策略只消费向上三买完成几何；通用账本保持关闭失败，可防止卖侧中枢
            # 被误认成入场证据。
            raise ValueError("causal entry center requires up-leave/down-return")
        if not (
            self.leave_market_start
            <= self.leave_market_end
            <= self.return_market_start
            <= self.return_market_end
            <= self.completed_at
            <= self.available_at
        ):
            raise ValueError("causal center completion times are inconsistent")
        if (
            self.leave_available_at > self.available_at
            or self.return_available_at > self.available_at
        ):
            raise ValueError("center units cannot become visible after the center fact")


@dataclass(frozen=True, slots=True)
class CausalStructureEventLedger:
    """只追加记录从冻结线段前缀中首次观察到的事实。"""

    points: tuple[StructuralPoint, ...]
    completed_trends: tuple[TrendType, ...]
    completed_units: tuple[ConstituentUnit, ...] = ()
    center_completions: tuple[CausalCenterCompletionFact, ...] = ()
    point_anchor_unit_ids: tuple[tuple[str, str], ...] = ()
    point_visibility: tuple[PointVisibilityInterval, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "completed_trends", tuple(self.completed_trends))
        object.__setattr__(self, "completed_units", tuple(self.completed_units))
        object.__setattr__(self, "center_completions", tuple(self.center_completions))
        object.__setattr__(
            self,
            "point_anchor_unit_ids",
            tuple(self.point_anchor_unit_ids),
        )
        object.__setattr__(self, "point_visibility", tuple(self.point_visibility))
        if (
            tuple(
                sorted(self.points, key=lambda item: (item.available_at, item.point_id))
            )
            != self.points
        ):
            raise ValueError("causal points must be deterministically ordered")
        if (
            tuple(
                sorted(
                    self.completed_trends,
                    key=lambda item: (item.available_at, item.trend_id),
                )
            )
            != self.completed_trends
        ):
            raise ValueError("causal trends must be deterministically ordered")
        if (
            tuple(
                sorted(
                    self.completed_units,
                    key=lambda item: (item.available_at, item.unit_id),
                )
            )
            != self.completed_units
        ):
            raise ValueError("causal completed units must be deterministically ordered")
        unit_keys = tuple(
            (item.unit_id, item.structural_level) for item in self.completed_units
        )
        if len(unit_keys) != len(set(unit_keys)):
            raise ValueError("causal completed units must be unique")
        if (
            tuple(
                sorted(
                    self.center_completions,
                    key=lambda item: (item.available_at, item.center_id),
                )
            )
            != self.center_completions
        ):
            raise ValueError(
                "causal center completions must be deterministically ordered"
            )
        center_keys = tuple(
            (item.center_id, item.structural_level) for item in self.center_completions
        )
        if len(center_keys) != len(set(center_keys)):
            raise ValueError("causal center completions must be unique")
        anchors = tuple(self.point_anchor_unit_ids)
        if anchors != tuple(sorted(anchors)):
            raise ValueError("causal point anchors must be sorted")
        if len({point_id for point_id, _unit_id in anchors}) != len(anchors):
            raise ValueError("causal point anchors must be unique by point")
        point_ids = {item.point_id for item in self.points}
        if {point_id for point_id, _unit_id in anchors} != point_ids:
            raise ValueError("every causal point requires one anchor unit")
        unit_ids = {item.unit_id for item in self.completed_units}
        if any(unit_id not in unit_ids for _point_id, unit_id in anchors):
            raise ValueError("causal point anchor references an unknown unit")
        units_by_key = {
            (item.unit_id, item.structural_level): item for item in self.completed_units
        }
        for center in self.center_completions:
            external_entry_ids = (
                ()
                if center.entry_unit_id is None
                or center.entry_unit_id in center.establishment_unit_ids
                else (center.entry_unit_id,)
            )
            referenced_ids = (
                *external_entry_ids,
                *center.establishment_unit_ids,
                center.leave_unit_id,
                center.return_unit_id,
            )
            try:
                referenced_units = tuple(
                    units_by_key[(unit_id, center.structural_level)]
                    for unit_id in referenced_ids
                )
            except KeyError as exc:
                raise ValueError(
                    "causal center references an unknown completed unit"
                ) from exc
            if any(
                unit.source_kind is not center.source_kind
                or unit.price_basis_revision != center.price_basis_revision
                or not unit.locked
                or unit.available_at > center.available_at
                for unit in referenced_units
            ):
                raise ValueError("causal center establishment evidence is incompatible")
            establishment_start = len(external_entry_ids)
            establishment_count = len(center.establishment_unit_ids)
            establishment_units = referenced_units[
                establishment_start : establishment_start + establishment_count
            ]
            core_units = tuple(
                units_by_key[(unit_id, center.structural_level)]
                for unit_id in center.core_unit_ids
            )
            expected_zd = max(unit.low_tick for unit in core_units)
            expected_zg = min(unit.high_tick for unit in core_units)
            if (center.zd_tick, center.zg_tick) != (expected_zd, expected_zg):
                raise ValueError(
                    "causal center core does not match its three core units"
                )
            strict_overlap = center.source_kind is not SourceKind.TREND_TYPE
            for unit in establishment_units:
                left = max(unit.low_tick, center.zd_tick)
                right = min(unit.high_tick, center.zg_tick)
                invalid_overlap = left >= right if strict_overlap else left > right
                if invalid_overlap:
                    raise ValueError("causal center establishment role misses the core")
            if strict_overlap:
                for previous, current in zip(
                    establishment_units,
                    establishment_units[1:],
                ):
                    if (
                        previous.direction == current.direction
                        or previous.end_tick != current.start_tick
                        or current.market_start < previous.market_end
                    ):
                        raise ValueError(
                            "physical causal center roles are not one sequence"
                        )
                establishment_leave = establishment_units[-1]
                outside = (
                    establishment_leave.end_tick > center.zg_tick
                    if establishment_leave.direction == "up"
                    else establishment_leave.end_tick < center.zd_tick
                )
                if not outside:
                    raise ValueError(
                        "physical causal center establishment leave stays in core"
                    )
            context_units = tuple(
                sorted(
                    (
                        unit
                        for unit in self.completed_units
                        if unit.structural_level == center.structural_level
                        and unit.source_kind is center.source_kind
                        and unit.price_basis_revision == center.price_basis_revision
                        and unit.available_at <= center.available_at
                    ),
                    key=lambda unit: (
                        unit.market_start,
                        unit.market_end,
                        unit.unit_id,
                    ),
                )
            )
            context_positions = {
                unit.unit_id: offset for offset, unit in enumerate(context_units)
            }
            seed_units = (*referenced_units[:establishment_start], *establishment_units)
            seed_offsets = tuple(context_positions[unit.unit_id] for unit in seed_units)
            if seed_offsets != tuple(
                range(seed_offsets[0], seed_offsets[0] + len(seed_offsets))
            ):
                raise ValueError("causal center establishment is not one source slice")
            leave = referenced_units[-2]
            ret = referenced_units[-1]
            leave_signature = (
                leave.unit_id,
                leave.direction,
                leave.market_start,
                leave.market_end,
                leave.available_at,
                leave.start_tick,
                leave.end_tick,
                leave.low_tick,
                leave.high_tick,
            )
            stored_leave_signature = (
                center.leave_unit_id,
                center.leave_direction,
                center.leave_market_start,
                center.leave_market_end,
                center.leave_available_at,
                center.leave_start_tick,
                center.leave_end_tick,
                center.leave_low_tick,
                center.leave_high_tick,
            )
            return_signature = (
                ret.unit_id,
                ret.direction,
                ret.market_start,
                ret.market_end,
                ret.available_at,
                ret.start_tick,
                ret.end_tick,
                ret.low_tick,
                ret.high_tick,
            )
            stored_return_signature = (
                center.return_unit_id,
                center.return_direction,
                center.return_market_start,
                center.return_market_end,
                center.return_available_at,
                center.return_start_tick,
                center.return_end_tick,
                center.return_low_tick,
                center.return_high_tick,
            )
            if (
                leave_signature != stored_leave_signature
                or return_signature != stored_return_signature
                or center.completed_at != ret.confirmed_at
            ):
                raise ValueError(
                    "causal center completion geometry changed from its unit ledger"
                )
            leave_overlap_left = max(leave.low_tick, center.zd_tick)
            leave_overlap_right = min(leave.high_tick, center.zg_tick)
            # Lifecycle leaves may start exactly on ZD/ZG.  Physical center
            # establishment still requires positive overlap above, but the
            # later departure watcher deliberately uses closed-interval touch.
            leave_misses_core = leave_overlap_left > leave_overlap_right
            if (
                leave_misses_core
                or leave.end_tick <= center.zg_tick
                or ret.direction == leave.direction
                or ret.start_tick != leave.end_tick
                or ret.market_start < leave.market_end
                or ret.low_tick < center.zg_tick
            ):
                raise ValueError(
                    "causal center completion is not an outside up-leave/down-return"
                )
            seed_end_offset = seed_offsets[-1]
            leave_offset = context_positions[leave.unit_id]
            return_offset = context_positions[ret.unit_id]
            if leave_offset < seed_end_offset or return_offset != leave_offset + 1:
                raise ValueError(
                    "causal center completion is not one lifecycle source slice"
                )
            relevant_oscillatory_ids = frozenset(
                trend.trend_id
                for trend in self.completed_trends
                if trend.kind is TrendKind.CONSOLIDATION
                and trend.available_at <= center.available_at
                and trend.structural_level + 1 == center.structural_level
                and trend.price_basis_revision == center.price_basis_revision
                and trend.trend_id in context_positions
            )
            try:
                replayed = (
                    establish_center(
                        establishment_units,
                        center.structural_level,
                        center.source_kind,
                        relevant_oscillatory_ids,
                        entry_unit=(
                            referenced_units[0] if external_entry_ids else None
                        ),
                    )
                    if center.source_kind is SourceKind.TREND_TYPE
                    else establish_center(
                        establishment_units,
                        center.structural_level,
                        center.source_kind,
                        relevant_oscillatory_ids,
                    )
                )
            except ValueError as exc:
                raise ValueError(
                    "causal center establishment cannot be replayed"
                ) from exc
            if replayed is None or replayed.center_id != center.center_id:
                raise ValueError("causal center establishment cannot be replayed")
            lifecycle_units = context_units[seed_end_offset + 1 : return_offset + 1]
            for lifecycle_unit in lifecycle_units:
                try:
                    replayed, _event = advance_center(
                        replayed,
                        lifecycle_unit,
                        relevant_oscillatory_ids,
                    )
                except ValueError as exc:
                    raise ValueError(
                        "causal center lifecycle cannot be replayed"
                    ) from exc
                if replayed.state is CenterState.COMPLETED:
                    actual_leave = replayed.completion_leave_unit
                    actual_return = replayed.completion_return_unit
                    if (
                        actual_leave is None
                        or actual_return is None
                        or actual_leave.unit_id != center.leave_unit_id
                        or actual_return.unit_id != center.return_unit_id
                    ):
                        raise ValueError(
                            "causal center completion must use its first outside return"
                        )
                    break
            if (
                replayed.state is not CenterState.COMPLETED
                or replayed.completion_leave_unit != leave
                or replayed.completion_return_unit != ret
            ):
                raise ValueError(
                    "causal center completion cannot be reproduced from its source slice"
                )
            if replayed.body_revision != center.body_revision:
                raise ValueError(
                    "causal center body revision changed during lifecycle replay"
                )
            if replayed.available_at > center.available_at:
                raise ValueError(
                    "causal center completion uses evidence not yet available"
                )
        visibility_order = tuple(
            (item.visible_from, item.point_id) for item in self.point_visibility
        )
        if visibility_order != tuple(sorted(visibility_order)):
            raise ValueError("causal point visibility must be chronological")
        visible_point_ids = {item.point_id for item in self.point_visibility}
        if visible_point_ids != point_ids:
            raise ValueError("every causal point requires a visibility interval")
        visibility_by_point: dict[str, list[PointVisibilityInterval]] = {}
        for interval in self.point_visibility:
            visibility_by_point.setdefault(interval.point_id, []).append(interval)
        for intervals in visibility_by_point.values():
            for previous, current in zip(intervals, intervals[1:]):
                if previous.visible_until is None or (
                    previous.visible_until > current.visible_from
                ):
                    raise ValueError("causal point visibility cannot overlap")


def final_confirmed_structure_events(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    *,
    visibility_windows: Sequence[tuple[datetime, datetime]] | None = None,
) -> CausalStructureEventLedger:
    """返回实时与回放路径共同使用的因果买卖点/趋势账本。"""

    return _causal_confirmed_structure_events(
        code,
        frequency,
        frame,
        visibility_windows=visibility_windows,
    )


def _causal_confirmed_points(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    *,
    visibility_windows: Sequence[tuple[datetime, datetime]] | None = None,
    recursive_level_limit: int | None = None,
) -> tuple[StructuralPoint, ...]:
    return _causal_confirmed_structure_events(
        code,
        frequency,
        frame,
        visibility_windows=visibility_windows,
        recursive_level_limit=recursive_level_limit,
        include_audit_ledger=False,
    ).points


def _causally_verified_point_available_at(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    dates: Sequence[datetime],
    point: StructuralPoint,
    checkpoint: datetime,
    occurrence_cache: dict[int, frozenset[str] | None],
) -> datetime:
    """Return the earliest production-observable time for a replay point.

    A live-tail projection evaluated at a later locked-Bi checkpoint can expose
    a geometrically completed unit whose ``formed_at`` lies much earlier.  That
    geometry time is causal only when an independent production-sized cold
    snapshot at that close already exposed the same physical market event.
    Otherwise later bars discovered the structure and replay must not backfill
    it into the past.

    Internal center/unit identities may change when the cold window's left
    boundary changes, so verification uses the stable physical occurrence
    identity rather than ``point_id``.
    """

    observed_at = normalize_datetime(checkpoint, "causal point checkpoint")
    claimed_at = normalize_datetime(point.available_at, "causal point availability")
    if claimed_at >= observed_at:
        return claimed_at
    end = bisect_right(dates, claimed_at)
    if end <= 0 or dates[end - 1] != claimed_at:
        return observed_at
    occurrences = occurrence_cache.get(end)
    if end not in occurrence_cache:
        request_bars = SCREENING_CANONICAL_REQUEST_BARS[frequency]
        minimum_bars = SCREENING_MINIMUM_BARS_BY_FREQUENCY[frequency]
        if end < minimum_bars:
            occurrences = None
        else:
            start = max(0, end - request_bars)
            prefix = frame.iloc[start:end].copy().reset_index(drop=True)
            copy_price_basis_metadata(frame, prefix)
            try:
                production_points = _production_current_points(
                    code,
                    frequency,
                    prefix,
                    as_of=dates[end - 1],
                )
            except (StrictStructureContractError, TypeError, ValueError):
                occurrences = None
            else:
                occurrences = frozenset(
                    structural_point_occurrence_id(candidate)
                    for candidate in production_points
                )
        occurrence_cache[end] = occurrences
    return (
        claimed_at
        if occurrences is not None
        and structural_point_occurrence_id(point) in occurrences
        else observed_at
    )


def _causal_confirmed_structure_events(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    *,
    visibility_windows: Sequence[tuple[datetime, datetime]] | None = None,
    recursive_level_limit: int | None = None,
    include_audit_ledger: bool = True,
) -> CausalStructureEventLedger:
    """只从已锁定笔前缀构建并返回只追加事实。

    普通的最终严格快照在这里有意不作为充分证据。线段和递归尾部计算只是当前状态投影：
    后续前缀可能替换尾部，让此前确认的事实消失。读取最终投影再按买卖点历史
    ``available_at`` 过滤，会产生幸存者偏差。

    已锁定笔是前缀稳定输入。本过程依次推进因果检查点，冻结首个完成的线段且不再重开
    其边界，并在买卖点及完成趋势证据首次可观察时记录。检查点本身限定
    ``available_at`` 的最早值，因此即使内部几何辅助函数返回更早见证时间，也不能把
    可交易事件倒填到过去。
    """

    if frame.empty or visibility_windows == ():
        return CausalStructureEventLedger(points=(), completed_trends=())
    windows = None
    if visibility_windows is not None:
        normalized = sorted(
            (
                normalize_datetime(start, "visibility_start"),
                normalize_datetime(end, "visibility_end"),
            )
            for start, end in visibility_windows
        )
        if any(start > end for start, end in normalized):
            raise ValueError("visibility window start cannot follow end")
        merged: list[tuple[datetime, datetime]] = []
        for start, end in normalized:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        windows = tuple(merged)

    def checkpoint_is_relevant(value: datetime) -> bool:
        return windows is None or any(start <= value <= end for start, end in windows)

    def checkpoint_visibility_floor(value: datetime) -> datetime | None:
        if windows is None:
            return None
        return next(
            (start for start, end in windows if start <= value <= end),
            None,
        )

    state = strict_state(code, frequency, frame)
    state.process_klines(frame)
    locked_bis = tuple(item for item in state.get_bis() if item.locked_at is not None)
    if len(locked_bis) < 3:
        return CausalStructureEventLedger(points=(), completed_trends=())

    price_quantum = Decimal(str(frame.attrs["structure_price_quantum"]))
    price_basis_revision = cast(str, frame.attrs["price_basis_revision"])
    strength = MacdStrengthProvider(state)
    available_levels = len(recursive_level_labels(frequency))
    if recursive_level_limit is None:
        max_levels = available_levels
    elif (
        type(recursive_level_limit) is not int
        or not 1 <= recursive_level_limit <= available_levels
    ):
        raise ValueError("recursive level limit is outside the frequency catalog")
    else:
        max_levels = recursive_level_limit
    recursive_engine = StrictRecursiveEngine(
        max_levels=max_levels,
        center_prefix_cache=OrderedDict(),
    )
    live_recursive_engine = StrictRecursiveEngine(
        max_levels=max_levels,
        center_prefix_cache=OrderedDict(),
    )
    unit_lock_registry = UnitLockRegistry(price_basis_revision)

    frozen_segments = []
    point_ledger: dict[str, StructuralPoint] = {}
    point_anchor_ledger: dict[str, str] = {}
    point_visibility: list[PointVisibilityInterval] = []
    active_point_starts: dict[str, datetime] = {}
    trend_ledger: dict[str, TrendType] = {}
    unit_ledger: dict[tuple[str, int], ConstituentUnit] = {}
    center_ledger: dict[tuple[str, int], CausalCenterCompletionFact] = {}

    def store_unit(unit: ConstituentUnit) -> None:
        """Keep the first live geometry until its immutable audit lock arrives."""

        key = (unit.unit_id, unit.structural_level)
        previous = unit_ledger.get(key)
        if previous is None or (unit.locked and not previous.locked):
            unit_ledger[key] = unit

    production_occurrences_by_end: dict[int, frozenset[str] | None] = {}
    frame_dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    next_segment_start: int | None = None
    # Several Bis may become locked at the same completed-bar timestamp.  That
    # close is one externally observable production snapshot, so replay only
    # evaluates the largest prefix for each timestamp and never publishes an
    # intermediate state from the same close.
    prefix_size_by_checkpoint: dict[datetime, int] = {}
    for prefix_size in range(3, len(locked_bis) + 1):
        checkpoint = cast(datetime, locked_bis[prefix_size - 1].locked_at)
        if checkpoint_is_relevant(checkpoint):
            prefix_size_by_checkpoint[checkpoint] = prefix_size
    prefix_sizes = tuple(
        prefix_size_by_checkpoint[checkpoint]
        for checkpoint in sorted(prefix_size_by_checkpoint)
    )

    for prefix_size in prefix_sizes:
        calculator = XdCalculator()
        prefix = list(locked_bis[:prefix_size])
        if next_segment_start is None:
            calculator.calculate(prefix)
        else:
            # 冻结线段是不可逆因果边界，只重建仍活动的后缀；未来笔不允许穿过已可观测点
            # 向前合并。
            calculator._build_segments(prefix, next_segment_start)
        checkpoint = locked_bis[prefix_size - 1].locked_at
        if checkpoint is None:
            raise ValueError("causal segment checkpoint must be locked")
        completed = tuple(item for item in calculator.xds if item.is_done())
        if not completed:
            continue
        for segment in completed:
            # All structural changes produced by one locked-Bi prefix are one
            # atomic close observation.  No consumer can see the transient
            # states between several segments that freeze at this timestamp.
            segment.locked_at = checkpoint
            segment.done = True
            following_start = int(segment.end_line.index) + 1
            if next_segment_start is not None and following_start <= next_segment_start:
                raise RuntimeError("causal segment replay did not advance")
            frozen_segments.append(segment)
            next_segment_start = following_start

        units = (
            adapt_lines(
                frozen_segments,
                0,
                SourceKind.SEGMENT,
                price_quantum,
                checkpoint,
                unit_lock_registry,
            )
            if include_audit_ledger
            else ()
        )
        structure = recursive_engine.calculate(
            units,
            price_basis_revision=price_basis_revision,
            strength=strength,
        )
        for level in structure.levels:
            for unit in level.units:
                if not unit.locked or unit.confirmed_at is None:
                    continue
                first_seen_unit = replace(
                    unit,
                    available_at=max(unit.available_at, checkpoint),
                )
                store_unit(first_seen_unit)
            for center in level.center_result.centers:
                leave = center.completion_leave_unit
                ret = center.completion_return_unit
                entry = center.entry_unit
                establishment_leave = center.establishment_leave_unit
                core_units = center.core_units
                establishment_units = center.establishment_units
                if (
                    center.completed_at is None
                    or leave is None
                    or ret is None
                    or not leave.locked
                    or not ret.locked
                    or leave.direction != "up"
                    or ret.direction != "down"
                ):
                    continue
                fact = CausalCenterCompletionFact(
                    center_id=center.center_id,
                    source_frequency=frequency,
                    structural_level=center.structural_level,
                    source_kind=center.source_kind,
                    price_basis_revision=center.price_basis_revision,
                    body_revision=center.body_revision,
                    available_at=max(center.available_at, checkpoint),
                    completed_at=center.completed_at,
                    zd_tick=center.zd_tick,
                    zg_tick=center.zg_tick,
                    entry_unit_id=(None if entry is None else entry.unit_id),
                    core_unit_ids=tuple(unit.unit_id for unit in core_units),
                    establishment_leave_unit_id=(
                        None
                        if establishment_leave is None
                        else establishment_leave.unit_id
                    ),
                    establishment_unit_ids=tuple(
                        unit.unit_id for unit in establishment_units
                    ),
                    leave_unit_id=leave.unit_id,
                    leave_direction=leave.direction,
                    leave_market_start=leave.market_start,
                    leave_market_end=leave.market_end,
                    leave_available_at=leave.available_at,
                    leave_start_tick=leave.start_tick,
                    leave_end_tick=leave.end_tick,
                    leave_low_tick=leave.low_tick,
                    leave_high_tick=leave.high_tick,
                    return_unit_id=ret.unit_id,
                    return_direction=ret.direction,
                    return_market_start=ret.market_start,
                    return_market_end=ret.market_end,
                    return_available_at=ret.available_at,
                    return_start_tick=ret.start_tick,
                    return_end_tick=ret.end_tick,
                    return_low_tick=ret.low_tick,
                    return_high_tick=ret.high_tick,
                )
                center_ledger.setdefault((fact.center_id, fact.structural_level), fact)
        for level in structure.levels:
            for trend in level.completed_trends:
                first_seen_trend = replace(
                    trend,
                    available_at=max(trend.available_at, checkpoint),
                )
                trend_ledger.setdefault(
                    first_seen_trend.trend_id,
                    first_seen_trend,
                )
        strict_config_revision = cast(
            str,
            state.get_config()["strict_config_revision"],
        )
        # The append-only ledgers above intentionally use audit-locked units.
        # The production trading view does not: it also promotes a point on the
        # latest geometrically completed segment before the later audit lock.
        # Rebuild the exact live-tail projection from this causal Bi prefix and
        # pass it through the same adapter used by screening.
        live_calculator = XdCalculator()
        live_calculator.calculate(prefix)
        live_units = adapt_lines(
            live_calculator.xds,
            0,
            SourceKind.SEGMENT,
            price_quantum,
            checkpoint,
            # Each historical close is an independent cold production
            # projection.  Cross-snapshot identity is enforced by the causal
            # point ledger below; the per-CL unit registry is intentionally not
            # shared between cold snapshots.
            UnitLockRegistry(price_basis_revision),
        )
        live_structure = live_recursive_engine.calculate(
            live_units,
            price_basis_revision=price_basis_revision,
            strength=strength,
        )
        live_assembler = StrictEvidenceAssembler(
            symbol=code,
            source_frequency=frequency,
            source_closed_at=checkpoint,
            price_basis_revision=price_basis_revision,
            structure_price_quantum=price_quantum,
            strict_config_revision=strict_config_revision,
            structure=live_structure,
            strength=strength,
            projection_cache=live_recursive_engine.center_prefix_cache,
        )
        needs_geometry_projection = any(
            target is not None and not target.locked
            for level in live_structure.levels
            for target in (
                next(
                    (
                        unit
                        for unit in reversed(level.units)
                        if not unit.forming
                        and (unit.locked or unit.formed_at is not None)
                    ),
                    None,
                ),
            )
        )
        current_points = convert_current_confirmed_point_evidence(
            live_structure,
            confirmed_points=live_assembler.confirmed_points(),
            # An approaching point can become operational only when it belongs
            # to a geometrically completed, not-yet-audit-locked terminal unit.
            # Forming-tail previews are always rejected by the shared adapter,
            # so constructing them at every locked-Bi checkpoint is pure waste.
            approaching_points=(
                live_assembler.approaching_points() if needs_geometry_projection else ()
            ),
            code=code,
            source_frequency=frequency,
            as_of=checkpoint,
        )
        current_by_id = {point.point_id: point for point in current_points}
        if len(current_by_id) != len(current_points):
            raise ValueError("current backtest point identities are not unique")
        live_units_by_key = {
            (unit.structural_level, unit.unit_id): unit
            for level in live_structure.levels
            for unit in level.units
        }

        # Persist the exact current-state transitions.  Production consumes
        # the strict tail projection, so replay must not approximate this
        # state with an age calculated from a potentially old anchor.
        visible_current_ids = set(current_by_id)
        for point_id in tuple(active_point_starts):
            if point_id in visible_current_ids:
                continue
            visible_from = active_point_starts.pop(point_id)
            point_visibility.append(
                PointVisibilityInterval(
                    point_id=point_id,
                    visible_from=visible_from,
                    visible_until=checkpoint,
                )
            )
        for point_id, point in current_by_id.items():
            reference = point.terminal_segment
            if reference is None:
                raise ValueError("current backtest point lost terminal segment lineage")
            anchor_unit = live_units_by_key.get(
                (reference.structural_level, reference.unit_id)
            )
            if anchor_unit is None or anchor_unit.forming:
                raise ValueError(
                    "operational point anchor is not a completed live unit"
                )
            store_unit(
                replace(
                    anchor_unit,
                    available_at=max(anchor_unit.available_at, checkpoint),
                )
            )
            first_observation = point_id not in point_ledger
            if first_observation:
                # Preserve an earlier geometry clock only when the same
                # physical event is independently observable from the exact
                # production cold window at that close.  This keeps real 1m
                # segment-difference facts at their precise minute while preventing a
                # structure discovered by future bars from being backfilled.
                first_visible_at = _causally_verified_point_available_at(
                    code=code,
                    frequency=frequency,
                    frame=frame,
                    dates=frame_dates,
                    point=point,
                    checkpoint=checkpoint,
                    occurrence_cache=production_occurrences_by_end,
                )
                point_ledger[point_id] = (
                    point
                    if first_visible_at == point.available_at
                    else replace(point, available_at=first_visible_at)
                )
            previous_anchor = point_anchor_ledger.setdefault(
                point_id,
                reference.unit_id,
            )
            if previous_anchor != reference.unit_id:
                # A still-forming terminal segment may refine its market start
                # and receive a new internal unit id while retaining the same
                # formal point, center and terminal market endpoint.  Keep the
                # first causal witness in the append-only ledger, but reject a
                # change to any immutable operation semantics.
                if _operation_point_identity_signature(
                    point_ledger[point_id]
                ) != _operation_point_identity_signature(point):
                    raise ValueError("causal point semantics changed across prefixes")
            if point_id not in active_point_starts:
                visibility_floor = checkpoint_visibility_floor(checkpoint)
                stored_point = point_ledger[point_id]
                active_point_starts[point_id] = (
                    max(stored_point.available_at, visibility_floor)
                    if first_observation and visibility_floor is not None
                    else stored_point.available_at
                    if first_observation
                    else checkpoint
                )

    point_visibility.extend(
        PointVisibilityInterval(
            point_id=point_id,
            visible_from=visible_from,
        )
        for point_id, visible_from in active_point_starts.items()
    )

    return CausalStructureEventLedger(
        points=tuple(
            sorted(
                point_ledger.values(),
                key=lambda point: (point.available_at, point.point_id),
            )
        ),
        completed_trends=tuple(
            sorted(
                trend_ledger.values(),
                key=lambda trend: (trend.available_at, trend.trend_id),
            )
        ),
        completed_units=tuple(
            sorted(
                unit_ledger.values(),
                key=lambda item: (item.available_at, item.unit_id),
            )
        ),
        center_completions=tuple(
            sorted(
                center_ledger.values(),
                key=lambda item: (item.available_at, item.center_id),
            )
        ),
        point_anchor_unit_ids=tuple(sorted(point_anchor_ledger.items())),
        point_visibility=tuple(
            sorted(
                point_visibility,
                key=lambda item: (item.visible_from, item.point_id),
            )
        ),
    )


def _point_lane(point: StructuralPoint) -> tuple[str, str, int]:
    return five_minute_setup_family_lane(point)


def setup_active_ends(
    points: Sequence[StructuralPoint],
) -> dict[str, tuple[datetime, bool]]:
    """返回每个形态的包含式结束时间，以及它是否已被更新通道取代。"""

    ordered = tuple(
        sorted(points, key=lambda point: (point.available_at, point.point_id))
    )
    next_by_lane: dict[tuple[str, str, int], datetime] = {}
    output: dict[str, tuple[datetime, bool]] = {}
    for point in reversed(ordered):
        lane = _point_lane(point)
        expiry = five_minute_setup_expires_at(
            point,
            max_setup_age_seconds=MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
        )
        following = next_by_lane.get(lane)
        if following is not None and following <= expiry:
            output[point.point_id] = (following, True)
        else:
            output[point.point_id] = (expiry, False)
        next_by_lane[lane] = point.available_at
    return output


def first_matching_segment_difference(
    setup: StructuralPoint,
    one_points: Sequence[StructuralPoint],
    *,
    active_end: datetime,
    end_exclusive: bool,
) -> StructuralPoint | None:
    if setup.source_frequency != "5m" or not setup.confirmed:
        raise ValueError("setup must be a confirmed 5m point")
    boundary = normalize_datetime(active_end, "active_end")
    visible = tuple(
        point
        for point in one_points
        if (
            point.available_at < boundary
            if end_exclusive
            else point.available_at <= boundary
        )
    )
    return match_one_minute_nesting_witness_for_point(
        setup,
        visible,
        as_of=boundary,
    )


def _one_minute_visibility_windows(
    setups: Sequence[StructuralPoint],
    active_ends: Mapping[str, tuple[datetime, bool]] | None = None,
    *,
    end_at: datetime,
    point_visibility: Sequence[PointVisibilityInterval] = (),
) -> tuple[tuple[datetime, datetime], ...]:
    """Return causal 1m replay windows covering each complete 5m terminal leg."""

    return tuple(
        (window.start, window.end)
        for window in _one_minute_replay_windows(
            setups,
            active_ends,
            end_at=end_at,
            point_visibility=point_visibility,
        )
    )


@dataclass(frozen=True, slots=True)
class _OneMinuteReplayWindow:
    """A bounded 1m replay epoch and its causal right-edge state.

    ``close_at`` is the first instant outside the active epoch.  It is the
    exact endpoint when a newer 5m state supersedes the setup, one microsecond
    after an inclusive execution expiry, and ``None`` when the research
    request itself right-censors the epoch.
    """

    start: datetime
    end: datetime
    close_at: datetime | None

    def __post_init__(self) -> None:
        start = normalize_datetime(self.start, "1m replay window start")
        end = normalize_datetime(self.end, "1m replay window end")
        close_at = (
            None
            if self.close_at is None
            else normalize_datetime(self.close_at, "1m replay window close")
        )
        if start > end:
            raise ValueError("1m replay window start cannot follow end")
        if close_at is not None and close_at < end:
            raise ValueError("1m replay window cannot close before its data end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "close_at", close_at)


def _one_minute_replay_windows(
    setups: Sequence[StructuralPoint],
    active_ends: Mapping[str, tuple[datetime, bool]] | None = None,
    *,
    end_at: datetime,
    point_visibility: Sequence[PointVisibilityInterval] = (),
) -> tuple[_OneMinuteReplayWindow, ...]:
    """Return 1m epochs without collapsing inclusive and exclusive ends."""

    replay_end = normalize_datetime(end_at, "one minute visibility end")
    output: list[_OneMinuteReplayWindow] = []
    visibility_by_point: dict[str, list[PointVisibilityInterval]] = {}
    for interval in point_visibility:
        visibility_by_point.setdefault(interval.point_id, []).append(interval)
    for setup in setups:
        setup_start = five_minute_segment_difference_window_start(setup)
        expiry = five_minute_setup_expires_at(setup)
        if point_visibility:
            active_windows = tuple(
                (
                    setup_start,
                    min(interval.visible_until or expiry, expiry, replay_end),
                    (
                        min(interval.visible_until, expiry, replay_end)
                        if interval.visible_until is not None
                        and interval.visible_until <= expiry
                        and interval.visible_until <= replay_end
                        else min(expiry, replay_end) + timedelta(microseconds=1)
                        if expiry <= replay_end
                        else None
                    ),
                )
                for interval in visibility_by_point.get(setup.point_id, ())
                if interval.visible_from <= min(expiry, replay_end)
                and (
                    interval.visible_until is None
                    or interval.visible_until >= setup.available_at
                )
            )
        else:
            if active_ends is None:
                raise ValueError("legacy setup windows require active ends")
            active_end, end_exclusive = active_ends[setup.point_id]
            bounded_end = min(active_end, expiry, replay_end)
            active_windows = (
                (
                    setup_start,
                    bounded_end,
                    (
                        bounded_end
                        if end_exclusive
                        and active_end <= expiry
                        and active_end <= replay_end
                        else bounded_end + timedelta(microseconds=1)
                        if min(active_end, expiry) <= replay_end
                        else None
                    ),
                ),
            )
        for window_start, bounded_end, close_at in active_windows:
            end_is_exclusive = close_at == bounded_end
            if (
                setup.available_at >= bounded_end
                if end_is_exclusive
                else setup.available_at > bounded_end
            ) or (
                window_start >= bounded_end
                if end_is_exclusive
                else window_start > bounded_end
            ):
                continue
            output.append(
                _OneMinuteReplayWindow(
                    start=window_start,
                    end=bounded_end,
                    close_at=close_at,
                )
            )
    return tuple(output)


def _five_minute_replay_windows(
    points: Sequence[StructuralPoint],
    point_visibility: Sequence[PointVisibilityInterval],
) -> dict[str, tuple[tuple[datetime, datetime | None, bool], ...]]:
    """Return setup-current windows, retaining an explicit legacy fallback."""

    if point_visibility:
        point_ids = {point.point_id for point in points}
        if any(interval.point_id not in point_ids for interval in point_visibility):
            raise ValueError("5m replay visibility references an unknown point")
        output: dict[str, list[tuple[datetime, datetime | None, bool]]] = {
            point_id: [] for point_id in point_ids
        }
        for interval in point_visibility:
            output[interval.point_id].append(
                (interval.visible_from, interval.visible_until, True)
            )
        return {point_id: tuple(windows) for point_id, windows in output.items()}

    active_ends = setup_active_ends(points)
    return {
        point.point_id: (
            (
                point.available_at,
                active_ends[point.point_id][0],
                active_ends[point.point_id][1],
            ),
        )
        for point in points
    }


def _causal_one_minute_events_by_windows(
    code: str,
    frame: pd.DataFrame,
    visibility_windows: Sequence[tuple[datetime, datetime] | _OneMinuteReplayWindow],
) -> tuple[tuple[StructuralPoint, ...], tuple[PointVisibilityInterval, ...]]:
    """Replay each active 5m terminal epoch from production-sized cold history.

    The live gateway cold-starts 1m analysis with 12,000 completed bars and
    retains that stable left anchor while a setup stays in the priority lane.
    A symbol that leaves the lane is not entitled to keep an unbounded annual
    runtime in the small production cache.  Replaying each disjoint active
    epoch with the same bounded prefix is both deterministic and faithful to
    that operational contract.
    """

    if frame.empty or not visibility_windows:
        return (), ()
    legacy_end = max(
        (
            window.end
            if isinstance(window, _OneMinuteReplayWindow)
            else normalize_datetime(window[1], "1m replay window end")
        )
        for window in visibility_windows
    )
    normalized = sorted(
        [
            window
            if isinstance(window, _OneMinuteReplayWindow)
            else _OneMinuteReplayWindow(
                start=window[0],
                end=window[1],
                close_at=(
                    None
                    if normalize_datetime(window[1], "1m replay window end")
                    == legacy_end
                    else normalize_datetime(window[1], "1m replay window end")
                ),
            )
            for window in visibility_windows
        ],
        key=lambda window: (window.start, window.end),
    )
    merged: list[_OneMinuteReplayWindow] = []
    for window in normalized:
        if not merged or window.start > merged[-1].end:
            merged.append(window)
            continue
        previous = merged[-1]
        if window.end < previous.end:
            continue
        if window.end > previous.end:
            merged[-1] = _OneMinuteReplayWindow(
                start=previous.start,
                end=window.end,
                close_at=window.close_at,
            )
        else:
            merged[-1] = _OneMinuteReplayWindow(
                start=previous.start,
                end=previous.end,
                close_at=(
                    None
                    if previous.close_at is None or window.close_at is None
                    else max(previous.close_at, window.close_at)
                ),
            )

    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    history_bars = SCREENING_CANONICAL_REQUEST_BARS["1m"]
    points_by_id: dict[str, StructuralPoint] = {}
    point_visibility: list[PointVisibilityInterval] = []
    for window in merged:
        start, end = window.start, window.end
        first = bisect_right(dates, start - timedelta(microseconds=1))
        stop = bisect_right(dates, end)
        if first >= len(dates) or stop <= first:
            continue
        chunk = frame.iloc[max(0, first - history_bars) : stop].copy()
        chunk = chunk.reset_index(drop=True)
        copy_price_basis_metadata(frame, chunk)
        ledger = _causal_confirmed_structure_events(
            code,
            "1m",
            chunk,
            visibility_windows=((start, end),),
            recursive_level_limit=1,
            include_audit_ledger=False,
        )
        # The window begins at the parent 5m terminal-segment start.  A 1m
        # witness must itself become causally visible inside that market epoch;
        # exact full-interval containment is enforced later by the shared
        # matcher.  This filter also prevents a truncated future prefix from
        # backfilling older unrelated audit points.
        eligible_points = {
            point.point_id: point
            for point in ledger.points
            if start <= point.available_at <= end
        }
        for point in eligible_points.values():
            previous = points_by_id.setdefault(point.point_id, point)
            if _operation_point_identity_signature(previous) != (
                _operation_point_identity_signature(point)
            ):
                raise ValueError("1m replay point changed across active epochs")
        for interval in ledger.point_visibility:
            if interval.point_id not in eligible_points:
                continue
            visible_until = interval.visible_until
            if window.close_at is not None and (
                visible_until is None or visible_until > window.close_at
            ):
                visible_until = window.close_at
            if visible_until is not None and visible_until <= interval.visible_from:
                continue
            point_visibility.append(replace(interval, visible_until=visible_until))
    visibility_by_id: dict[str, list[PointVisibilityInterval]] = {}
    for interval in sorted(
        point_visibility,
        key=lambda value: (value.point_id, value.visible_from),
    ):
        merged = visibility_by_id.setdefault(interval.point_id, [])
        if not merged:
            merged.append(interval)
            continue
        previous = merged[-1]
        if previous.visible_until is None:
            continue
        if previous.visible_until < interval.visible_from:
            merged.append(interval)
            continue
        merged[-1] = PointVisibilityInterval(
            point_id=interval.point_id,
            visible_from=previous.visible_from,
            visible_until=(
                None
                if interval.visible_until is None
                else max(previous.visible_until, interval.visible_until)
            ),
        )
    merged_visibility = [
        interval for intervals in visibility_by_id.values() for interval in intervals
    ]
    visible_point_ids = {interval.point_id for interval in merged_visibility}
    return tuple(
        sorted(
            (
                point
                for point_id, point in points_by_id.items()
                if point_id in visible_point_ids
            ),
            key=lambda point: (point.available_at, point.point_id),
        )
    ), tuple(
        sorted(
            merged_visibility,
            key=lambda interval: (interval.visible_from, interval.point_id),
        )
    )


def _operation_point_identity_signature(point: StructuralPoint) -> tuple[object, ...]:
    """Return fields that cannot change when one operation event is reloaded.

    Audit locking may add evidence codes, advance ``formed`` to ``locked``, or
    refine the start and internal id of the active terminal segment.  Its
    formal point id, market endpoint, operation class, price evidence and graph
    links remain immutable.  Confirmation/availability clocks are state
    observations and the append-only ledger retains their first causal value.
    """

    terminal = point.terminal_segment
    terminal_geometry = (
        None
        if terminal is None
        else (
            terminal.role,
            terminal.structural_level,
            terminal.source_kind,
            terminal.direction,
            terminal.market_end,
        )
    )
    carrier_geometry = tuple(
        ("terminal_segment", terminal_geometry)
        if terminal is not None and unit_id == terminal.unit_id
        else unit_id
        for unit_id in point.small_to_large_carrier_unit_ids
    )
    return (
        point.point_id,
        point.code,
        point.point_type,
        point.side,
        point.status,
        point.variant,
        point.source_frequency,
        point.price_basis_revision,
        point.tower,
        point.recursive_level,
        point.anchor_at,
        point.structure_anchor_price,
        point.structure_invalidation_price,
        point.center_id,
        point.center_zd,
        point.center_zg,
        point.center_ordinal,
        point.divergence_kind,
        point.parent_point_id,
        point.related_point_ids,
        carrier_geometry,
        terminal_geometry,
    )


def _causal_one_minute_points_by_windows(
    code: str,
    frame: pd.DataFrame,
    visibility_windows: Sequence[tuple[datetime, datetime]],
) -> tuple[StructuralPoint, ...]:
    """Project the causal ledger to 1m segment-difference point events."""

    points, _visibility = _causal_one_minute_events_by_windows(
        code,
        frame,
        visibility_windows,
    )
    return points


def sparse_evaluation_times(
    *,
    five_points: Sequence[StructuralPoint],
    one_points: Sequence[StructuralPoint],
    thirty_closes: Sequence[datetime],
    one_closes: Sequence[datetime],
    effective_start: datetime,
    requested_end: datetime,
    five_point_visibility: Sequence[PointVisibilityInterval] = (),
) -> tuple[datetime, ...]:
    """构建 5 分钟正式点可用后可能发生变化的唯一时间点集合。

    1 分钟结构点只作为段差证据，不能决定一条 5 分钟信号是否进入回放。
    ``one_points`` 参数为旧事实档案兼容而保留；执行仍落在下一根可见 1 分钟柱。
    """

    start = normalize_datetime(effective_start, "effective_start")
    end = normalize_datetime(requested_end, "requested_end")
    if start > end:
        raise ValueError("effective_start cannot follow requested_end")
    one_dates = tuple(
        sorted(normalize_datetime(value, "one_close") for value in one_closes)
    )
    thirty_dates = tuple(
        sorted(normalize_datetime(value, "thirty_close") for value in thirty_closes)
    )
    replay_windows = _five_minute_replay_windows(
        five_points,
        five_point_visibility,
    )
    output: set[datetime] = set()
    for setup in five_points:
        for visible_from, raw_active_end, raw_end_exclusive in replay_windows[
            setup.point_id
        ]:
            active_end = min(
                raw_active_end or end,
                five_minute_setup_expires_at(setup),
                end,
            )
            end_exclusive = raw_end_exclusive and (
                raw_active_end is not None and raw_active_end == active_end
            )
            if (
                active_end < start
                or setup.available_at > end
                or setup.available_at > active_end
            ):
                continue
            first_at = max(setup.available_at, visible_from, start)
            position = bisect_right(
                one_dates,
                first_at - timedelta(microseconds=1),
            )
            if position >= len(one_dates):
                continue
            first_bar = one_dates[position]
            if first_bar > end or (
                first_bar >= active_end if end_exclusive else first_bar > active_end
            ):
                continue
            output.add(first_bar)
            witness = match_one_minute_nesting_witness_for_point(
                setup,
                tuple(one_points),
                as_of=active_end,
            )
            if witness is not None:
                jointly_known_at = _segment_difference_jointly_known_at(
                    setup,
                    witness,
                )
                if (
                    jointly_known_at is not None
                    and first_bar <= jointly_known_at <= end
                    and jointly_known_at in one_dates
                    and jointly_known_at <= active_end
                    and not (end_exclusive and jointly_known_at >= active_end)
                ):
                    output.add(jointly_known_at)
            for observed_at in thirty_dates:
                if observed_at <= first_bar or observed_at > end:
                    continue
                if observed_at > active_end or (
                    end_exclusive and observed_at >= active_end
                ):
                    break
                output.add(observed_at)
    return tuple(sorted(output))


def causal_directions(
    code: str,
    frame: pd.DataFrame,
    observed_times: Sequence[datetime],
    *,
    frequency: str = "30m",
) -> tuple[tuple[datetime, ContextDirection], int]:
    if not observed_times:
        return (), 0
    if frequency not in {"d", "30m"}:
        raise ValueError("causal context direction requires d or 30m")
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    state = strict_state(code, frequency, frame)
    cursor = 0
    output: list[tuple[datetime, ContextDirection]] = []
    unavailable = 0
    for raw_time in sorted(set(observed_times)):
        observed_at = normalize_datetime(raw_time, "observed_at")
        end = bisect_right(dates, observed_at)
        if end > cursor:
            chunk = frame.iloc[cursor:end].copy().reset_index(drop=True)
            copy_price_basis_metadata(frame, chunk)
            # ``chunk`` is a monotonic slice of the same immutable frame and
            # ``cursor`` proves that every prior row is unchanged.  Let the
            # HTF calculators consume only the revised tail/new rows instead
            # of revalidating the complete prefix at every evaluation point.
            state.process_validated_incremental_klines(chunk)
            cursor = end
        if cursor == 0:
            output.append((observed_at, "neutral"))
            unavailable += 1
            continue
        try:
            direction = cast(
                ContextDirection,
                current_formal_direction_from_components(
                    structure=state.get_strict_structure_levels(),
                    confirmed_points=state.get_strict_points(),
                    source_closed_at=dates[cursor - 1],
                ),
            )
            output.append((observed_at, direction))
        except (StrictStructureContractError, TypeError, ValueError):
            output.append((observed_at, "neutral"))
            unavailable += 1
    return tuple(output), unavailable


def _production_current_points(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    *,
    as_of: datetime,
) -> tuple[StructuralPoint, ...]:
    evidence = screening_evidence_from_frame(
        code=code,
        frequency=frequency,
        frame=frame,
        as_of=as_of,
        market="a",
    )
    return convert_current_confirmed_point_evidence(
        evidence.structure,
        confirmed_points=evidence.confirmed_points,
        approaching_points=evidence.approaching_points,
        code=code,
        source_frequency=frequency,
        as_of=as_of,
    )


def _decision_snapshot_point_ledger(
    snapshots: Sequence[tuple[datetime, tuple[StructuralPoint, ...]]],
) -> tuple[tuple[StructuralPoint, ...], tuple[PointVisibilityInterval, ...]]:
    """Freeze exact current-point membership only at replay decision times.

    Daily production analysis is a cold, bounded-window calculation.  It cannot
    be represented by one ever-growing ``CL`` instance without changing the
    left boundary that production actually saw.  This ledger records the exact
    current set at every sparse decision while preserving half-open visibility
    intervals for the shared bundle adapter.
    """

    ordered = tuple(sorted(snapshots, key=lambda row: row[0]))
    times = tuple(row[0] for row in ordered)
    if times != tuple(sorted(set(times))):
        raise ValueError("decision point snapshots must be unique and chronological")
    points_by_id: dict[str, StructuralPoint] = {}
    active_from: dict[str, datetime] = {}
    visibility: list[PointVisibilityInterval] = []
    for observed_at, points in ordered:
        current = {point.point_id: point for point in points}
        if len(current) != len(points):
            raise ValueError("decision point snapshot contains duplicate identities")
        for point_id in tuple(active_from):
            if point_id in current:
                continue
            visibility.append(
                PointVisibilityInterval(
                    point_id=point_id,
                    visible_from=active_from.pop(point_id),
                    visible_until=observed_at,
                )
            )
        for point_id, point in current.items():
            if point.available_at > observed_at:
                raise ValueError("decision snapshot contains future point evidence")
            previous = points_by_id.setdefault(point_id, point)
            if _operation_point_identity_signature(previous) != (
                _operation_point_identity_signature(point)
            ):
                raise ValueError("current point identity changed across decisions")
            active_from.setdefault(point_id, observed_at)
    visibility.extend(
        PointVisibilityInterval(point_id=point_id, visible_from=visible_from)
        for point_id, visible_from in active_from.items()
    )
    return (
        tuple(
            sorted(
                points_by_id.values(),
                key=lambda point: (point.available_at, point.point_id),
            )
        ),
        tuple(
            sorted(
                visibility,
                key=lambda interval: (interval.visible_from, interval.point_id),
            )
        ),
    )


def _production_context_snapshots(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    observed_times: Sequence[datetime],
    *,
    request_bars: int,
    minimum_bars: int,
) -> tuple[
    tuple[tuple[datetime, ContextDirection], ...],
    tuple[StructuralPoint, ...],
    tuple[PointVisibilityInterval, ...],
    tuple[datetime, ...],
]:
    """Rebuild the exact cold production window at each distinct source close."""

    if request_bars <= 0 or minimum_bars <= 0 or minimum_bars > request_bars:
        raise ValueError("production context bar budgets are invalid")
    if not observed_times:
        return (), (), (), ()
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    cache: dict[
        int,
        tuple[ContextDirection, tuple[StructuralPoint, ...]] | None,
    ] = {}
    directions: list[tuple[datetime, ContextDirection]] = []
    snapshots: list[tuple[datetime, tuple[StructuralPoint, ...]]] = []
    unavailable: list[datetime] = []
    for raw_time in sorted(set(observed_times)):
        observed_at = normalize_datetime(raw_time, "context observation")
        end = bisect_right(dates, observed_at)
        measurement = cache.get(end)
        if end not in cache:
            if end < minimum_bars:
                measurement = None
            else:
                start = max(0, end - request_bars)
                prefix = frame.iloc[start:end].copy().reset_index(drop=True)
                copy_price_basis_metadata(frame, prefix)
                source_closed_at = dates[end - 1]
                try:
                    evidence = screening_evidence_from_frame(
                        code=code,
                        frequency=frequency,
                        frame=prefix,
                        as_of=source_closed_at,
                        market="a",
                    )
                    points = convert_current_confirmed_point_evidence(
                        evidence.structure,
                        confirmed_points=evidence.confirmed_points,
                        approaching_points=evidence.approaching_points,
                        code=code,
                        source_frequency=frequency,
                        as_of=source_closed_at,
                    )
                    direction = cast(
                        ContextDirection,
                        current_formal_direction(evidence),
                    )
                    measurement = (direction, points)
                except (
                    StrictStructureContractError,
                    TypeError,
                    ValueError,
                ):
                    measurement = None
            cache[end] = measurement
        if measurement is None:
            unavailable.append(observed_at)
            continue
        direction, points = measurement
        directions.append((observed_at, direction))
        snapshots.append((observed_at, points))
    point_ledger, visibility = _decision_snapshot_point_ledger(snapshots)
    return (
        tuple(directions),
        point_ledger,
        visibility,
        tuple(unavailable),
    )


def _same_period_technical_context_snapshots(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    observed_times: Sequence[datetime],
    *,
    request_bars: int,
    minimum_bars: int,
) -> dict[datetime, SamePeriodTechnicalContext]:
    """Freeze the live MA/fractal/pen context at sparse entry boundaries."""

    if frequency not in {"d", "30m"}:
        raise ValueError("same-period snapshots require d or 30m")
    if request_bars <= 0 or minimum_bars <= 0 or minimum_bars > request_bars:
        raise ValueError("same-period context bar budgets are invalid")
    if not observed_times:
        return {}
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    cache: dict[int, SamePeriodTechnicalContext | None] = {}
    output: dict[datetime, SamePeriodTechnicalContext] = {}
    for raw_time in sorted(set(observed_times)):
        observed_at = normalize_datetime(raw_time, "technical context observation")
        end = bisect_right(dates, observed_at)
        base = cache.get(end)
        if end not in cache:
            if end < minimum_bars:
                base = None
            else:
                start = max(0, end - request_bars)
                prefix = frame.iloc[start:end].copy().reset_index(drop=True)
                copy_price_basis_metadata(frame, prefix)
                source_closed_at = dates[end - 1]
                try:
                    runtime = ScreeningRuntimeState(
                        code=code,
                        frequency=frequency,
                        market="a",
                    )
                    update = runtime.update_from_frame(
                        frame=prefix,
                        as_of=source_closed_at,
                    )
                    base = build_same_period_technical_context(
                        frequency=frequency,
                        frame=prefix,
                        cl_state=update.state,
                        as_of=source_closed_at,
                    )
                except (
                    StrictStructureContractError,
                    TypeError,
                    ValueError,
                ):
                    base = None
            cache[end] = base
        if base is not None:
            output[observed_at] = replace(base, observed_at=observed_at)
    return output


class _HistoricalQmtFrameExchange:
    """Read-only causal frame source matching the QMT gateway's row contract."""

    supports_stable_incremental_window = False

    def __init__(
        self,
        frames: Mapping[tuple[str, str], pd.DataFrame],
    ) -> None:
        self._frames = dict(frames)

    def klines(
        self,
        code: str,
        frequency: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        args: Mapping[str, object] | None = None,
    ) -> pd.DataFrame:
        try:
            source = self._frames[(code, frequency)]
        except KeyError as exc:
            raise ValueError(
                f"historical QMT frame is unavailable: {code}/{frequency}"
            ) from exc
        dates = pd.to_datetime(source["date"], errors="raise")
        if dates.dt.tz is None:
            raise ValueError("historical QMT frame must be timezone-aware")
        mask = pd.Series(True, index=source.index)
        for raw, lower in ((start_date, True), (end_date, False)):
            if raw is None:
                continue
            boundary = pd.Timestamp(raw)
            if boundary.tzinfo is None:
                boundary = boundary.tz_localize(CN)
            else:
                boundary = boundary.tz_convert(CN)
            mask &= dates >= boundary if lower else dates <= boundary
        result = source.loc[mask].copy().reset_index(drop=True)
        request = dict(args or {}).get("req_counts")
        if request is not None:
            if type(request) is not int or request <= 0:
                raise ValueError("historical QMT req_counts must be positive")
            result = result.iloc[-request:].copy().reset_index(drop=True)
        result.attrs = dict(source.attrs)
        return result


@lru_cache(maxsize=8)
def _historical_benchmark_frames(
    start_at: datetime,
    end_at: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    empty_factors = pd.DataFrame()
    return (
        load_qmt_frame(
            "SH.000300",
            "1m",
            start_at=start_at,
            end_at=end_at,
            factors=empty_factors,
        ),
        load_qmt_daily_frame(
            "SH.000300",
            start_at=start_at,
            end_at=end_at,
            factors=empty_factors,
        ),
    )


def _historical_higher_timeframe_gates(
    *,
    code: str,
    one_minute_frame: pd.DataFrame,
    daily_frame: pd.DataFrame,
    observed_times: Sequence[datetime],
    sector_by_time: Mapping[datetime, str | None],
) -> dict[datetime, HigherTimeframeGateBundle]:
    """Build production M/W/D integrity evidence at executable buy boundaries.

    The live provider uses today's front-adjusted vendor history.  Historical
    replay instead supplies point-in-time factor-adjusted prefixes through the
    same provider so post-decision corporate actions cannot leak backwards.
    Sector M/W/D remains advisory; the PIT sector gate used by the strategy is
    replayed separately by ``SectorResearchFacts``.
    """

    ordered = tuple(sorted(set(observed_times)))
    if not ordered:
        return {}
    earliest = ordered[0] - timedelta(days=365)
    latest = ordered[-1]
    benchmark_start = datetime(earliest.year, 1, 1, tzinfo=CN)
    benchmark_end = datetime(latest.year, 12, 31, 15, 0, tzinfo=CN)
    benchmark_one, benchmark_daily = _historical_benchmark_frames(
        benchmark_start,
        benchmark_end,
    )
    exchange = _HistoricalQmtFrameExchange(
        {
            ("SH.000300", "1m"): benchmark_one,
            ("SH.000300", "d"): benchmark_daily,
            (code, "1m"): one_minute_frame,
            (code, "d"): daily_frame,
        }
    )
    # Import lazily: unit tests and non-QMT callers can use the fixed-year data
    # types without requiring a running vendor calendar service.
    from chanlun.exchange.qmt_screening_sector_source import qmt_trading_sessions

    provider = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: exchange,
        sector_frame_provider=None,
        trading_calendar_provider=qmt_trading_sessions,
        refresh_stale_benchmark=False,
    )
    output: dict[datetime, HigherTimeframeGateBundle] = {}
    for observed_at in ordered:
        sector_id = sector_by_time.get(observed_at)
        try:
            output[observed_at] = provider.gates(
                symbol=code,
                as_of=observed_at,
                sector_id=sector_id,
            )
        except Exception:
            output[observed_at] = unresolved_higher_timeframe_gates(
                symbol=code,
                observed_at=observed_at,
                reason_code="QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
                sector_subject=sector_id,
            )
    return output


def _five_minute_warmup_facts(
    code: str,
    frame: pd.DataFrame,
    one_minute_frame: pd.DataFrame,
    observed_times: Sequence[datetime],
) -> tuple[FiveMinuteWarmupFact, ...]:
    """Freeze exact production 5m/1m evidence at each joint-knowledge close."""

    if frame.empty or not observed_times:
        return ()
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    one_dates = tuple(
        pd.Timestamp(value).to_pydatetime()
        for value in one_minute_frame.get("date", ())
    )
    request_bars = SCREENING_CANONICAL_REQUEST_BARS["5m"]
    required_bars = SCREENING_WARMUP_REQUIRED_BARS["5m"]
    minimum_bars = SCREENING_MINIMUM_BARS_BY_FREQUENCY["5m"]
    measurements: dict[
        datetime,
        tuple[
            bool,
            int,
            int,
            str,
            tuple[str, ...],
            tuple[StructuralPoint, ...],
        ],
    ] = {}
    output: list[FiveMinuteWarmupFact] = []
    for raw_time in sorted(set(observed_times)):
        observed_at = normalize_datetime(raw_time, "warmup decision time")
        end = bisect_right(dates, observed_at)
        if end == 0:
            continue
        source_closed_at = dates[end - 1]
        measurement = measurements.get(source_closed_at)
        if measurement is None:
            start = max(0, end - request_bars)
            full = frame.iloc[start:end].copy().reset_index(drop=True)
            copy_price_basis_metadata(frame, full)
            full_count = len(full)
            full_points = (
                _production_current_points(
                    code,
                    "5m",
                    full,
                    as_of=source_closed_at,
                )
                if full_count >= minimum_bars
                else ()
            )
            if full_count < required_bars:
                measurement = (
                    False,
                    full_count,
                    0,
                    "WARMUP_HISTORY_INSUFFICIENT",
                    (),
                    full_points,
                )
            else:
                trim = full_count // 3
                suffix = full.iloc[trim:].copy().reset_index(drop=True)
                copy_price_basis_metadata(full, suffix)
                active_tail_start = max(
                    pd.Timestamp(suffix.iloc[0]["date"]).to_pydatetime(),
                    source_closed_at - context_point_max_age("5m"),
                )
                suffix_points = _production_current_points(
                    code,
                    "5m",
                    suffix,
                    as_of=source_closed_at,
                )
                full_signature = screening_warmup_tail_signature(
                    direction="neutral",
                    points=full_points,
                    not_before=active_tail_start,
                    trade_level_only=True,
                )
                suffix_signature = screening_warmup_tail_signature(
                    direction="neutral",
                    points=suffix_points,
                    not_before=active_tail_start,
                    trade_level_only=True,
                )
                converged = full_signature == suffix_signature
                measurement = (
                    converged,
                    full_count,
                    len(suffix),
                    "WARMUP_TAIL_STABLE" if converged else "WARMUP_TAIL_DIVERGED",
                    () if converged else ("WARMUP_OTHER_SEMANTIC_CHANGED",),
                    full_points,
                )
            measurements[source_closed_at] = measurement
        (
            converged,
            full_count,
            suffix_count,
            reason,
            differences,
            production_five_points,
        ) = measurement
        one_end = bisect_right(one_dates, observed_at)
        one_start = max(
            0,
            one_end - SCREENING_CANONICAL_REQUEST_BARS["1m"],
        )
        one_count = one_end - one_start
        production_one_points: tuple[StructuralPoint, ...] = ()
        if one_count >= SCREENING_MINIMUM_BARS_BY_FREQUENCY["1m"]:
            one_prefix = (
                one_minute_frame.iloc[one_start:one_end].copy().reset_index(drop=True)
            )
            copy_price_basis_metadata(one_minute_frame, one_prefix)
            one_evidence = screening_evidence_from_frame(
                code=code,
                frequency="1m",
                frame=one_prefix,
                as_of=observed_at,
                market="a",
            )
            production_one_points = tuple(
                point
                for point in extract_one_minute_segment_difference_points(
                    one_evidence,
                    code=code,
                    source_frequency="1m",
                    as_of=observed_at,
                )
                if point.available_at == observed_at
                and is_one_minute_segment_difference(point)
            )
        output.append(
            FiveMinuteWarmupFact(
                observed_at=observed_at,
                source_closed_at=source_closed_at,
                converged=converged,
                full_bar_count=full_count,
                suffix_bar_count=suffix_count,
                reason_code=reason,
                difference_codes=differences,
                production_five_points=production_five_points,
                production_one_points=production_one_points,
                one_minute_bar_count=one_count,
            )
        )
    return tuple(output)


def _neutral_context(frequency: str, observed_at: datetime) -> TimeframeContext:
    return TimeframeContext(
        frequency=frequency,
        direction="neutral",
        disposition="neutral",
        hard_block=False,
        dominant_point_id=None,
        dominant_point_type=None,
        reason_codes=("sector_frequency_not_a_hard_gate",),
        observed_at=observed_at,
    )


def sector_facts_from_frame(
    *,
    sector_id: str,
    sector_name: str,
    member_count: int,
    frame: pd.DataFrame,
    observed_times: Sequence[datetime],
    algorithm_revision: str,
    source_revision: str,
    market_data_source: str = "qmt_gics3_component_composite",
    expected_closes: Sequence[datetime] = (),
) -> SectorResearchFacts:
    """在标的评估时点回放生产环境的板块硬闸门。"""

    times = tuple(sorted(set(observed_times)))
    if not times:
        return SectorResearchFacts(
            schema=SECTOR_FACT_SCHEMA,
            algorithm_revision=algorithm_revision,
            source_revision=source_revision,
            sector_id=sector_id,
            sector_name=sector_name,
            member_count=member_count,
            row_count=len(frame),
            thirty_points=(),
            assessments=(),
        )
    if frame.empty:
        return unavailable_sector_facts(
            sector_id=sector_id,
            sector_name=sector_name,
            member_count=member_count,
            observed_times=times,
            reason="sector_composite_frame_empty",
            algorithm_revision=algorithm_revision,
            source_revision=source_revision,
        )
    structure_events = final_confirmed_structure_events(sector_id, "30m", frame)
    points = structure_events.points
    visibility = structure_events.point_visibility
    points_by_id = {point.point_id: point for point in points}
    directions, unavailable = causal_directions(sector_id, frame, times)
    frame_closes = tuple(
        sorted(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    )
    market_closes = tuple(
        sorted(normalize_datetime(value, "expected_close") for value in expected_closes)
    )
    assessments: list[tuple[datetime, SectorAssessment]] = []
    for observed_at, direction in directions:
        current_ids = {
            interval.point_id
            for interval in visibility
            if interval.contains(observed_at)
        }
        current = tuple(points_by_id[point_id] for point_id in sorted(current_ids))
        thirty = classify_context(
            frequency="30m",
            current_direction=direction,
            points=current,
            as_of=observed_at,
        )
        expected_position = bisect_right(market_closes, observed_at)
        actual_position = bisect_right(frame_closes, observed_at)
        data_complete = (
            True
            if not market_closes
            else expected_position > 0
            and actual_position > 0
            and market_closes[expected_position - 1]
            == frame_closes[actual_position - 1]
        )
        assessments.append(
            (
                observed_at,
                assess_sector(
                    sector_id=sector_id,
                    sector_name=sector_name,
                    market_data_source=market_data_source,
                    thirty=thirty,
                    five=_neutral_context("5m", observed_at),
                    one=_neutral_context("1m", observed_at),
                    data_complete=data_complete,
                ),
            )
        )
    return SectorResearchFacts(
        schema=SECTOR_FACT_SCHEMA,
        algorithm_revision=algorithm_revision,
        source_revision=source_revision,
        sector_id=sector_id,
        sector_name=sector_name,
        member_count=member_count,
        row_count=len(frame),
        thirty_points=points,
        assessments=tuple(assessments),
        thirty_point_visibility=visibility,
        direction_unavailable_count=unavailable,
    )


def unavailable_sector_facts(
    *,
    sector_id: str,
    sector_name: str,
    member_count: int,
    observed_times: Sequence[datetime],
    reason: str,
    algorithm_revision: str,
    source_revision: str,
) -> SectorResearchFacts:
    if not reason:
        raise ValueError("unavailable sector reason is required")
    assessments = tuple(
        (
            observed_at,
            SectorAssessment(
                sector_id=sector_id,
                sector_name=sector_name,
                eligible=False,
                hard_block=True,
                regime="hostile",
                rank_components=(),
                reason_codes=("sector_data_incomplete",),
            ),
        )
        for observed_at in sorted(set(observed_times))
    )
    return SectorResearchFacts(
        schema=SECTOR_FACT_SCHEMA,
        algorithm_revision=algorithm_revision,
        source_revision=source_revision,
        sector_id=sector_id,
        sector_name=sector_name,
        member_count=member_count,
        row_count=0,
        thirty_points=(),
        assessments=assessments,
        error=reason,
    )


def build_symbol_bundle(
    facts: SymbolResearchFacts,
    evaluation: SparseEvaluationFact,
    sector: SectorAssessment,
    *,
    held_tower: StructureTower | None = None,
    held_level: int | None = None,
    selection_sources: tuple[str, ...] = (),
    selection_research: SelectionResearchSnapshot | None = None,
) -> SymbolStructureBundle:
    observed_at = evaluation.observed_at

    def tradable(points, frequency):
        return tuple(
            point
            for point in points
            if point.available_at <= observed_at and point.source_frequency == frequency
        )

    def current_at(points, frequency, visibility):
        visible = tradable(points, frequency)
        if not visibility:
            return visible
        current_ids = {
            interval.point_id
            for interval in visibility
            if interval.contains(observed_at)
        }
        return tuple(point for point in visible if point.point_id in current_ids)

    warmup_by_time = {row.observed_at: row for row in facts.five_minute_warmup}
    warmup = warmup_by_time.get(observed_at)
    if (
        warmup is not None
        and warmup.one_minute_bar_count != evaluation.one_minute_bar_count
    ):
        raise ValueError("execution snapshot 1m history count changed")

    # 完整递归图保留在事实档案中；订单级别只消费物理 5m/L0。5m/L1 的有效
    # 周期是 30m，只能进入高周期上下文，不能成为第二条 5m 交易通道。
    daily = current_at(
        facts.daily_points,
        "d",
        facts.daily_point_visibility,
    )
    thirty = current_at(
        facts.thirty_points,
        "30m",
        facts.thirty_point_visibility,
    )
    # At an execution candidate close, replay consumes the exact cold,
    # canonical production 5m snapshot.  A completed 1m nesting witness may
    # already have been visible before the 5m point became jointly known, so
    # its append-only causal ledger entry must be retained alongside the
    # current 1m tail rather than being dropped solely because it was known first.
    five_context = (
        warmup.production_five_points
        if warmup is not None
        else current_at(
            facts.five_points,
            "5m",
            facts.five_point_visibility,
        )
    )
    five = tuple(
        point
        for point in five_context
        if is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        )
    )
    one_history_ready = (
        evaluation.one_minute_bar_count >= SCREENING_MINIMUM_BARS_BY_FREQUENCY["1m"]
    )
    audit_one = tradable(facts.one_points, "1m") if one_history_ready else ()
    production_one = (
        warmup.production_one_points
        if warmup is not None
        else audit_one
        if one_history_ready
        else ()
    )
    current_one = (
        production_one
        if warmup is not None
        else current_at(
            facts.one_points,
            "1m",
            facts.one_point_visibility,
        )
        if one_history_ready
        else ()
    )
    nested_one_by_id = {
        point.point_id: point
        for setup in five
        if five_minute_setup_is_in_policy_scope(setup)
        and five_minute_setup_is_executable(setup, as_of=observed_at)
        for point in audit_one
        if (
            jointly_known_at := _segment_difference_jointly_known_at(
                setup,
                point,
            )
        )
        is not None
        and jointly_known_at <= observed_at
    }
    one_by_id = {point.point_id: point for point in production_one}
    one_by_id.update(nested_one_by_id)
    one = tuple(
        sorted(
            one_by_id.values(),
            key=lambda point: (
                point.available_at,
                point.recursive_level,
                point.tower,
                point.point_id,
            ),
        )
    )
    boundary_pairs = {
        (structural_point_occurrence_id(setup), point.point_id): (setup, point)
        for setup in five
        if setup.side == "buy"
        and five_minute_setup_is_in_policy_scope(setup)
        and five_minute_setup_is_executable(setup, as_of=observed_at)
        for point in (
            match_one_minute_nesting_witness_for_point(
                setup,
                one,
                as_of=observed_at,
            ),
        )
        if point is not None
        and point.side == "buy"
        and _segment_difference_jointly_known_at(setup, point) == observed_at
    }
    entry_boundaries = tuple(
        EntryExecutionBoundary(
            symbol=facts.code,
            setup_occurrence_id=structural_point_occurrence_id(setup),
            point_id=point.point_id,
            source_frequency="1m",
            confirmation_bar_closed_at=evaluation.bar.closed_at,
            raw_open=evaluation.bar.raw_open,
            raw_high=evaluation.bar.raw_high,
            raw_low=evaluation.bar.raw_low,
            raw_close=evaluation.bar.raw_close,
            raw_volume=evaluation.bar.volume,
            entry_valid_until=a_share_optional_entry_valid_until(
                evaluation.bar.closed_at
            ),
            raw_price_basis_revision=facts.source_revision,
        )
        for setup, point in sorted(
            boundary_pairs.values(),
            key=lambda value: (
                structural_point_occurrence_id(value[0]),
                value[1].point_id,
            ),
        )
    )
    buy_boundary_present = bool(boundary_pairs)
    enforce_warmup = buy_boundary_present
    warmup_converged = True if warmup is None else warmup.converged
    warmup_reasons: tuple[str, ...] = ()
    warmup_rows: tuple[tuple[str, bool, int, int], ...] = ()
    warmup_differences: tuple[tuple[str, tuple[str, ...]], ...] = ()
    if enforce_warmup:
        if warmup is None:
            warmup_converged = False
            warmup_reasons = ("5M:WARMUP_FACT_MISSING",)
            warmup_rows = (("5m", False, 0, 0),)
        else:
            warmup_reasons = (f"5M:{warmup.reason_code}",)
            warmup_rows = (
                (
                    "5m",
                    warmup.converged,
                    warmup.full_bar_count,
                    warmup.suffix_bar_count,
                ),
            )
            warmup_differences = (("5m", warmup.difference_codes),)
    return SymbolStructureBundle(
        code=facts.code,
        as_of=observed_at,
        sector=sector,
        daily_direction=evaluation.daily_direction,
        daily_points=daily,
        thirty_direction=evaluation.thirty_direction,
        thirty_points=thirty,
        five_points=five,
        one_points=one,
        # 与实时网关一致：冲突证据覆盖全部已分析个股周期，并在 ``resolve_conflict``
        # 内按方向和级别过滤。
        opposite_points=(*daily, *thirty, *five_context, *current_one),
        held_tower=held_tower,
        held_level=held_level,
        warmup_converged=warmup_converged,
        warmup_reason_codes=warmup_reasons,
        warmup_by_frequency=warmup_rows,
        warmup_difference_codes_by_frequency=warmup_differences,
        enforce_warmup_entry_gate=enforce_warmup,
        higher_timeframe_gates=evaluation.higher_timeframe_gates,
        enforce_higher_timeframe_entry_gate=(
            evaluation.higher_timeframe_gates is not None
        ),
        physical_timeframe_recursive=True,
        entry_execution_boundaries=entry_boundaries,
        selection_sources=selection_sources,
        selection_research=selection_research,
        daily_technical_context=evaluation.daily_technical_context,
        thirty_technical_context=evaluation.thirty_technical_context,
    )


def _event_bars(
    frame: pd.DataFrame,
    observed_times: Iterable[datetime],
) -> dict[datetime, MinuteBar]:
    if frame.empty:
        return {}
    rows = frame.set_index("date", drop=False)
    sessions = tuple(sorted({pd.Timestamp(value).date() for value in frame["date"]}))
    session_close = {
        session: Decimal(
            str(
                group.iloc[-1]["raw_close" if "raw_close" in group.columns else "close"]
            )
        )
        for session, group in frame.groupby(
            frame["date"].map(lambda value: value.date())
        )
    }
    previous: dict[date, Decimal] = {}
    for index, session in enumerate(sessions):
        if index > 0:
            previous[session] = session_close[sessions[index - 1]]
    output: dict[datetime, MinuteBar] = {}
    for raw_time in observed_times:
        observed_at = normalize_datetime(raw_time, "observed_at")
        key = pd.Timestamp(observed_at)
        if key not in rows.index:
            continue
        row = rows.loc[key]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        opened = Decimal(str(row["open"]))
        high = Decimal(str(row["high"]))
        low = Decimal(str(row["low"]))
        closed = Decimal(str(row["close"]))
        raw_opened = Decimal(str(row.get("raw_open", row["open"])))
        raw_high = Decimal(str(row.get("raw_high", row["high"])))
        raw_low = Decimal(str(row.get("raw_low", row["low"])))
        raw_closed = Decimal(str(row.get("raw_close", row["close"])))
        volume = Decimal(str(row["volume"]))
        reference = previous.get(observed_at.date(), opened)
        output[observed_at] = MinuteBar(
            code=str(row["code"]),
            opened_at=observed_at - timedelta(minutes=1),
            closed_at=observed_at,
            raw_open=raw_opened,
            raw_high=raw_high,
            raw_low=raw_low,
            raw_close=raw_closed,
            analysis_open=opened,
            analysis_high=high,
            analysis_low=low,
            analysis_close=closed,
            previous_raw_close=reference,
            volume=volume,
            turnover=raw_closed * volume,
            adjustment_known_at=observed_at,
        )
    return output


def _board_limit(code: str, session: date) -> Decimal:
    digits = code.split(".", 1)[1]
    if code.startswith("BJ."):
        return Decimal("0.30")
    if digits.startswith(("688", "689")):
        return Decimal("0.20")
    if digits.startswith(("300", "301")) and session >= date(2020, 8, 24):
        return Decimal("0.20")
    return Decimal("0.10")


def _status_for_bar(
    bar: MinuteBar,
    master: SecurityMasterRecord | None = None,
):
    from chanlun.decision_support.trading_system.backtest.models import SecurityStatus

    session = bar.opened_at.date()
    return SecurityStatus(
        session=session,
        code=bar.code,
        listed=True if master is None else master.listed_on(session),
        st=False,
        suspended=bar.volume <= 0,
        # 认证执行直接观察下一根已完成分钟 K 线价格区间；有意使用宽限制可避免虚构历史
        # 特别处理股票涨跌幅，同时原始 OHLC 仍约束所有可能成交价。
        limit_pct=(_board_limit(bar.code, session) if master is None else Decimal("1")),
        lot_size=100,
        t_plus_days=1,
    )


@dataclass(slots=True)
class _ActiveMinuteSource:
    frame: pd.DataFrame
    dates: tuple[datetime, ...]
    previous_by_session: dict[date, Decimal]
    index: int

    @property
    def next_at(self) -> datetime | None:
        return None if self.index >= len(self.dates) else self.dates[self.index]

    def pop(self) -> MinuteBar:
        if self.index >= len(self.frame):
            raise IndexError("minute source is exhausted")
        row = self.frame.iloc[self.index]
        observed_at = self.dates[self.index]
        self.index += 1
        opened = Decimal(str(row["open"]))
        high = Decimal(str(row["high"]))
        low = Decimal(str(row["low"]))
        closed = Decimal(str(row["close"]))
        raw_opened = Decimal(str(row.get("raw_open", row["open"])))
        raw_high = Decimal(str(row.get("raw_high", row["high"])))
        raw_low = Decimal(str(row.get("raw_low", row["low"])))
        raw_closed = Decimal(str(row.get("raw_close", row["close"])))
        volume = Decimal(str(row["volume"]))
        return MinuteBar(
            code=str(row["code"]),
            opened_at=observed_at - timedelta(minutes=1),
            closed_at=observed_at,
            raw_open=raw_opened,
            raw_high=raw_high,
            raw_low=raw_low,
            raw_close=raw_closed,
            analysis_open=opened,
            analysis_high=high,
            analysis_low=low,
            analysis_close=closed,
            previous_raw_close=self.previous_by_session.get(
                observed_at.date(),
                opened,
            ),
            volume=volume,
            turnover=raw_closed * volume,
            adjustment_known_at=observed_at,
        )


def _active_minute_source(
    code: str,
    *,
    requested_start: date,
    requested_end: date,
    after: datetime,
) -> _ActiveMinuteSource:
    frame = load_qmt_frame(
        code,
        "1m",
        start_at=datetime.combine(
            requested_start - timedelta(days=1),
            time(9, 30),
            tzinfo=CN,
        ),
        end_at=datetime.combine(requested_end, time(15, 0), tzinfo=CN),
    )
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    sessions = tuple(sorted({value.date() for value in dates}))
    session_closes = {
        session: Decimal(
            str(
                group.iloc[-1]["raw_close" if "raw_close" in group.columns else "close"]
            )
        )
        for session, group in frame.groupby(
            frame["date"].map(lambda value: value.date())
        )
    }
    previous = {
        session: session_closes[sessions[index - 1]]
        for index, session in enumerate(sessions)
        if index > 0
    }
    return _ActiveMinuteSource(
        frame=frame,
        dates=dates,
        previous_by_session=previous,
        index=bisect_right(dates, normalize_datetime(after, "after")),
    )


def _market_minute_timeline(
    *,
    effective_start: date,
    requested_end: date,
) -> tuple[datetime, ...]:
    frame = load_qmt_frame(
        "SH.000001",
        "1m",
        start_at=datetime.combine(effective_start, time(9, 30), tzinfo=CN),
        end_at=datetime.combine(requested_end, time(15, 0), tzinfo=CN),
    )
    if frame.empty:
        raise RuntimeError("QMT market-minute timeline is unavailable")
    return tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])


def _densify_equity_curve(
    run,
    timeline: Sequence[datetime],
    *,
    effective_start: date,
    requested_end: date,
):
    from chanlun.decision_support.trading_system.backtest.portfolio import (
        BacktestRun,
        EquityPoint,
    )

    points = tuple(sorted(run.equity_curve, key=lambda row: row.closed_at))
    if not points:
        raise ValueError("sparse run has no equity points")
    baseline_at = datetime.combine(effective_start, time(9, 30), tzinfo=CN)
    terminal_at = datetime.combine(requested_end, time(15, 0), tzinfo=CN)
    times = {
        normalize_datetime(value, "market_minute")
        for value in timeline
        if baseline_at <= normalize_datetime(value, "market_minute") <= terminal_at
    }
    times.update((baseline_at, terminal_at))
    times.update(point.closed_at for point in points)
    ordered_times = tuple(sorted(times))
    dense: list[EquityPoint] = []
    cursor = 0
    current = points[0]
    if current.closed_at > ordered_times[0]:
        raise ValueError("equity curve starts after the market timeline")
    for observed_at in ordered_times:
        while cursor + 1 < len(points) and points[cursor + 1].closed_at <= observed_at:
            cursor += 1
            current = points[cursor]
        dense.append(
            EquityPoint(
                closed_at=observed_at,
                cash=current.cash,
                market_value=current.market_value,
                equity=current.equity,
                open_risk_cash=current.open_risk_cash,
            )
        )
    return BacktestRun(
        fills=run.fills,
        trades=run.trades,
        equity_curve=tuple(dense),
        open_positions=run.open_positions,
        pending_exits=run.pending_exits,
    )


def run_sparse_portfolio(
    symbol_facts: Sequence[SymbolResearchFacts],
    sector_facts: Mapping[str, SectorResearchFacts],
    *,
    initial_cash: Decimal,
    minute_timeline: Sequence[datetime] | None = None,
    selection_research_by_code: Mapping[
        str,
        tuple[SelectionResearchSnapshot, ...],
    ]
    | None = None,
    formal_selection_required: bool = True,
):
    """执行稀疏决策，并逐分钟回放所有持仓与待处理订单。

    新买入始终要求当时的板块评估确属支持状态。只有调用方明确启用
    ``formal_selection_required`` 时，才额外要求当时可见的正式个股研究快照；
    当前生产选股不读取该旧账本，因此正式固定年度回放会显式关闭这一门。
    板块来源由同一次因果回放中的 ``SectorResearchFacts`` 唯一推导，调用方不能再
    用静态标的映射把未来触发回填到整段历史。
    """

    from chanlun.decision_support.trading_system.backtest.execution import (
        ExecutionPolicy,
    )
    from chanlun.decision_support.trading_system.backtest.portfolio import (
        EvaluatedDecisionAt,
        _PortfolioState,
        apply_evaluated_decisions,
    )
    from chanlun.decision_support.trading_system.human_assisted_decision import (
        HumanAssistedDecisionCore,
    )
    from chanlun.decision_support.trading_system.models import TradingPolicy
    from chanlun.decision_support.trading_system.portfolio_risk import (
        RiskLimits,
    )

    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if type(formal_selection_required) is not bool:
        raise ValueError("formal_selection_required must be a boolean")
    if not symbol_facts:
        raise ValueError("symbol facts are required")
    selection_research = {
        code: tuple(snapshots)
        for code, snapshots in (selection_research_by_code or {}).items()
    }
    facts_by_code = {row.code: row for row in symbol_facts}
    if len(facts_by_code) != len(symbol_facts):
        raise ValueError("symbol facts contain duplicate codes")
    unknown_research_symbols = set(selection_research).difference(facts_by_code)
    if unknown_research_symbols:
        raise ValueError("selection research contains symbols outside replay facts")
    for code, snapshots in selection_research.items():
        if any(snapshot.symbol != code for snapshot in snapshots):
            raise ValueError("selection research symbol does not match mapping key")
        identities = tuple(snapshot.snapshot_id for snapshot in snapshots)
        if len(identities) != len(set(identities)):
            raise ValueError("selection research snapshots must be unique")
        if snapshots != tuple(
            sorted(
                snapshots,
                key=lambda snapshot: (
                    snapshot.effective_at,
                    snapshot.known_at,
                    snapshot.snapshot_id,
                ),
            )
        ):
            raise ValueError("selection research snapshots must be chronological")
    requested_starts = {row.requested_start for row in symbol_facts}
    requested_ends = {row.requested_end for row in symbol_facts}
    effective_starts = {row.effective_start for row in symbol_facts}
    if any(
        len(values) != 1
        for values in (requested_starts, requested_ends, effective_starts)
    ):
        raise ValueError("symbol fact ranges disagree")
    requested_start = next(iter(requested_starts))
    requested_end = next(iter(requested_ends))
    effective_start = next(iter(effective_starts))
    events_by_time: dict[
        datetime,
        list[tuple[SymbolResearchFacts, SparseEvaluationFact]],
    ] = {}
    for facts in symbol_facts:
        for evaluation in facts.evaluations:
            events_by_time.setdefault(evaluation.observed_at, []).append(
                (facts, evaluation)
            )
    event_times = tuple(sorted(events_by_time))
    sector_by_time = {
        sector_id: dict(facts.assessments) for sector_id, facts in sector_facts.items()
    }
    state = _PortfolioState.initial(initial_cash)
    baseline_at = datetime.combine(effective_start, time(9, 30), tzinfo=CN)
    state.record_equity(baseline_at)
    engine = HumanAssistedDecisionCore(
        TradingPolicy(),
        formal_selection_required=formal_selection_required,
    )
    risk_limits = RiskLimits()
    execution_policy = ExecutionPolicy(require_observed_price_range=True)
    actions = tuple(
        sorted(
            (
                factor.corporate_action()
                for facts in symbol_facts
                for factor in facts.factors
            ),
            key=lambda row: (row.effective_at, row.code),
        )
    )
    action_keys = tuple((row.code, row.effective_at) for row in actions)
    if len(action_keys) != len(set(action_keys)):
        raise ValueError("corporate actions contain duplicate symbol dates")
    action_index = 0
    active: dict[str, _ActiveMinuteSource] = {}
    last_bars: dict[str, MinuteBar] = {}
    event_index = 0
    while event_index < len(event_times) or active:
        next_event = (
            event_times[event_index] if event_index < len(event_times) else None
        )
        active_times = tuple(
            source.next_at for source in active.values() if source.next_at is not None
        )
        next_active = min(active_times, default=None)
        candidates = tuple(
            value for value in (next_event, next_active) if value is not None
        )
        if not candidates:
            break
        observed_at = min(candidates)
        due_actions = []
        while (
            action_index < len(actions)
            and actions[action_index].effective_at <= observed_at
        ):
            due_actions.append(actions[action_index])
            action_index += 1
        if due_actions:
            state.apply_corporate_actions(tuple(due_actions))
        bars: dict[str, MinuteBar] = {}
        for code, source in tuple(active.items()):
            if source.next_at == observed_at:
                bars[code] = source.pop()
        current_events = (
            events_by_time[observed_at] if next_event == observed_at else []
        )
        if next_event == observed_at:
            event_index += 1
        for facts, evaluation in current_events:
            bars.setdefault(facts.code, evaluation.bar)
        for code in sorted(bars):
            bar = bars[code]
            status = _status_for_bar(
                bar,
                facts_by_code[code].security_master,
            )
            state.try_pending_orders(
                bar,
                status,
                execution_policy,
                risk_limits,
            )
            state.mark_to_market(bar)
            state.check_intrabar_structural_stops(
                bar,
                status,
                execution_policy,
            )
            last_bars[code] = bar
        evaluated_rows: list[EvaluatedDecisionAt] = []
        for facts, evaluation in sorted(current_events, key=lambda row: row[0].code):
            bar = bars[facts.code]
            evaluation_sector_id = (
                facts.sector_id
                if facts.security_master is None
                else evaluation.sector_id
            )
            resolved_sector_id = evaluation_sector_id or "qmt-sw1:unclassified"
            sector = sector_by_time.get(resolved_sector_id, {}).get(observed_at)
            if sector is None:
                sector = SectorAssessment(
                    sector_id=resolved_sector_id,
                    sector_name=resolved_sector_id,
                    eligible=False,
                    hard_block=True,
                    regime="hostile",
                    rank_components=(),
                    reason_codes=("sector_data_incomplete",),
                )
            held = state.held_structures().get(facts.code)
            current_research = visible_selection_research(
                selection_research.get(facts.code, ()),
                symbol=facts.code,
                selection_path="INDIVIDUAL_THREE_PROGRAM",
                decision_time=observed_at,
            )
            bundle = build_symbol_bundle(
                facts,
                evaluation,
                sector,
                held_tower=None if held is None else held[0],
                held_level=None if held is None else held[1],
                selection_sources=(
                    ("QMT_SECTOR_TRIGGER",)
                    if sector.regime == "supportive"
                    else ("QMT_SECTOR_ELIGIBLE_SCOPE",)
                    if sector.eligible
                    else ("INCREMENTAL_SCAN_SCOPE",)
                ),
                selection_research=current_research,
            )
            status = _status_for_bar(bar, facts.security_master)
            for evaluated in engine.evaluate_symbol(bundle):
                evaluated_rows.append(
                    EvaluatedDecisionAt(
                        code=facts.code,
                        evaluated=evaluated,
                        bar=bar,
                        lot_size=status.lot_size,
                    )
                )
        apply_evaluated_decisions(
            state,
            tuple(evaluated_rows),
            risk_limits=risk_limits,
            created_at=observed_at,
        )
        state.record_equity(observed_at)
        needed = set(state.positions_by_code)
        needed.update(row.intent.code for row in state.pending_orders)
        for code in tuple(active):
            if code not in needed or active[code].next_at is None:
                del active[code]
        for code in sorted(needed - set(active)):
            active[code] = _active_minute_source(
                code,
                requested_start=requested_start,
                requested_end=requested_end,
                after=observed_at,
            )
    if state.positions_by_code:
        terminal_at = datetime.combine(requested_end, time(15, 0), tzinfo=CN)
        for code in sorted(tuple(state.positions_by_code)):
            master = facts_by_code[code].security_master
            if master is not None and (
                (
                    master.listed_through is not None
                    and master.listed_through < requested_end
                )
                or "\u9000\u5e02" in master.name
                or master.name.endswith("\u9000")
            ):
                state.write_off_position(
                    code=code,
                    closed_at=terminal_at,
                    reason="expired_security_zero_recovery",
                )
        # 样本边界仍开放的持仓继续保持开放；若用已完成末分钟 OHLC 卖出，会制造常规执行链
        # 明确禁止的同 K 线成交。改用最后一个因果可见的原始收盘价盯市，报告将开放持仓
        # 与已实现交易分开展示。
        state.record_equity(terminal_at)
    run = state.finish()
    timeline = (
        _market_minute_timeline(
            effective_start=effective_start,
            requested_end=requested_end,
        )
        if minute_timeline is None
        else tuple(minute_timeline)
    )
    return _densify_equity_curve(
        run,
        timeline,
        effective_start=effective_start,
        requested_end=requested_end,
    )


def build_symbol_facts(
    *,
    code: str,
    sector_id: str,
    warmup_start: date,
    requested_start: date,
    requested_end: date,
    effective_start: date,
    algorithm_revision: str,
    security_master: SecurityMasterRecord | None = None,
    memberships: Sequence[SectorMembershipChange] = (),
    qmt_factors: Sequence[QmtFactorAt] = (),
) -> SymbolResearchFacts:
    if not warmup_start <= requested_start <= effective_start <= requested_end:
        raise ValueError("invalid fixed-year date boundaries")
    if security_master is not None and security_master.code != code:
        raise ValueError("security master does not match requested code")
    membership_rows = tuple(
        sorted(memberships, key=lambda row: (row.known_at, row.sector_id))
    )
    factor_rows = tuple(sorted(qmt_factors, key=lambda row: row.effective_on))
    if any(row.code != code for row in (*membership_rows, *factor_rows)):
        raise ValueError("PIT metadata does not match requested code")
    factors = qmt_factor_frame(factor_rows)
    end_at = datetime.combine(requested_end, time(15, 0), tzinfo=CN)
    context_start = datetime.combine(warmup_start, time(9, 30), tzinfo=CN)
    physical_history_start = datetime(1990, 1, 1, tzinfo=CN)
    # Replay starts with the same 1m warmup horizon as a freshly started live
    # monitor, then keeps appending closed bars for the complete run.
    minute_start = context_start
    effective_at = datetime.combine(effective_start, time(9, 30), tzinfo=CN)
    local_directory = resolve_qmt_local_data_dir()
    local_five_snapshot = (
        read_qmt_local_kline(
            data_dir=local_directory,
            code=code,
            frequency="5m",
            start_at=physical_history_start,
            end_at=end_at,
        )
        if local_directory is not None
        else None
    )
    frames = {
        frequency: load_qmt_frame(
            code,
            frequency,
            start_at=physical_history_start,
            end_at=end_at,
            factors=factors,
            _local_five_snapshot=local_five_snapshot,
        )
        for frequency in ("30m", "5m")
    }
    missing_context = tuple(
        frequency for frequency in ("30m", "5m") if frames[frequency].empty
    )
    if missing_context:
        raise RuntimeError(
            "QMT context history is unavailable for classified security "
            f"{code}: {','.join(missing_context)}"
        )
    five_ledger = _causal_confirmed_structure_events(
        code,
        "5m",
        frames["5m"],
        include_audit_ledger=False,
    )
    five_points = five_ledger.points
    five_point_visibility = five_ledger.point_visibility
    operation_five_points = tuple(
        point
        for point in five_points
        if is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        )
        and five_minute_setup_is_in_policy_scope(point)
    )
    operation_point_ids = {point.point_id for point in operation_five_points}
    operation_point_visibility = tuple(
        interval
        for interval in five_point_visibility
        if interval.point_id in operation_point_ids
    )
    relevant_point_ids = {
        interval.point_id
        for interval in operation_point_visibility
        if interval.visible_from <= end_at
        and (interval.visible_until is None or interval.visible_until > effective_at)
    }
    relevant_setups = tuple(
        setup
        for setup in operation_five_points
        if setup.point_id in relevant_point_ids
        and setup.available_at <= end_at
        and five_minute_setup_expires_at(setup) >= effective_at
    )
    has_relevant_setup = bool(relevant_setups)
    daily_frame = (
        load_qmt_daily_frame(
            code,
            # Production asks for a fixed canonical daily snapshot (with a
            # smaller admission floor).  Select that exact physical tail
            # preceding the first tradable session plus every later bar;
            # pre-tail vendor records are causally irrelevant.
            start_at=datetime.combine(effective_start, time.min, tzinfo=CN),
            end_at=end_at,
            factors=factors,
            history_bars_before_start=SCREENING_CANONICAL_REQUEST_BARS["d"],
        )
        if has_relevant_setup
        else _empty_frame(code)
    )
    if has_relevant_setup and daily_frame.empty:
        raise RuntimeError(
            f"QMT daily history is unavailable for a causally relevant setup: {code}"
        )
    frames["d"] = daily_frame
    if relevant_setups:
        minute_start = min(
            minute_start,
            min(
                five_minute_segment_difference_window_start(point)
                for point in relevant_setups
            ),
        )
    one_frame = (
        load_qmt_frame(
            code,
            "1m",
            start_at=minute_start,
            end_at=end_at,
            factors=factors,
        )
        if has_relevant_setup
        else _empty_frame(code)
    )
    if has_relevant_setup and one_frame.empty:
        raise RuntimeError(
            f"QMT 1m history is unavailable for a causally relevant setup: {code}"
        )
    frames["1m"] = one_frame
    source_revision = _symbol_source_revision(
        frames,
        factors,
        security_master=security_master,
        memberships=membership_rows,
    )
    one_points, one_point_visibility = _causal_one_minute_events_by_windows(
        code,
        one_frame,
        _one_minute_replay_windows(
            relevant_setups,
            end_at=end_at,
            point_visibility=operation_point_visibility,
        ),
    )
    one_dates = tuple(
        pd.Timestamp(value).to_pydatetime() for value in one_frame["date"]
    )
    evaluation_times = sparse_evaluation_times(
        five_points=operation_five_points,
        one_points=one_points,
        thirty_closes=tuple(
            pd.Timestamp(value).to_pydatetime() for value in frames["30m"]["date"]
        ),
        one_closes=one_dates,
        effective_start=effective_at,
        requested_end=end_at,
        five_point_visibility=operation_point_visibility,
    )
    thirty_dates = tuple(
        pd.Timestamp(value).to_pydatetime() for value in frames["30m"]["date"]
    )
    context_ready_times = tuple(
        observed_at
        for observed_at in evaluation_times
        if bisect_right(thirty_dates, observed_at)
        >= SCREENING_MINIMUM_BARS_BY_FREQUENCY["30m"]
    )
    thirty_history_unavailable = len(evaluation_times) - len(context_ready_times)
    (
        daily_directions,
        daily_points,
        daily_point_visibility,
        daily_unavailable_times,
    ) = _production_context_snapshots(
        code,
        "d",
        daily_frame,
        context_ready_times,
        request_bars=SCREENING_CANONICAL_REQUEST_BARS["d"],
        minimum_bars=SCREENING_MINIMUM_BARS_BY_FREQUENCY["d"],
    )
    daily_direction_by_time = dict(daily_directions)
    evaluation_times = tuple(
        observed_at
        for observed_at in context_ready_times
        if observed_at in daily_direction_by_time
    )
    thirty_ledger = _causal_confirmed_structure_events(
        code,
        "30m",
        frames["30m"],
        visibility_windows=((context_start, max(evaluation_times)),)
        if evaluation_times
        else (),
        include_audit_ledger=False,
    )
    thirty_points = thirty_ledger.points
    directions, unavailable = causal_directions(
        code,
        frames["30m"],
        evaluation_times,
    )
    direction_by_time = dict(directions)
    bars = _event_bars(one_frame, evaluation_times)

    def sector_at(observed_at: datetime) -> str | None:
        if security_master is not None and not security_master.listed_on(
            observed_at.date()
        ):
            return None
        available = tuple(row for row in membership_rows if row.known_at <= observed_at)
        return None if not available else available[-1].sector_id

    evaluation_time_set = {
        observed_at for observed_at in evaluation_times if observed_at in bars
    }
    buy_boundary_times = tuple(
        sorted(
            _buy_segment_difference_boundary_times(
                operation_five_points,
                one_points,
                eligible_times=evaluation_time_set,
            )
        )
    )
    five_minute_warmup = _five_minute_warmup_facts(
        code,
        frames["5m"],
        one_frame,
        buy_boundary_times,
    )
    daily_technical_contexts = _same_period_technical_context_snapshots(
        code,
        "d",
        daily_frame,
        buy_boundary_times,
        request_bars=SCREENING_CANONICAL_REQUEST_BARS["d"],
        minimum_bars=SCREENING_MINIMUM_BARS_BY_FREQUENCY["d"],
    )
    thirty_technical_contexts = _same_period_technical_context_snapshots(
        code,
        "30m",
        frames["30m"],
        buy_boundary_times,
        request_bars=SCREENING_CANONICAL_REQUEST_BARS["30m"],
        minimum_bars=SCREENING_MINIMUM_BARS_BY_FREQUENCY["30m"],
    )
    sector_by_time = {
        observed_at: sector_at(observed_at) for observed_at in buy_boundary_times
    }
    higher_timeframe_gates = _historical_higher_timeframe_gates(
        code=code,
        one_minute_frame=one_frame,
        daily_frame=daily_frame,
        observed_times=buy_boundary_times,
        sector_by_time=sector_by_time,
    )
    evaluations = tuple(
        SparseEvaluationFact(
            observed_at=observed_at,
            thirty_direction=direction_by_time[observed_at],
            bar=bars[observed_at],
            sector_id=sector_at(observed_at),
            daily_direction=daily_direction_by_time[observed_at],
            higher_timeframe_gates=higher_timeframe_gates.get(observed_at),
            daily_technical_context=daily_technical_contexts.get(observed_at),
            thirty_technical_context=thirty_technical_contexts.get(observed_at),
            one_minute_bar_count=min(
                bisect_right(one_dates, observed_at),
                SCREENING_CANONICAL_REQUEST_BARS["1m"],
            ),
        )
        for observed_at in evaluation_times
        if observed_at in bars
    )
    return SymbolResearchFacts(
        schema=FACT_SCHEMA,
        algorithm_revision=algorithm_revision,
        source_revision=source_revision,
        code=code,
        sector_id=sector_id,
        requested_start=requested_start,
        requested_end=requested_end,
        effective_start=effective_start,
        row_counts=tuple(
            (frequency, len(frames[frequency])) for frequency in FACT_FREQUENCIES
        ),
        daily_points=daily_points,
        thirty_points=thirty_points,
        five_points=five_points,
        one_points=one_points,
        evaluations=evaluations,
        daily_point_visibility=daily_point_visibility,
        thirty_point_visibility=thirty_ledger.point_visibility,
        five_point_visibility=five_point_visibility,
        one_point_visibility=one_point_visibility,
        five_minute_warmup=five_minute_warmup,
        direction_unavailable_count=(
            unavailable + thirty_history_unavailable + len(daily_unavailable_times)
        ),
        security_master=security_master,
        memberships=membership_rows,
        factors=factor_rows,
    )


__all__ = (
    "CAUSAL_CENTER_COMPLETION_CONTRACT",
    "CausalCenterCompletionFact",
    "CausalStructureEventLedger",
    "FACT_SCHEMA",
    "FACT_FREQUENCIES",
    "FiveMinuteWarmupFact",
    "PointVisibilityInterval",
    "SECTOR_FACT_SCHEMA",
    "SectorResearchFacts",
    "SparseEvaluationFact",
    "SymbolResearchFacts",
    "build_symbol_bundle",
    "build_symbol_facts",
    "causal_directions",
    "final_confirmed_points",
    "first_matching_segment_difference",
    "final_confirmed_structure_events",
    "load_qmt_frame",
    "load_qmt_daily_frame",
    "qmt_factor_frame",
    "qmt_native_code",
    "run_sparse_portfolio",
    "sector_facts_from_frame",
    "setup_active_ends",
    "sparse_evaluation_times",
    "strict_state",
    "unavailable_sector_facts",
)
