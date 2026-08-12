"""唯一生产选股引擎使用的原生行情适配器。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
import math
from numbers import Integral
import re
from threading import RLock
from typing import Protocol, cast

import pandas as pd

from chanlun.core.strict_structure.errors import StrictStructureContractError
from chanlun.core.strict_structure.formal_state import current_formal_direction
from chanlun.core.strict_structure.models import StrictEvidenceResult
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)
from chanlun.decision_support.trading_system.context import (
    classify_context,
    context_point_max_age,
)
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeDataUnavailable,
    HigherTimeframeGateBundle,
    HigherTimeframeSessionEvidence,
    unresolved_higher_timeframe_gates,
)
from chanlun.decision_support.trading_system.incremental_scan import BarKey
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    EntryExecutionBoundary,
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    SectorAssessment,
    StructuralPoint,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.provisional import (
    ProvisionalCandidate,
    extract_provisional_candidates,
)
from chanlun.decision_support.trading_system.qmt_sector_same_base import (
    QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT,
    derive_qmt_sector_thirty_minute_frame,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.runtime_config import (
    strict_snapshot_price_metadata,
)
from chanlun.decision_support.trading_system.screening_runtime import (
    screening_evidence_from_frame,
)
from chanlun.decision_support.trading_system.screening_structure import (
    SCREENING_STRUCTURE_FREQUENCIES,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_QMT_30M_FALLBACK_REASON_CODE,
    SCREENING_WARMUP_DIFFERENCE_CODES,
    SCREENING_WARMUP_REQUIRED_BARS,
)
from chanlun.decision_support.trading_system.sector_policy import assess_sector
from chanlun.decision_support.trading_system.sector_strength import (
    SectorStrengthBatch,
    SectorStrengthEvidence,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (
    qmt_sector_catalog_revision,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    normalize_qmt_opening_events_for_completed_minutes,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupConvergenceEnvelope,
    WarmupPrefixObservation,
    classify_warmup_convergence_envelope,
)
from chanlun.exchange.exchange import convert_stock_kline_frequency
from chanlun.exchange.price_basis import QMT_STRUCTURE_DIVIDEND_TYPE
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_GICS3_CATALOG_SOURCE,
    QMT_GICS3_COMPOSITE_ADJUSTMENT,
    QMT_GICS3_COMPOSITE_PROVIDER,
)
from chanlun.tools.log_util import LogUtil


_FREQUENCIES = SCREENING_STRUCTURE_FREQUENCIES
CANONICAL_REQUEST_BARS_BY_FREQUENCY = (
    ("d", 1600),
    ("30m", 4000),
    ("5m", 12000),
    ("1m", 12000),
)
_SECTOR_FREQUENCIES = ("30m", "5m")
_A_STOCK_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")
_FRAME_UNSET = object()
_WARMUP_ENVELOPE_PREFIX_RATIOS = ((1, 2), (2, 3), (5, 6), (1, 1))
_TRADABLE_SCREENING_INSTRUMENT_TYPES = frozenset({"stock_cn", "etf_cn"})
_KNOWN_SCREENING_INSTRUMENT_TYPES = frozenset(
    {
        "stock_cn",
        "etf_cn",
        "index_cn",
        "fund_cn",
        "unsupported_cn",
        "unresolved_cn",
    }
)


@dataclass(frozen=True, slots=True)
class FrameStructureAnalysis:
    closed_at: datetime
    direction: ContextDirection
    confirmed_points: tuple[StructuralPoint, ...]
    provisional_points: tuple[ProvisionalCandidate, ...]
    warmup_converged: bool = True
    warmup_full_bar_count: int = 0
    warmup_suffix_bar_count: int = 0
    warmup_reason_codes: tuple[str, ...] = ()
    warmup_difference_codes: tuple[str, ...] = ()
    entry_execution_boundaries: tuple[EntryExecutionBoundary, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "closed_at",
            normalize_datetime(self.closed_at, "closed_at"),
        )
        if self.direction not in {"up", "down", "neutral"}:
            raise ValueError("invalid structure direction")
        if min(self.warmup_full_bar_count, self.warmup_suffix_bar_count) < 0:
            raise ValueError("warmup bar counts cannot be negative")
        if len(self.warmup_reason_codes) != len(set(self.warmup_reason_codes)):
            raise ValueError("warmup reason codes must be unique")
        if len(self.warmup_difference_codes) != len(
            set(self.warmup_difference_codes)
        ) or not set(self.warmup_difference_codes).issubset(
            SCREENING_WARMUP_DIFFERENCE_CODES
        ):
            raise ValueError("warmup difference codes are invalid")
        if len({value.point_id for value in self.entry_execution_boundaries}) != len(
            self.entry_execution_boundaries
        ):
            raise ValueError("entry execution boundary point ids must be unique")


class SectorAnalysisUnavailable(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        if not code:
            raise ValueError("sector analysis error code is required")
        self.code = code


class StrictStructureAnalysisError(RuntimeError):
    """表示已校验行情帧不满足严格结构契约。"""


@dataclass(frozen=True, slots=True)
class SectorAnalysisFailure:
    sector_id: str
    code: str
    error_type: str
    reason: str
    detail_code: str | None = None
    catalog_member_count: int | None = None
    universe_member_count: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("sector_id", "code", "error_type", "reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} is required")
        if self.detail_code is not None and (
            not isinstance(self.detail_code, str) or not self.detail_code
        ):
            raise ValueError("detail_code must be a non-empty string")
        for field_name in ("catalog_member_count", "universe_member_count"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SectorAnalysisExclusion:
    """表示确定性的目录资格结论，而不是分析失败。"""

    sector_id: str
    code: str
    reason_code: str
    reason: str
    detail_code: str
    catalog_member_count: int
    universe_member_count: int
    required_member_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "sector_id",
            "code",
            "reason_code",
            "reason",
            "detail_code",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} is required")
        if self.reason_code != "sector_member_coverage_insufficient":
            raise ValueError("unsupported sector exclusion reason")
        for field_name in (
            "catalog_member_count",
            "universe_member_count",
            "required_member_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.required_member_count <= 0:
            raise ValueError("required_member_count must be positive")
        if self.universe_member_count >= self.required_member_count:
            raise ValueError("sector exclusion must be below its member threshold")


@dataclass(frozen=True, slots=True)
class SectorAssessmentBatch:
    assessments: tuple[SectorAssessment, ...]
    discovered_count: int
    completed_count: int
    failure_counts: tuple[tuple[str, int], ...]
    errors: tuple[SectorAnalysisFailure, ...]
    exclusion_counts: tuple[tuple[str, int], ...] = ()
    exclusions: tuple[SectorAnalysisExclusion, ...] = ()
    # 精确的 QMT 目录身份。缺失时批次保持关闭失败，不能进入前向发布。
    catalog_revision: str | None = None
    # 紧凑的成员分类证据，用于重算全部横向强度、跨板块排序和各板块来源身份。
    # 缺失时只能展示，不能进入前向发布。
    strength_evidence: SectorStrengthBatch | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "assessments", tuple(self.assessments))
        object.__setattr__(self, "failure_counts", tuple(self.failure_counts))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "exclusion_counts", tuple(self.exclusion_counts))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        if self.catalog_revision is not None and (
            not isinstance(self.catalog_revision, str)
            or not self.catalog_revision.startswith("sha256:")
        ):
            raise ValueError("sector catalog revision must be a sha256 identity")
        if self.strength_evidence is not None:
            evidence_ids = tuple(self.strength_evidence)
            assessment_ids = tuple(sorted(item.sector_id for item in self.assessments))
            if evidence_ids != assessment_ids:
                raise ValueError("sector strength evidence must cover every assessment")
            assessment_by_id = {item.sector_id: item for item in self.assessments}
            if any(
                assessment_by_id[sector_id].horizontal_strength
                != self.strength_evidence[sector_id].strength
                or assessment_by_id[sector_id].horizontal_rank
                != self.strength_evidence[sector_id].rank
                or assessment_by_id[sector_id].strength_anchor_session
                != self.strength_evidence[sector_id].anchor_session
                or assessment_by_id[sector_id].strength_member_count
                != self.strength_evidence[sector_id].member_count
                or assessment_by_id[sector_id].strength_source_revision
                != self.strength_evidence[sector_id].source_revision
                or assessment_by_id[sector_id].strength_reason_codes
                != self.strength_evidence[sector_id].reason_codes
                for sector_id in evidence_ids
            ):
                raise ValueError("sector strength evidence does not match assessments")
            evidence_document = self.strength_evidence.evidence_document()
            if (
                self.catalog_revision is not None
                and evidence_document.get("membership_revision")
                != self.catalog_revision
            ):
                raise ValueError(
                    "sector strength evidence membership revision is inconsistent"
                )
        if (
            type(self.discovered_count) is not int
            or type(self.completed_count) is not int
            or not 0 <= self.completed_count <= self.discovered_count
        ):
            raise ValueError("sector completion counts are invalid")
        if len({item.sector_id for item in self.errors}) != len(self.errors):
            raise ValueError("sector analysis errors must have unique sector ids")
        if len({item.sector_id for item in self.exclusions}) != len(self.exclusions):
            raise ValueError("sector analysis exclusions must have unique sector ids")
        if {item.sector_id for item in self.errors} & {
            item.sector_id for item in self.exclusions
        }:
            raise ValueError("sector errors and exclusions must be disjoint")
        normalized_counts = tuple(sorted(self.failure_counts))
        if normalized_counts != self.failure_counts:
            raise ValueError("sector failure counts must be sorted by code")
        if any(
            not code or type(count) is not int or count <= 0
            for code, count in self.failure_counts
        ):
            raise ValueError("sector failure counts are invalid")
        if sum(count for _code, count in self.failure_counts) != len(self.errors):
            raise ValueError("sector failure counts do not match errors")
        actual_counts = tuple(
            sorted(Counter(item.error_type for item in self.errors).items())
        )
        if actual_counts != self.failure_counts:
            raise ValueError("sector failure codes do not match errors")
        normalized_exclusion_counts = tuple(sorted(self.exclusion_counts))
        if normalized_exclusion_counts != self.exclusion_counts:
            raise ValueError("sector exclusion counts must be sorted by code")
        if any(
            not code or type(count) is not int or count <= 0
            for code, count in self.exclusion_counts
        ):
            raise ValueError("sector exclusion counts are invalid")
        if sum(count for _code, count in self.exclusion_counts) != len(self.exclusions):
            raise ValueError("sector exclusion counts do not match exclusions")
        actual_exclusion_counts = tuple(
            sorted(Counter(item.reason_code for item in self.exclusions).items())
        )
        if actual_exclusion_counts != self.exclusion_counts:
            raise ValueError("sector exclusion codes do not match exclusions")

    @property
    def completion_ratio(self) -> Decimal:
        if self.discovered_count == 0:
            return Decimal("0")
        return Decimal(self.completed_count) / Decimal(self.discovered_count)

    @property
    def resolution_ratio(self) -> Decimal:
        if self.discovered_count == 0:
            return Decimal("0")
        return Decimal(self.completed_count + len(self.exclusions)) / Decimal(
            self.discovered_count
        )


def _sector_failure_document(item: SectorAnalysisFailure) -> dict[str, object]:
    result: dict[str, object] = {
        "sector_id": item.sector_id,
        "code": item.code,
        "error_type": item.error_type,
        "reason": item.reason[:160],
    }
    if item.detail_code is not None:
        result["detail_code"] = item.detail_code
    if item.catalog_member_count is not None:
        result["catalog_member_count"] = item.catalog_member_count
    if item.universe_member_count is not None:
        result["universe_member_count"] = item.universe_member_count
    return result


def _sector_exclusion_document(
    item: SectorAnalysisExclusion,
) -> dict[str, object]:
    return {
        "sector_id": item.sector_id,
        "code": item.code,
        "exclusion_type": "sector_analysis_exclusion",
        "eligibility": "MINIMUM_SECTOR_MEMBERS_NOT_MET",
        "reason_code": item.reason_code,
        "reason": item.reason[:160],
        "detail_code": item.detail_code,
        "catalog_member_count": item.catalog_member_count,
        "universe_member_count": item.universe_member_count,
        "required_member_count": item.required_member_count,
        "deterministic_for_catalog_revision": True,
        "retry_policy": "NEXT_SECTOR_CATALOG_REVISION",
    }


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
    # 冷启动使用各物理周期可获得的完整 QMT 回看区间。旧的 3200/4800 根尾部数据
    # 足以形成线段，却不足以稳定一、二类点：同一标的在旧中枢恢复前可能只显示三类点。
    # 标的采用确定性分片，未变化数据帧会缓存，实时调度只分析有界变更代码批次；
    # 较长耗时仅发生在冷态契约重建。
    request_bars_by_frequency: tuple[tuple[str, int], ...] = (
        CANONICAL_REQUEST_BARS_BY_FREQUENCY
    )
    minimum_bars_by_frequency: tuple[tuple[str, int], ...] = (
        ("d", 240),
        ("30m", 240),
        ("5m", 480),
        ("1m", 960),
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
                raise ValueError(f"{field_name} must define d, 30m, 5m and 1m")
            if any(type(value) is not int or value <= 0 for value in values.values()):
                raise ValueError(f"{field_name} values must be positive integers")
        requests = dict(self.request_bars_by_frequency)
        minimums = dict(self.minimum_bars_by_frequency)
        if any(minimums[key] > requests[key] for key in _FREQUENCIES):
            raise ValueError("minimum bars cannot exceed requested bars")
        if (
            type(self.minimum_sector_members) is not int
            or self.minimum_sector_members <= 0
        ):
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
    snapshot_attrs = dict(value.attrs)
    optional = tuple(column for column in ("member_mask",) if column in value.columns)
    result = value.loc[:, [*required, *optional]].copy()
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
    if "member_mask" in result:
        masks = tuple(result["member_mask"])
        if any(
            isinstance(mask, bool) or not isinstance(mask, Integral) for mask in masks
        ):
            raise ValueError("kline member masks must be exact integers")
        result.loc[:, "member_mask"] = tuple(int(mask) for mask in masks)
    if len(result) < minimum_bars:
        raise ValueError("kline frame does not meet minimum history")
    result.loc[:, list(numeric_columns)] = numeric
    result.attrs = snapshot_attrs
    return result


def _frame_content_revision(frame: pd.DataFrame) -> str:
    """把分析缓存命中绑定到精确的已收盘输入前缀。

    价格基准身份会有意跨普通 QMT 刷新保持稳定，不能单独证明缓存结构有效：QMT 可能
    补齐缺失成分，或在末根收盘时间不变时修订开高低收。哈希需覆盖全部已使用行情行，
    板块合成还需覆盖精确贡献者位图路径，使这类修订必然触发确定性重算。
    """

    identity_attrs = {
        name: frame.attrs.get(name)
        for name in (
            "structure_price_quantum",
            "price_basis_revision",
            "price_basis_provider",
            "price_basis_adjustment",
            "source_base_stream_revision",
            "source_base_frequency",
            "sector_id",
            "sector_membership_revision",
            "sector_members",
            "sector_composite_members",
            "sector_composite_required_member_count",
            "sector_composite_member_mask_contract",
            "sector_composite_member_path_revision",
            "sector_composite_method",
            "sector_factor_adjustment_contract_id",
            "sector_factor_revision",
            "sector_thirty_minute_derivation_contract",
            "derived_frequency",
        )
        if name in frame.attrs
    }
    has_member_mask = "member_mask" in frame.columns
    return sha256_json(
        {
            "schema": "chanlun-screening-closed-frame",
            "attrs": identity_attrs,
            "rows": tuple(
                {
                    "date": _market_datetime(row.date, "kline.date"),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                    **(
                        {"member_mask": int(row.member_mask)} if has_member_mask else {}
                    ),
                }
                for row in frame.itertuples(index=False)
            ),
        }
    )


def _entry_valid_until(confirmation_closed_at: datetime) -> datetime:
    """返回按结束时间标记的 A 股 1m 行情对应的冻结可选入场有效期。"""

    return a_share_optional_entry_valid_until(confirmation_closed_at)


def _entry_execution_boundaries(
    *,
    code: str,
    points: tuple[StructuralPoint, ...],
    raw_frame: pd.DataFrame,
) -> tuple[EntryExecutionBoundary, ...]:
    """把已确认 1m 买点绑定到精确的 QMT 不复权行情。"""

    metadata = strict_snapshot_price_metadata(raw_frame)
    if (
        raw_frame.attrs.get("price_basis_provider") != "qmt"
        or raw_frame.attrs.get("price_basis_adjustment") != "none"
    ):
        raise ValueError("entry confirmation evidence must be unadjusted QMT data")
    rows_by_time: dict[datetime, object] = {}
    for row in raw_frame.itertuples(index=False):
        closed_at = _market_datetime(row.date, "raw confirmation bar close")
        if closed_at in rows_by_time:
            raise ValueError("raw confirmation bar times must be unique")
        rows_by_time[closed_at] = row
    output: list[EntryExecutionBoundary] = []
    for point in points:
        if point.source_frequency != "1m" or not point.confirmed or point.side != "buy":
            continue
        row = rows_by_time.get(point.available_at)
        if row is None:
            continue
        output.append(
            EntryExecutionBoundary(
                symbol=code,
                point_id=point.point_id,
                source_frequency="1m",
                confirmation_bar_closed_at=point.available_at,
                raw_open=Decimal(str(row.open)),
                raw_high=Decimal(str(row.high)),
                raw_low=Decimal(str(row.low)),
                raw_close=Decimal(str(row.close)),
                raw_volume=Decimal(str(row.volume)),
                entry_valid_until=_entry_valid_until(point.available_at),
                raw_price_basis_revision=metadata.price_basis_revision,
            )
        )
    return tuple(sorted(output, key=lambda value: value.point_id))


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
    # 元数据失败描述输入快照，而非结构引擎契约违规，因此保留公开的 ValueError 分类。
    strict_snapshot_price_metadata(frame)
    try:
        evidence = screening_evidence_from_frame(
            code=code,
            frequency=frequency,
            frame=frame,
            as_of=closed_at,
            market="a",
        )
    except (StrictStructureContractError, ValueError) as exc:
        raise StrictStructureAnalysisError(str(exc)) from exc
    provisional = extract_provisional_candidates(
        evidence,
        code=code,
        source_frequency=frequency,
        as_of=closed_at,
    )
    return FrameStructureAnalysis(
        closed_at=closed_at,
        direction=_strict_direction(evidence),
        confirmed_points=extract_confirmed_points(
            evidence,
            code=code,
            source_frequency=frequency,
            as_of=closed_at,
        ),
        provisional_points=provisional,
    )


def _warmup_tail_signature(
    analysis: FrameStructureAnalysis,
    *,
    not_before: datetime,
) -> tuple[object, ...]:
    latest: dict[tuple[str, str, int], tuple[datetime, tuple[object, ...]]] = {}
    # 预热门禁只比较已经确认、能够进入正式决策链的证据。未确认候选的形态会
    # 随最后一根行情变化，它们仍展示在观察列表中，但不能让正式历史收敛失效。
    for point in analysis.confirmed_points:
        observed_at = point.available_at
        # 锚点过旧而最近才完成递归确认的点，不是当前尾部信号。锚点时间与
        # 可用时间必须同时位于活动窗口内，避免旧结构重新进入实时决策。
        if observed_at < not_before or point.anchor_at < not_before:
            continue
        lane = (point.point_type, point.tower, point.recursive_level)
        # 对象编号包含完整上游结构血缘。同一个尾部点在截短左侧历史后可能
        # 得到不同编号，因此收敛比较使用稳定语义，正式信号仍保留原始血缘编号。
        semantic = (
            point.side,
            point.status,
            point.source_frequency,
            point.anchor_at.isoformat(),
            None if point.confirmed_at is None else point.confirmed_at.isoformat(),
            point.available_at.isoformat(),
            point.price_basis_revision,
            point.structure_anchor_price,
            point.structure_invalidation_price,
            point.center_zd,
            point.center_zg,
            point.center_ordinal,
            point.variant,
            point.divergence_kind,
            point.evidence_codes,
        )
        previous = latest.get(lane)
        if previous is None or observed_at > previous[0]:
            latest[lane] = (observed_at, semantic)
    return (
        analysis.direction,
        tuple(
            (lane, observed_at.isoformat(), semantic)
            for lane, (observed_at, semantic) in sorted(latest.items())
        ),
    )


def _warmup_latest_points(
    analysis: FrameStructureAnalysis,
    *,
    not_before: datetime,
) -> dict[tuple[str, str, int], tuple[datetime, StructuralPoint]]:
    """返回活动签名使用的各条正式语义通道中的最新点。"""

    latest: dict[tuple[str, str, int], tuple[datetime, StructuralPoint]] = {}
    for point in analysis.confirmed_points:
        observed_at = point.available_at
        if observed_at < not_before or point.anchor_at < not_before:
            continue
        lane = (point.point_type, point.tower, point.recursive_level)
        previous = latest.get(lane)
        if previous is None or observed_at > previous[0]:
            latest[lane] = (observed_at, point)
    return latest


def _warmup_tail_difference_codes(
    full: FrameStructureAnalysis,
    short: FrameStructureAnalysis,
    *,
    not_before: datetime,
) -> tuple[str, ...]:
    """解释两段历史的正式证据差异，不放宽活动门禁。"""

    codes: list[str] = []
    if full.direction != short.direction:
        codes.append("WARMUP_DIRECTION_CHANGED")
    full_points = _warmup_latest_points(full, not_before=not_before)
    short_points = _warmup_latest_points(short, not_before=not_before)
    if set(full_points) != set(short_points):
        codes.append("WARMUP_ACTIVE_POINT_LANES_CHANGED")

    def values(point: object, names: tuple[str, ...]) -> tuple[object, ...]:
        return tuple(getattr(point, name, None) for name in names)

    for lane in sorted(set(full_points).intersection(short_points)):
        left = full_points[lane][1]
        right = short_points[lane][1]
        if type(left) is not type(right) or values(
            left,
            ("side", "status", "actionable"),
        ) != values(right, ("side", "status", "actionable")):
            codes.append("WARMUP_POINT_STATUS_CHANGED")
        if values(
            left,
            ("anchor_at", "confirmed_at", "available_at", "observed_at"),
        ) != values(
            right,
            ("anchor_at", "confirmed_at", "available_at", "observed_at"),
        ):
            codes.append("WARMUP_POINT_TIMING_CHANGED")
        if values(
            left,
            (
                "price_basis_revision",
                "structure_anchor_price",
                "structure_invalidation_price",
                "center_zd",
                "center_zg",
                "anchor_price",
            ),
        ) != values(
            right,
            (
                "price_basis_revision",
                "structure_anchor_price",
                "structure_invalidation_price",
                "center_zd",
                "center_zg",
                "anchor_price",
            ),
        ):
            codes.append("WARMUP_PRICE_OR_BOUNDARY_CHANGED")
        if values(
            left,
            (
                "source_frequency",
                "center_ordinal",
                "variant",
                "divergence_kind",
            ),
        ) != values(
            right,
            (
                "source_frequency",
                "center_ordinal",
                "variant",
                "divergence_kind",
            ),
        ):
            codes.append("WARMUP_STRUCTURE_IDENTITY_CHANGED")
        if values(
            left,
            ("evidence_codes", "missing_conditions"),
        ) != values(right, ("evidence_codes", "missing_conditions")):
            codes.append("WARMUP_POINT_EVIDENCE_CHANGED")
    unique = tuple(dict.fromkeys(codes))
    if not unique and _warmup_tail_signature(
        full,
        not_before=not_before,
    ) != _warmup_tail_signature(short, not_before=not_before):
        return ("WARMUP_OTHER_SEMANTIC_CHANGED",)
    return unique


def analyze_native_frame_with_warmup(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
) -> FrameStructureAnalysis:
    """要求活动尾部的正式证据在两种左侧历史长度下保持一致。"""

    full = analyze_native_frame(
        code=code,
        frequency=frequency,
        frame=frame,
        as_of=as_of,
    )
    required = SCREENING_WARMUP_REQUIRED_BARS[frequency]
    if len(frame) < required:
        return replace(
            full,
            warmup_converged=False,
            warmup_full_bar_count=len(frame),
            warmup_suffix_bar_count=0,
            warmup_reason_codes=("WARMUP_HISTORY_INSUFFICIENT",),
        )
    trim = len(frame) // 3
    suffix = frame.iloc[trim:].copy().reset_index(drop=True)
    suffix.attrs = dict(frame.attrs)
    suffix_start = _market_datetime(suffix["date"].iloc[0], "warmup suffix start")
    active_tail_start = max(
        suffix_start,
        normalize_datetime(as_of, "as_of") - context_point_max_age(frequency),
    )
    short = analyze_native_frame(
        code=code,
        frequency=frequency,
        frame=suffix,
        as_of=as_of,
    )
    converged = _warmup_tail_signature(
        full,
        not_before=active_tail_start,
    ) == _warmup_tail_signature(short, not_before=active_tail_start)
    difference_codes = (
        ()
        if converged
        else _warmup_tail_difference_codes(
            full,
            short,
            not_before=active_tail_start,
        )
    )
    return replace(
        full,
        warmup_converged=converged,
        warmup_full_bar_count=len(frame),
        warmup_suffix_bar_count=len(suffix),
        warmup_reason_codes=(
            "WARMUP_TAIL_STABLE" if converged else "WARMUP_TAIL_DIVERGED",
        ),
        warmup_difference_codes=difference_codes,
    )


def audit_native_frame_warmup_envelope(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
    prefix_ratios: tuple[tuple[int, int], ...] = (_WARMUP_ENVELOPE_PREFIX_RATIOS),
) -> WarmupConvergenceEnvelope:
    """在四种左侧历史长度下审计同一活动尾部。

    此处特意调用 :func:`analyze_native_frame`，而不是生效中的成对预热包装器。结果只
    用于诊断，并携带不可变的 ``active_gate_unchanged`` 标记；调用方不得把结果反馈到
    排名、候选选择或订单资格判断。
    """

    if frequency not in SCREENING_WARMUP_REQUIRED_BARS:
        raise ValueError(f"unsupported warmup frequency: {frequency}")
    if not isinstance(frame, pd.DataFrame) or frame.empty or "date" not in frame:
        raise ValueError("warmup envelope requires a non-empty dated frame")
    ratios = tuple(prefix_ratios)
    if len(ratios) < 3 or any(
        type(numerator) is not int
        or type(denominator) is not int
        or numerator <= 0
        or denominator <= 0
        or numerator > denominator
        for numerator, denominator in ratios
    ):
        raise ValueError("prefix_ratios require at least three valid fractions")
    parameter_set_id = sha256_json(
        {
            "contract": "warmup-common-tail-multi-prefix",
            "frequency": frequency,
            "prefix_ratios": ratios,
            "minimum_prefix_bars": SCREENING_WARMUP_REQUIRED_BARS[frequency],
            "active_gate_unchanged": True,
        }
    )
    bar_counts = tuple(
        sorted(
            {
                len(frame) * numerator // denominator
                for numerator, denominator in ratios
                if len(frame) * numerator // denominator
                >= SCREENING_WARMUP_REQUIRED_BARS[frequency]
            }
        )
    )
    prepared: list[tuple[int, datetime, FrameStructureAnalysis]] = []
    for bar_count in bar_counts:
        suffix = frame.iloc[-bar_count:].copy().reset_index(drop=True)
        suffix.attrs = dict(frame.attrs)
        starts_at = _market_datetime(
            suffix["date"].iloc[0],
            "warmup envelope prefix start",
        )
        prepared.append(
            (
                bar_count,
                starts_at,
                analyze_native_frame(
                    code=code,
                    frequency=frequency,
                    frame=suffix,
                    as_of=as_of,
                ),
            )
        )
    common_tail_start = (
        None
        if not prepared
        else max(
            max(row[1] for row in prepared),
            normalize_datetime(as_of, "as_of") - context_point_max_age(frequency),
        )
    )
    observations = tuple(
        WarmupPrefixObservation(
            bar_count=bar_count,
            starts_at=starts_at,
            signature_sha256=sha256_json(
                _warmup_tail_signature(
                    analysis,
                    not_before=cast(datetime, common_tail_start),
                )
            ),
        )
        for bar_count, starts_at, analysis in prepared
    )
    return classify_warmup_convergence_envelope(
        frequency=frequency,
        as_of=as_of,
        parameter_set_id=parameter_set_id,
        observations=observations,
    )


def _strict_direction(evidence: StrictEvidenceResult) -> ContextDirection:
    return cast(ContextDirection, current_formal_direction(evidence))


def _stock_codes(raw: object) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise TypeError("stock scope provider must return a sequence")
    values: set[str] = set()
    for item in raw:
        code = item.get("code") if isinstance(item, Mapping) else item
        if isinstance(code, str) and _A_STOCK_CODE.fullmatch(code):
            values.add(code)
    return tuple(sorted(values))


def _qmt_catalog_universe(
    rows: Sequence[object],
) -> dict[str, str]:
    """使用已捕获的 QMT GICS3 成员作为板块优先选股范围。

    目录构建器已经把原生响应规范化并筛成 A 股身份；再与
    ``ExchangeQMT.all_stocks`` 求交既重复，也会让 ``get_full_tick`` 意外影响扫描范围。
    """

    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw_members = row.get("member_codes")
        if isinstance(raw_members, (str, bytes)) or not isinstance(
            raw_members, Sequence
        ):
            continue
        for value in raw_members:
            if isinstance(value, str) and _A_STOCK_CODE.fullmatch(value):
                result.setdefault(value.split(".", 1)[1], value)
    return result


def _catalog_member_count(raw: Sequence[object]) -> int:
    """统计 QMT 目录中唯一且规范的 A 股身份。"""

    identities: set[str] = set()
    for value in raw:
        if isinstance(value, str) and _A_STOCK_CODE.fullmatch(value):
            identities.add(value)
    return len(identities)


class NativeTradingDataGateway:
    """先读取板块事实，再构建个股日线、30m、5m、1m 物理结构。"""

    def __init__(
        self,
        *,
        exchange_provider: Callable[[], object],
        sector_provider: Callable[[], object],
        sector_frame_provider: Callable[..., object] | None = None,
        sector_strength_provider: Callable[..., Mapping[str, SectorStrengthEvidence]]
        | None = None,
        higher_timeframe_provider: Callable[..., HigherTimeframeGateBundle]
        | None = None,
        trading_session_provider: Callable[..., Mapping[str, object]] | None = None,
        instrument_type_provider: Callable[[tuple[str, ...]], Mapping[str, str]]
        | None = None,
        watchlist_provider: Callable[[], object] = lambda: (),
        holdings_provider: Callable[[], object] = lambda: (),
        analyzer: StructureAnalyzer = analyze_native_frame_with_warmup,
        progress_callback: Callable[[], None] = lambda: None,
        config: NativeTradingGatewayConfig = NativeTradingGatewayConfig(),
    ) -> None:
        providers = (
            exchange_provider,
            sector_provider,
            watchlist_provider,
            holdings_provider,
            analyzer,
            progress_callback,
        )
        if any(not callable(provider) for provider in providers):
            raise TypeError("trading gateway providers must be callable")
        if sector_frame_provider is not None and not callable(sector_frame_provider):
            raise TypeError("sector_frame_provider must be callable")
        if sector_strength_provider is not None and not callable(
            sector_strength_provider
        ):
            raise TypeError("sector_strength_provider must be callable")
        if higher_timeframe_provider is not None and not callable(
            higher_timeframe_provider
        ):
            raise TypeError("higher_timeframe_provider must be callable")
        if trading_session_provider is not None and not callable(
            trading_session_provider
        ):
            raise TypeError("trading_session_provider must be callable")
        if instrument_type_provider is not None and not callable(
            instrument_type_provider
        ):
            raise TypeError("instrument_type_provider must be callable")
        self._exchange_provider = exchange_provider
        self._sector_provider = sector_provider
        self._sector_frame_provider = sector_frame_provider
        self._sector_strength_provider = sector_strength_provider
        self._higher_timeframe_provider = higher_timeframe_provider
        self._trading_session_provider = trading_session_provider
        self._instrument_type_provider = instrument_type_provider
        self._watchlist_provider = watchlist_provider
        self._holdings_provider = holdings_provider
        self._analyzer = analyzer
        self._progress_callback = progress_callback
        self._config = config
        self._lock = RLock()
        self._members: dict[str, tuple[str, ...]] = {}
        self._symbol_names: dict[str, str] = {}
        # 原生证券类型在同一工作进程和源码版本内稳定；只缓存已明确解析的类型。
        # ``unresolved_cn`` 可能来自瞬时 QMT 故障，必须保留后续重试机会。
        self._instrument_types: dict[str, str] = {}
        self._latest_sector_bars: dict[tuple[str, str], datetime] = {}
        self._emitted_sector_bars: dict[tuple[str, str], datetime] = {}
        self._analysis_cache: dict[
            tuple[str, str], tuple[str, FrameStructureAnalysis]
        ] = {}
        # 月/周/日事实冻结在盘中监听所用的显式因果截止点。只缓存完全解析的证据包，
        # 避免重复 1m/5m 观测反复采样数百个日级交易日；瞬时 UNRESOLVED 仍可重试。
        self._higher_timeframe_cache: dict[
            tuple[str, str, str, str, str], HigherTimeframeGateBundle
        ] = {}

    def _report_progress(self) -> None:
        """在原生调用或高计算量边界前后立即证明进度。

        回调会有意传播失败。隔离工作进程与父进程断连后必须停止后续 QMT 工作，不能
        留下继续消耗原生资源的孤儿进程。
        """

        self._progress_callback()

    def set_progress_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("progress callback must be callable")
        with self._lock:
            self._progress_callback = callback

    def _load_analysis(
        self,
        *,
        exchange: object | None,
        code: str,
        analysis_code: str,
        frequency: str,
        as_of: datetime,
        sector_source: str | None = None,
        frame_override: object = _FRAME_UNSET,
    ) -> FrameStructureAnalysis:
        if sector_source not in {
            None,
            QMT_GICS3_CATALOG_SOURCE,
        }:
            raise ValueError("unsupported sector source")
        is_sector = sector_source is not None
        loader = getattr(exchange, "klines", None)
        if frame_override is _FRAME_UNSET and not callable(loader):
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_adapter_error",
                    "sector frame source is unavailable",
                )
            raise TypeError("exchange must expose klines")
        args: dict[str, object] = {
            "req_counts": self._config.request_bars(frequency),
            "dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
        }
        if frame_override is _FRAME_UNSET:
            try:
                self._report_progress()
                raw_frame = loader(code, frequency, args=args)
                self._report_progress()
            except SectorAnalysisUnavailable:
                raise
            except Exception as exc:
                if is_sector:
                    raise SectorAnalysisUnavailable(
                        "sector_adapter_error",
                        str(exc),
                    ) from exc
                raise
        else:
            raw_frame = frame_override
        if is_sector:
            if not isinstance(raw_frame, pd.DataFrame):
                raise SectorAnalysisUnavailable(
                    "sector_kline_unavailable",
                    "kline frame is unavailable",
                )
            try:
                expected_attrs = {
                    "price_basis_provider": QMT_GICS3_COMPOSITE_PROVIDER,
                    "price_basis_adjustment": QMT_GICS3_COMPOSITE_ADJUSTMENT,
                    "sector_factor_adjustment_contract_id": (
                        QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
                    ),
                }
                factor_revision = raw_frame.attrs.get("sector_factor_revision")
                if (
                    not isinstance(factor_revision, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", factor_revision) is None
                ):
                    raise ValueError("sector causal factor revision is unavailable")
                if frequency == "30m":
                    expected_attrs.update(
                        {
                            "source_base_frequency": "5m",
                            "derived_frequency": "30m",
                            "sector_thirty_minute_derivation_contract": (
                                QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT
                            ),
                        }
                    )
                if any(
                    raw_frame.attrs.get(name) != value
                    for name, value in expected_attrs.items()
                ):
                    raise ValueError("sector price basis attrs are incomplete")
                strict_snapshot_price_metadata(raw_frame)
            except Exception as exc:
                raise SectorAnalysisUnavailable(
                    "sector_price_basis_unavailable",
                    str(exc),
                ) from exc
        fallback_reason_codes: tuple[str, ...] = ()
        try:
            frame = _closed_frame(
                raw_frame,
                not_after=as_of,
                minimum_bars=self._config.minimum_bars(frequency),
            )
            if frequency == "1m" and not is_sector:
                frame = normalize_qmt_opening_events_for_completed_minutes(frame)
                if len(frame) < self._config.minimum_bars("1m"):
                    raise ValueError("kline frame does not meet minimum history")
        except Exception as exc:
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_kline_unavailable",
                    str(exc),
                ) from exc
            if (
                frequency == "30m"
                and frame_override is _FRAME_UNSET
                and str(exc) == "kline frame contains invalid market facts"
            ):
                try:
                    frame = self._validated_thirty_minute_fallback(
                        exchange=exchange,
                        code=code,
                        as_of=as_of,
                    )
                except Exception as fallback_exc:
                    raise ValueError(
                        "native 30m frame contains invalid market facts; "
                        "validated completed-5m fallback unavailable: "
                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                    ) from fallback_exc
                fallback_reason_codes = (SCREENING_QMT_30M_FALLBACK_REASON_CODE,)
                LogUtil.warning(
                    "[trading_screening.market_data_fallback] "
                    "code="
                    f"{code} frequency=30m "
                    f"reason={SCREENING_QMT_30M_FALLBACK_REASON_CODE}"
                )
            else:
                raise
        try:
            strict_snapshot_price_metadata(frame)
            if sector_source == QMT_GICS3_CATALOG_SOURCE:
                expected_provider = QMT_GICS3_COMPOSITE_PROVIDER
                expected_adjustment = QMT_GICS3_COMPOSITE_ADJUSTMENT
            else:
                expected_provider = expected_adjustment = None
            if is_sector and (
                frame.attrs.get("price_basis_provider") != expected_provider
                or frame.attrs.get("price_basis_adjustment") != expected_adjustment
                or (
                    sector_source == QMT_GICS3_CATALOG_SOURCE
                    and (
                        frame.attrs.get("sector_factor_adjustment_contract_id")
                        != QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
                        or re.fullmatch(
                            r"sha256:[0-9a-f]{64}",
                            str(frame.attrs.get("sector_factor_revision")),
                        )
                        is None
                    )
                )
            ):
                raise ValueError("closed sector frame lost price basis attrs")
        except Exception as exc:
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_price_basis_unavailable",
                    str(exc),
                ) from exc
            raise
        closed_at = _market_datetime(frame["date"].iloc[-1], "bar close")
        frame_content_revision = _frame_content_revision(frame)
        cache_key = (analysis_code, frequency)
        with self._lock:
            cached = self._analysis_cache.get(cache_key)
        if (
            cached is not None
            and cached[0] == frame_content_revision
            and cached[1].closed_at == closed_at
        ):
            return cached[1]
        try:
            self._report_progress()
            analysis = self._analyzer(
                code=analysis_code,
                frequency=frequency,
                frame=frame,
                as_of=closed_at,
            )
            self._report_progress()
        except StrictStructureAnalysisError as exc:
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_structure_invalid",
                    str(exc),
                ) from exc
            raise
        except Exception as exc:
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_adapter_error",
                    str(exc),
                ) from exc
            raise
        if (
            not is_sector
            and frequency == "1m"
            and any(
                point.confirmed and point.side == "buy"
                for point in analysis.confirmed_points
            )
        ):
            # 结构在冻结复权基准上计算；可选入场上限属于执行事实，必须取自准确的
            # 未复权确认 K 线。后者单独读取，绝不能由结构锚点或复权因子推断。
            try:
                self._report_progress()
                raw_confirmation_frame = loader(
                    code,
                    "1m",
                    args={
                        "req_counts": self._config.request_bars("1m"),
                        "dividend_type": "none",
                        "skip_download": True,
                    },
                )
                self._report_progress()
                raw_confirmation_frame = _closed_frame(
                    raw_confirmation_frame,
                    not_after=as_of,
                    minimum_bars=self._config.minimum_bars("1m"),
                )
                raw_confirmation_frame = (
                    normalize_qmt_opening_events_for_completed_minutes(
                        raw_confirmation_frame
                    )
                )
                if len(raw_confirmation_frame) < self._config.minimum_bars("1m"):
                    raise ValueError("kline frame does not meet minimum history")
                analysis = replace(
                    analysis,
                    entry_execution_boundaries=_entry_execution_boundaries(
                        code=code,
                        points=analysis.confirmed_points,
                        raw_frame=raw_confirmation_frame,
                    ),
                )
            except Exception as exc:
                # 筛选结果仍可供人工看图复核；下游虚拟意图闸门会把缺失边界视为仅观测，
                # 因此不会静默变成成交。
                LogUtil.warning(
                    "[trading_screening.entry_execution_boundary] "
                    f"code={code} reason={type(exc).__name__}: {str(exc)[:160]}"
                )
        if fallback_reason_codes:
            analysis = replace(
                analysis,
                warmup_reason_codes=tuple(
                    dict.fromkeys(
                        (*analysis.warmup_reason_codes, *fallback_reason_codes)
                    )
                ),
            )
        with self._lock:
            self._analysis_cache[cache_key] = (
                frame_content_revision,
                analysis,
            )
        return analysis

    def _validated_thirty_minute_fallback(
        self,
        *,
        exchange: object | None,
        code: str,
        as_of: datetime,
    ) -> pd.DataFrame:
        """用已完成的 QMT 5m 行情重建一条无效的原生 30m 数据流。

        这不是价格修补：无效原生行情会被完整丢弃。替代开高低收量全部由同源的六根
        已完成 5m 行情确定性聚合，再通过常规因果行情门槛校验；底层行情缺失或无效
        仍属于硬失败。
        """

        loader = getattr(exchange, "klines", None)
        if not callable(loader):
            raise TypeError("exchange must expose klines")
        requested_thirty = self._config.request_bars("30m")
        minimum_thirty = self._config.minimum_bars("30m")
        self._report_progress()
        raw_five = loader(
            code,
            "5m",
            args={
                "req_counts": requested_thirty * 6,
                "dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
            },
        )
        self._report_progress()
        five = _closed_frame(
            raw_five,
            not_after=as_of,
            minimum_bars=minimum_thirty * 6,
        )
        five.insert(0, "code", code)
        source_attrs = dict(five.attrs)
        rebuilt = convert_stock_kline_frequency(five, "30m")
        rebuilt.attrs = source_attrs
        return _closed_frame(
            rebuilt,
            not_after=as_of,
            minimum_bars=minimum_thirty,
        )

    def _has_current_five_minute_setup(
        self,
        analysis: FrameStructureAnalysis,
    ) -> bool:
        cutoff = analysis.closed_at.timestamp() - self._config.current_setup_age_seconds
        return any(
            (
                point.observed_at
                if isinstance(point, ProvisionalCandidate)
                else point.available_at
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
            cached = self._analysis_cache.get((code, frequency))
        return None if cached is None else cached[1]

    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
    ) -> SectorAssessmentBatch:
        observed_at = normalize_datetime(as_of, "as_of")
        self._report_progress()
        raw = self._sector_provider()
        self._report_progress()
        if not isinstance(raw, Mapping):
            raise TypeError("sector catalog must be a mapping")
        catalog_source = raw.get("source")
        if catalog_source != QMT_GICS3_CATALOG_SOURCE:
            raise ValueError("sector catalog must expose QMT GICS3 components")
        rows = raw.get("sectors")
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise TypeError("sector catalog must expose a sectors sequence")
        catalog_revision = qmt_sector_catalog_revision(raw)
        provided_revision = raw.get("catalog_revision")
        if provided_revision is not None and provided_revision != catalog_revision:
            raise ValueError("QMT sector catalog revision does not match its members")
        # 当前 QMT 成分构成时点化选股标的池。
        digits = _qmt_catalog_universe(rows)
        symbol_names: dict[str, str] = {}
        universe_codes = set(digits.values())
        assessments: list[SectorAssessment] = []
        errors: list[SectorAnalysisFailure] = []
        exclusions: list[SectorAnalysisExclusion] = []
        discovered_count = 0
        completed_count = 0
        members_by_sector: dict[str, tuple[str, ...]] = {}
        latest_bars: dict[tuple[str, str], datetime] = {}
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            sector_id = row.get("sector_id")
            sector_name = row.get("name")
            source_key = row.get("source_key")
            raw_members = row.get("member_codes")
            valid_identity = (
                isinstance(sector_id, str)
                and sector_id.startswith("qmt-gics3:")
                and isinstance(source_key, str)
                and source_key.startswith("GICS3")
            )
            if (
                not valid_identity
                or sector_id in seen
                or not isinstance(sector_name, str)
                or not sector_name.strip()
                or isinstance(raw_members, (str, bytes))
                or not isinstance(raw_members, Sequence)
            ):
                continue
            members = tuple(
                sorted(
                    {
                        (value if value in universe_codes else digits[value])
                        for value in raw_members
                        if isinstance(value, str)
                        and (value in universe_codes or value in digits)
                    }
                )
            )
            catalog_member_count = _catalog_member_count(raw_members)
            seen.add(sector_id)
            discovered_count += 1
            members_by_sector[sector_id] = members
            if len(members) < self._config.minimum_sector_members:
                if catalog_member_count == 0:
                    detail_code = "sector_catalog_members_missing"
                elif catalog_member_count < self._config.minimum_sector_members:
                    detail_code = "sector_constituent_count_below_minimum"
                else:
                    detail_code = "sector_universe_member_coverage_insufficient"
                exclusion = SectorAnalysisExclusion(
                    sector_id=sector_id,
                    code=cast(str, source_key),
                    reason_code="sector_member_coverage_insufficient",
                    reason=(
                        f"catalog_members={catalog_member_count}; "
                        f"universe_members={len(members)}; "
                        f"required={self._config.minimum_sector_members}"
                    ),
                    detail_code=detail_code,
                    catalog_member_count=catalog_member_count,
                    universe_member_count=len(members),
                    required_member_count=self._config.minimum_sector_members,
                )
                exclusions.append(exclusion)
                assessments.append(
                    SectorAssessment(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        rank_components=(),
                        reason_codes=(exclusion.reason_code, detail_code),
                        strength_member_count=len(members),
                        strength_reason_codes=(
                            "SECTOR_STRENGTH_MEMBER_COVERAGE_INSUFFICIENT",
                            detail_code.upper(),
                        ),
                    )
                )
                continue
            current_frequency = "unknown"
            try:
                analyses: dict[str, FrameStructureAnalysis] = {}
                for frequency in _SECTOR_FREQUENCIES:
                    current_frequency = frequency
                    if self._sector_frame_provider is None:
                        raise SectorAnalysisUnavailable(
                            "sector_adapter_error",
                            "QMT sector frame provider is unavailable",
                        )
                    provider_frequency = frequency
                    provider_request_bars = self._config.request_bars(frequency)
                    if frequency == "30m":
                        # 原生 30m 成员收益中位数不等同于六段 5m 成员收益中位数的连乘结果。
                        provider_frequency = "5m"
                        provider_request_bars = (
                            self._config.request_bars("30m") * 6 + 47
                        )
                    self._report_progress()
                    raw_sector_frame = self._sector_frame_provider(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        members=members,
                        frequency=provider_frequency,
                        as_of=observed_at,
                        request_bars=provider_request_bars,
                    )
                    self._report_progress()
                    if frequency == "30m":
                        if not isinstance(raw_sector_frame, pd.DataFrame):
                            raise SectorAnalysisUnavailable(
                                "sector_kline_unavailable",
                                "QMT sector 5m base is unavailable",
                            )
                        raw_sector_frame = derive_qmt_sector_thirty_minute_frame(
                            raw_sector_frame,
                            request_bars=self._config.request_bars("30m"),
                        )
                    analyses[frequency] = self._load_analysis(
                        exchange=None,
                        code=sector_id,
                        analysis_code=sector_id,
                        frequency=frequency,
                        as_of=observed_at,
                        sector_source=cast(str, catalog_source),
                        frame_override=raw_sector_frame,
                    )
                contexts = {
                    frequency: classify_context(
                        frequency=frequency,
                        current_direction=analyses[frequency].direction,
                        points=analyses[frequency].confirmed_points,
                        as_of=analyses[frequency].closed_at,
                    )
                    for frequency in _SECTOR_FREQUENCIES
                }
                context_time = max(analysis.closed_at for analysis in analyses.values())
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
                        market_data_source="qmt_gics3_component_composite",
                        thirty=contexts["30m"],
                        five=contexts["5m"],
                        one=one,
                        data_complete=True,
                    )
                )
                for frequency, analysis in analyses.items():
                    latest_bars[(sector_id, frequency)] = analysis.closed_at
                completed_count += 1
            except SectorAnalysisUnavailable as exc:
                failure = SectorAnalysisFailure(
                    sector_id=sector_id,
                    code=cast(str, source_key),
                    error_type=exc.code,
                    reason=str(exc),
                )
                errors.append(failure)
                LogUtil.error(
                    "[trading_screening.sector] "
                    f"sector={sector_id} frequency={current_frequency} "
                    "provider=qmt-gics3-composite "
                    f"error_type={failure.error_type} reason={failure.reason}"
                )
                assessments.append(
                    SectorAssessment(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        rank_components=(),
                        reason_codes=(failure.error_type,),
                    )
                )
            except Exception as exc:
                failure = SectorAnalysisFailure(
                    sector_id=sector_id,
                    code=cast(str, source_key),
                    error_type="sector_adapter_error",
                    reason=str(exc),
                )
                errors.append(failure)
                LogUtil.error(
                    "[trading_screening.sector] "
                    f"sector={sector_id} frequency={current_frequency} "
                    "provider=qmt-gics3-composite "
                    f"error_type={failure.error_type} reason={failure.reason}"
                )
                assessments.append(
                    SectorAssessment(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        rank_components=(),
                        reason_codes=(failure.error_type,),
                    )
                )
        strength_evidence: SectorStrengthBatch | None = None
        if self._sector_strength_provider is not None:
            try:
                self._report_progress()
                # ``observed_at`` 是工作进程墙钟。午夜后回放可能在周二观察目录，但全部
                # 已完成 30m/5m K 线仍属于周一收盘。板块强度使用同一已完成价格事实，
                # 决策时间必须取其已验证截止点而非进程时钟；否则因果有效的周一快照会
                # 被误标为周二并被不可变复核边界拒绝。``latest_bars`` 只包含已确认不晚于
                # ``observed_at`` 的分析。
                strength_decision_time = max(
                    latest_bars.values(),
                    default=observed_at,
                )
                strengths = self._sector_strength_provider(
                    members_by_sector=members_by_sector,
                    as_of=strength_decision_time,
                    membership_revision=catalog_revision,
                )
                self._report_progress()
                if not isinstance(strengths, Mapping):
                    raise TypeError("sector strength provider must return a mapping")
                if isinstance(strengths, SectorStrengthBatch):
                    strength_evidence = strengths
                assessments = [
                    replace(
                        assessment,
                        horizontal_strength=(
                            None
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].strength
                        ),
                        horizontal_rank=(
                            None
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].rank
                        ),
                        strength_anchor_session=(
                            None
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].anchor_session
                        ),
                        strength_member_count=(
                            0
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].member_count
                        ),
                        strength_source_revision=(
                            None
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].source_revision
                        ),
                        strength_reason_codes=(
                            ("SECTOR_STRENGTH_RESULT_MISSING",)
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].reason_codes
                        ),
                    )
                    for assessment in assessments
                ]
            except Exception as exc:
                LogUtil.error(
                    "[trading_screening.sector_strength] "
                    f"error_type=sector_strength_unavailable reason={str(exc)[:160]}"
                )
                assessments = [
                    replace(
                        assessment,
                        strength_reason_codes=("SECTOR_STRENGTH_PROVIDER_UNAVAILABLE",),
                    )
                    for assessment in assessments
                ]
        with self._lock:
            self._members = members_by_sector
            self._symbol_names = symbol_names
            self._latest_sector_bars = latest_bars
        ordered_errors = tuple(sorted(errors, key=lambda item: item.sector_id))
        ordered_exclusions = tuple(sorted(exclusions, key=lambda item: item.sector_id))
        failure_counts = tuple(
            sorted(Counter(item.error_type for item in ordered_errors).items())
        )
        exclusion_counts = tuple(
            sorted(Counter(item.reason_code for item in ordered_exclusions).items())
        )
        return SectorAssessmentBatch(
            assessments=tuple(sorted(assessments, key=lambda item: item.sector_id)),
            discovered_count=discovered_count,
            completed_count=completed_count,
            failure_counts=failure_counts,
            errors=ordered_errors,
            exclusion_counts=exclusion_counts,
            exclusions=ordered_exclusions,
            catalog_revision=catalog_revision,
            strength_evidence=strength_evidence,
        )

    def members(self) -> Mapping[str, tuple[str, ...]]:
        with self._lock:
            return dict(self._members)

    def changed_bars(self, since: datetime | None) -> tuple[BarKey, ...]:
        cutoff = (
            None if since is None else normalize_datetime(since, "changed bars cutoff")
        )
        with self._lock:
            changed = tuple(
                BarKey(code=code, frequency=frequency, closed_at=closed_at)
                for (code, frequency), closed_at in self._latest_sector_bars.items()
                if self._emitted_sector_bars.get((code, frequency)) != closed_at
                and (cutoff is None or closed_at > cutoff)
            )
            for item in changed:
                self._emitted_sector_bars[(item.code, item.frequency)] = item.closed_at
        return tuple(
            sorted(
                changed, key=lambda item: (item.closed_at, item.code, item.frequency)
            )
        )

    def active_watchlist(self) -> tuple[str, ...]:
        return self.active_watchlist_scope()[0]

    def active_watchlist_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = _stock_codes(self._watchlist_provider())
        eligible = self.tradable_instrument_codes(requested)
        return eligible, tuple(code for code in requested if code not in eligible)

    def holdings(self) -> tuple[str, ...]:
        return self.holdings_scope()[0]

    def holdings_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = _stock_codes(self._holdings_provider())
        eligible = self.tradable_instrument_codes(requested)
        return eligible, tuple(code for code in requested if code not in eligible)

    def tradable_instrument_codes(
        self,
        codes: tuple[str, ...],
    ) -> tuple[str, ...]:
        """只保留 QMT 原生 A 股股票和交易所交易基金。"""

        dispositions = self.screening_instrument_types(codes)
        return tuple(
            code
            for code in dispositions
            if dispositions[code] in _TRADABLE_SCREENING_INSTRUMENT_TYPES
        )

    def screening_instrument_types(
        self,
        codes: tuple[str, ...],
    ) -> Mapping[str, str]:
        """从统一证券目录读取并缓存精确原生类型。"""

        normalized = _stock_codes(codes)
        if not normalized:
            return {}
        with self._lock:
            result = {
                code: self._instrument_types[code]
                for code in normalized
                if code in self._instrument_types
            }
        missing = tuple(code for code in normalized if code not in result)
        if not missing:
            return {code: result[code] for code in normalized}
        provider = self._instrument_type_provider
        if provider is None:
            raise RuntimeError("instrument type catalog is unavailable")
        raw = provider(missing)
        if not isinstance(raw, Mapping) or set(raw) != set(missing):
            raise RuntimeError("instrument type catalog result is incomplete")
        if any(
            type(code) is not str
            or type(kind) is not str
            or kind not in _KNOWN_SCREENING_INSTRUMENT_TYPES
            for code, kind in raw.items()
        ):
            raise RuntimeError("instrument type catalog result is invalid")
        resolved = {code: str(raw[code]) for code in missing}

        stable = {
            code: kind for code, kind in resolved.items() if kind != "unresolved_cn"
        }
        if stable:
            with self._lock:
                self._instrument_types.update(stable)
        result.update(resolved)
        return {code: result[code] for code in normalized}

    def tick_probe(self, code: str) -> Mapping[str, object]:
        """在原生进程内探测一个 A 股实时报价，不泄露原生行情对象。"""

        if not isinstance(code, str) or _A_STOCK_CODE.fullmatch(code) is None:
            raise ValueError("tick probe requires an exact normalized A-share code")
        exchange = self._exchange_provider()
        market_open_probe = getattr(exchange, "now_trading", None)
        if not callable(market_open_probe):
            raise TypeError("exchange must expose now_trading")
        market_open = bool(market_open_probe("a"))
        if not market_open:
            return {
                "schema": "chanlun-native-tick-probe",
                "code": code,
                "status": "market_closed",
                "market_open": False,
                "usable": False,
                "tick_data_used": False,
                "real_account_access": False,
                "real_order_transport": False,
            }
        loader = getattr(exchange, "ticks", None)
        if not callable(loader):
            raise TypeError("exchange must expose ticks")
        self._report_progress()
        values = loader([code]) or {}
        self._report_progress()
        tick = values.get(code) if isinstance(values, Mapping) else None
        last = None if tick is None else getattr(tick, "last", None)
        usable = bool(
            isinstance(last, (Integral, float))
            and not isinstance(last, bool)
            and math.isfinite(float(last))
            and float(last) > 0
        )
        return {
            "schema": "chanlun-native-tick-probe",
            "code": code,
            "status": "ready" if usable else "empty",
            "market_open": True,
            "usable": usable,
            "tick_data_used": True,
            "real_account_access": False,
            "real_order_transport": False,
        }

    def symbol_name(self, code: str) -> str | None:
        with self._lock:
            cached = self._symbol_names.get(code)
        if cached is not None:
            return cached
        # 三级行业成员关系不含展示名称；仅为实际产生复核记录的代码读取静态标的信息，
        # 绝不使用全量逐笔数据决定名称或标的池成员。
        try:
            provider = getattr(self._exchange_provider(), "stock_info", None)
            if not callable(provider):
                return None
            self._report_progress()
            raw = provider(code)
            self._report_progress()
            name = raw.get("name") if isinstance(raw, Mapping) else None
            if not isinstance(name, str) or not name.strip():
                return None
            normalized = name.strip()
            with self._lock:
                self._symbol_names.setdefault(code, normalized)
            return normalized
        except Exception as exc:
            LogUtil.warning(
                "[trading_screening.symbol_name] "
                f"code={code} error={type(exc).__name__}: {str(exc)[:160]}"
            )
            return None

    def trading_session_evidence(
        self,
        *,
        session: date,
        observed_at: datetime,
    ) -> Mapping[str, object]:
        """通过同一个只读 QMT 边界返回交易日历证据。"""

        provider = self._trading_session_provider
        if provider is None:
            raise RuntimeError("QMT trading session provider is unavailable")
        observed = normalize_datetime(observed_at, "observed_at")
        self._report_progress()
        result = provider(session=session, observed_at=observed)
        self._report_progress()
        if not isinstance(result, Mapping):
            raise TypeError("trading session provider returned an invalid document")
        return dict(result)

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        sector_members: tuple[str, ...] | None = None,
        frequencies: tuple[str, ...] | None = None,
        higher_timeframe_as_of: datetime | None = None,
    ) -> SymbolStructureBundle:
        if _A_STOCK_CODE.fullmatch(code) is None:
            raise ValueError("invalid A-share code")
        observed_at = normalize_datetime(as_of, "as_of")
        exchange = self._exchange_provider()
        requested = set(_FREQUENCIES if frequencies is None else frequencies)
        if not requested or not requested.issubset(_FREQUENCIES):
            raise ValueError("frequencies must contain only d, 30m, 5m and 1m")
        analyses: dict[str, FrameStructureAnalysis] = {}
        # 共享决策核心从“当前 5m 设置”开始，必须先检查这一必要条件。缺失时，30m/日级
        # 背景、1m 精细触发和月/周/日风险事实都无法生成决策记录，无需读取或计算。
        # 这只是执行剪枝：``_has_current_five_minute_setup`` 与统一决策核心内部的
        # 5m 时效契约一致；返回证据包仍携带精确 5m 证据，由共享核心独立证明结果为空。
        cached_five = self._cached_analysis(code, "5m")
        analyses["5m"] = (
            self._load_analysis(
                exchange=exchange,
                code=code,
                analysis_code=code,
                frequency="5m",
                as_of=observed_at,
            )
            if "5m" in requested or cached_five is None
            else cached_five
        )
        has_current_five_minute_setup = self._has_current_five_minute_setup(
            analyses["5m"]
        )
        if has_current_five_minute_setup:
            for frequency in ("d", "30m"):
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
            if "1m" in requested:
                analyses["1m"] = self._load_analysis(
                    exchange=exchange,
                    code=code,
                    analysis_code=code,
                    frequency="1m",
                    as_of=observed_at,
                )
        bundle_as_of = max(item.closed_at for item in analyses.values())
        # 低级别 1m 精细通道可合理晚于最新已完成板块 5m K 线（如 09:47 对 09:45）。
        # 信号保留在该已完成 1m 前缀上，而全部月/周/日风险事实冻结到页面统一行情截止点。
        # 若复用信号墙钟，会让收敛、对账和来源覆盖证据描述 09:47，尽管原子板块快照
        # 冻结于 09:45，导致文档写入后立即无法通过自身因果校验。
        risk_as_of = bundle_as_of
        if higher_timeframe_as_of is not None:
            requested_risk_as_of = normalize_datetime(
                higher_timeframe_as_of,
                "higher_timeframe_as_of",
            )
            decision_as_of = normalize_datetime(as_of, "as_of")
            if requested_risk_as_of > decision_as_of:
                raise ValueError(
                    "higher_timeframe_as_of cannot be after decision as_of"
                )
            risk_as_of = min(bundle_as_of, requested_risk_as_of)
        higher_timeframe_gates = None
        # 决策核心为每个当前 5m 设置输出一个结果。已完成 5m 前缀没有当前设置时，
        # 无论月/周/日闸门如何，输出都可证明为空元组，因此避免为该空分支读取并重采样
        # 约 300 个交易日的 QMT 1m 历史。这仅是执行剪枝；所有可能产生候选的标的仍使用
        # 完全相同的高周期提供器，且下方入场闸门继续关闭失败。
        if (
            self._higher_timeframe_provider is not None
            and has_current_five_minute_setup
        ):
            resolved_sector_members = (
                self._members.get(sector.sector_id)
                if sector_members is None
                else sector_members
            )
            higher_timeframe_cache_key = (
                code,
                risk_as_of.isoformat(),
                sector.sector_id,
                sector.sector_name,
                sha256_json(
                    {
                        "sector_members": list(resolved_sector_members or ()),
                    }
                ),
            )
            with self._lock:
                higher_timeframe_gates = self._higher_timeframe_cache.get(
                    higher_timeframe_cache_key
                )
            try:
                if higher_timeframe_gates is None:
                    self._report_progress()
                    higher_timeframe_gates = self._higher_timeframe_provider(
                        symbol=code,
                        as_of=risk_as_of,
                        sector_id=sector.sector_id,
                        sector_name=sector.sector_name,
                        sector_members=resolved_sector_members,
                    )
                    self._report_progress()
                if not isinstance(
                    higher_timeframe_gates,
                    HigherTimeframeGateBundle,
                ):
                    raise TypeError(
                        "higher timeframe provider returned an invalid bundle"
                    )
                if all(
                    evidence.gate != "UNRESOLVED"
                    for evidence in (
                        higher_timeframe_gates.market,
                        higher_timeframe_gates.sector,
                        higher_timeframe_gates.symbol,
                    )
                ):
                    with self._lock:
                        if len(self._higher_timeframe_cache) >= 4096:
                            self._higher_timeframe_cache.pop(
                                next(iter(self._higher_timeframe_cache))
                            )
                        self._higher_timeframe_cache[higher_timeframe_cache_key] = (
                            higher_timeframe_gates
                        )
            except HigherTimeframeDataUnavailable as exc:
                LogUtil.error(
                    "[trading_screening.higher_timeframe.data] "
                    f"code={code} reason_codes={','.join(exc.reason_codes)}"
                )
                higher_timeframe_gates = unresolved_higher_timeframe_gates(
                    symbol=code,
                    observed_at=risk_as_of,
                    reason_codes=exc.reason_codes,
                    session_evidence=HigherTimeframeSessionEvidence.exact(
                        exc.session_issues
                    ),
                    sector_subject=sector.sector_id,
                )
            except Exception as exc:
                LogUtil.error(
                    "[trading_screening.higher_timeframe] "
                    f"code={code} reason={str(exc)[:160]}"
                )
                higher_timeframe_gates = unresolved_higher_timeframe_gates(
                    symbol=code,
                    observed_at=risk_as_of,
                    reason_code="QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
                    sector_subject=sector.sector_id,
                )
        confirmed = tuple(
            point
            for analysis in analyses.values()
            for point in analysis.confirmed_points
        )
        warmup_by_frequency = tuple(
            (
                frequency,
                analyses[frequency].warmup_converged,
                analyses[frequency].warmup_full_bar_count,
                analyses[frequency].warmup_suffix_bar_count,
            )
            for frequency in ("d", "30m", "5m", "1m")
            if frequency in analyses
        )
        warmup_reasons = tuple(
            dict.fromkeys(
                f"{frequency.upper()}:{reason}"
                for frequency in ("d", "30m", "5m", "1m")
                if frequency in analyses
                for reason in analyses[frequency].warmup_reason_codes
            )
        )
        warmup_difference_codes_by_frequency = tuple(
            (
                frequency,
                analyses[frequency].warmup_difference_codes,
            )
            for frequency in ("d", "30m", "5m", "1m")
            if frequency in analyses
        )
        return SymbolStructureBundle(
            code=code,
            as_of=bundle_as_of,
            sector=sector,
            daily_direction=(
                "neutral" if "d" not in analyses else analyses["d"].direction
            ),
            daily_points=(
                () if "d" not in analyses else analyses["d"].confirmed_points
            ),
            thirty_direction=(
                "neutral" if "30m" not in analyses else analyses["30m"].direction
            ),
            thirty_points=(
                () if "30m" not in analyses else analyses["30m"].confirmed_points
            ),
            five_points=(
                *analyses["5m"].confirmed_points,
                *analyses["5m"].provisional_points,
            ),
            one_points=(
                () if "1m" not in analyses else analyses["1m"].confirmed_points
            ),
            opposite_points=confirmed,
            higher_timeframe_gates=higher_timeframe_gates,
            enforce_higher_timeframe_entry_gate=(
                self._higher_timeframe_provider is not None
            ),
            warmup_converged=all(
                analysis.warmup_converged for analysis in analyses.values()
            ),
            warmup_reason_codes=warmup_reasons,
            warmup_by_frequency=warmup_by_frequency,
            warmup_difference_codes_by_frequency=(warmup_difference_codes_by_frequency),
            enforce_warmup_entry_gate=True,
            physical_timeframe_recursive=True,
            entry_execution_boundaries=(
                ()
                if "1m" not in analyses
                else analyses["1m"].entry_execution_boundaries
            ),
        )


__all__ = (
    "CANONICAL_REQUEST_BARS_BY_FREQUENCY",
    "FrameStructureAnalysis",
    "NativeTradingDataGateway",
    "NativeTradingGatewayConfig",
    "SectorAnalysisExclusion",
    "SectorAnalysisFailure",
    "SectorAnalysisUnavailable",
    "SectorAssessmentBatch",
    "StrictStructureAnalysisError",
    "_sector_failure_document",
    "_sector_exclusion_document",
    "analyze_native_frame",
    "analyze_native_frame_with_warmup",
    "audit_native_frame_warmup_envelope",
)
