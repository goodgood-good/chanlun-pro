from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal, Mapping

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import (
    STRICT_POINT_TYPES,
    STRICT_POINT_TYPE_SET,
    StrictPointType,
)
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)


if TYPE_CHECKING:
    from chanlun.decision_support.trading_system.provisional import (
        ProvisionalCandidate,
    )


PointType = StrictPointType
# 决策层只引用严格结构核心发布的六类点协议，不再维护第二份类型清单。
CANONICAL_POINT_TYPES: tuple[PointType, ...] = STRICT_POINT_TYPES
CANONICAL_POINT_TYPE_SET: frozenset[PointType] = STRICT_POINT_TYPE_SET
# 人工复核时按类别成对展示，避免三类买点把一、二类卖点挤到队列末端。
POINT_REVIEW_ORDER: tuple[PointType, ...] = (
    "1buy",
    "1sell",
    "2buy",
    "2sell",
    "3buy",
    "3sell",
)
REVERSAL_SUPPORT_POINT_TYPES: frozenset[PointType] = frozenset(
    {"1buy", "2buy", "1sell", "2sell"}
)
CONTINUATION_SUPPORT_POINT_TYPES: frozenset[PointType] = frozenset(
    {"3buy", "3sell"}
)
# 1 分钟段差证据既可以由一、二类反转/回试点完成，也可以由满足严格中枢边界
# 间隔的三类延续点完成。它只用于 5 分钟正式买卖点之后的段差/精细定位，
# 不能反过来决定 5 分钟买卖点是否成立。
ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES: frozenset[PointType] = frozenset(
    (*REVERSAL_SUPPORT_POINT_TYPES, *CONTINUATION_SUPPORT_POINT_TYPES)
)
# 持久化文档和历史研究代码仍使用旧名称。保留只读别名，避免把旧档案误判为
# 损坏；新决策代码必须使用上面的“段差证据”名称和语义。
ONE_MINUTE_TRIGGER_POINT_TYPES = ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES
PointSide = Literal["buy", "sell"]
PointStatus = Literal["confirmed"]
PointVariant = Literal["standard", "strict", "weak_divergence", "boundary_touch"]
StructureTower = Literal["formal"]
ContextDirection = Literal["up", "down", "neutral"]
ContextDisposition = Literal["supportive", "neutral", "hostile"]
LifecycleStage = Literal[
    "observed",
    "approaching",
    "formed",
    "armed",
    "triggered",
    "executable",
    "active",
    "closed",
    "invalidated",
]
MAX_FIVE_MINUTE_SETUP_AGE_SECONDS = 4 * 24 * 60 * 60


ENTRY_EXECUTION_BOUNDARY_POLICY_ID = sha256_json(
    {
        "schema": "chanlun-human-entry-execution-boundary",
        "locator_frequency": "1m",
        "price_cap": "UNADJUSTED_CONFIRMATION_BAR_HIGH",
        "validity": (
            "NEXT_LOCATOR_BAR_CLOSE_OR_CURRENT_CONTINUOUS_AUCTION_END_FIRST"
        ),
        "signal_bar_fill_allowed": False,
        "price_chasing_allowed": False,
        "tick_data_used": False,
        "live_status": "LIVE_DISABLED",
    }
)


def build_point_id(
    *,
    code: str,
    price_basis_revision: str,
    point_type: PointType,
    source_frequency: str,
    tower: StructureTower,
    recursive_level: int,
    anchor_at: datetime,
    center_id: str | None,
    parent_point_id: str | None,
) -> str:
    if not price_basis_revision or not price_basis_revision.strip():
        raise ValueError("price_basis_revision is required")
    if tower != "formal" or type(recursive_level) is not int or recursive_level < 0:
        raise ValueError("invalid structure identity")
    return sha256_json(
        {
            "schema": "chanlun-structural-point",
            "code": code,
            "price_basis_revision": price_basis_revision,
            "point_type": point_type,
            "source_frequency": source_frequency,
            "tower": tower,
            "recursive_level": recursive_level,
            "anchor_at": normalize_datetime(anchor_at, "anchor_at").isoformat(),
            "center_id": center_id,
            "parent_point_id": parent_point_id,
        }
    )


@dataclass(frozen=True, slots=True)
class StructuralPoint:
    point_id: str
    code: str
    point_type: PointType
    side: PointSide
    status: PointStatus
    variant: PointVariant
    source_frequency: str
    price_basis_revision: str
    tower: StructureTower
    recursive_level: int
    anchor_at: datetime
    confirmed_at: datetime | None
    available_at: datetime
    structure_anchor_price: float
    structure_invalidation_price: float
    center_id: str | None
    center_zd: float | None
    center_zg: float | None
    center_ordinal: int | None
    divergence_kind: str | None
    parent_point_id: str | None
    evidence_codes: tuple[str, ...]
    related_point_ids: tuple[str, ...] = ()
    small_to_large_carrier_unit_ids: tuple[str, ...] = ()
    terminal_segment: TerminalSegmentReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("买卖点标的不能为空")
        if self.point_type not in CANONICAL_POINT_TYPE_SET:
            raise ValueError("买卖点类型无效")
        expected_side = "buy" if self.point_type.endswith("buy") else "sell"
        if self.side != expected_side:
            raise ValueError("point_type and side disagree")
        if (
            self.tower != "formal"
            or type(self.recursive_level) is not int
            or self.recursive_level < 0
        ):
            raise ValueError("invalid structure identity")
        if not isinstance(self.source_frequency, str) or not self.source_frequency:
            raise ValueError("买卖点物理周期不能为空")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        anchor_at = normalize_datetime(self.anchor_at, "anchor_at")
        object.__setattr__(self, "anchor_at", anchor_at)
        available_at = normalize_datetime(self.available_at, "available_at")
        if available_at < anchor_at:
            raise ValueError("available_at cannot precede anchor_at")
        object.__setattr__(self, "available_at", available_at)
        if self.confirmed_at is not None:
            confirmed_at = normalize_datetime(self.confirmed_at, "confirmed_at")
            if confirmed_at < anchor_at:
                raise ValueError("confirmed_at cannot precede anchor_at")
            if available_at < confirmed_at:
                raise ValueError("available_at cannot precede confirmed_at")
            object.__setattr__(self, "confirmed_at", confirmed_at)
        if self.status != "confirmed" or self.confirmed_at is None:
            raise ValueError("正式买卖点必须已经确认")
        if self.point_id != build_point_id(
            code=self.code,
            price_basis_revision=self.price_basis_revision,
            point_type=self.point_type,
            source_frequency=self.source_frequency,
            tower=self.tower,
            recursive_level=self.recursive_level,
            anchor_at=anchor_at,
            center_id=self.center_id,
            parent_point_id=self.parent_point_id,
        ):
            raise ValueError("买卖点身份与正式结构证据不一致")
        object.__setattr__(self, "evidence_codes", tuple(self.evidence_codes))
        object.__setattr__(self, "related_point_ids", tuple(self.related_point_ids))
        object.__setattr__(
            self,
            "small_to_large_carrier_unit_ids",
            tuple(self.small_to_large_carrier_unit_ids),
        )
        if any(
            not isinstance(point_id, str) or not point_id
            for point_id in self.related_point_ids
        ) or len(set(self.related_point_ids)) != len(self.related_point_ids):
            raise ValueError("related point ids must be unique non-empty strings")
        if self.point_id in self.related_point_ids:
            raise ValueError("structural point cannot reference itself")
        if self.terminal_segment is not None:
            if not isinstance(self.terminal_segment, TerminalSegmentReference):
                raise TypeError("terminal_segment must be a terminal reference")
            if (
                self.terminal_segment.structural_level != self.recursive_level
                or self.terminal_segment.market_end != anchor_at
            ):
                raise ValueError("structural point terminal lineage mismatch")
            if (
                self.terminal_segment.role != "latest_completed"
                or self.terminal_segment.state not in {"formed", "locked"}
                or self.terminal_segment.direction
                != ("down" if self.side == "buy" else "up")
            ):
                raise ValueError(
                    "confirmed point must belong to the formed latest completed segment"
                )
        if (self.center_zd is None) != (self.center_zg is None):
            raise ValueError("中枢上下沿必须同时存在")
        if (
            self.center_zd is not None
            and self.center_zg is not None
            and self.center_zd > self.center_zg
        ):
            raise ValueError("中枢区间无效")
        if self.point_type in {"1buy", "1sell"} and (
            self.variant != "standard"
            or self.divergence_kind not in {"trend", "consolidation"}
        ):
            raise ValueError("一类买卖点必须来自正式趋势背驰或盘整背驰")
        if self.point_type in {"2buy", "2sell"} and (
            self.variant not in {"strict", "weak_divergence"}
            or self.parent_point_id is None
            or (
                self.variant == "weak_divergence"
                and self.divergence_kind != "consolidation"
            )
            or self.variant == "strict"
            and self.divergence_kind is not None
        ):
            raise ValueError("二类买卖点缺少一类父点或形态证据")
        if self.point_type in {"3buy", "3sell"} and (
            self.variant not in {"standard", "boundary_touch"}
            or self.center_id is None
            or self.center_zd is None
            or self.center_zg is None
            or self.center_ordinal is None
            or self.divergence_kind is not None
        ):
            raise ValueError("三类买卖点必须保留完整中枢血缘")
        if self.point_type not in {"3buy", "3sell"} and self.center_ordinal is not None:
            raise ValueError("中枢序号只属于三类买卖点")
        is_small_to_large = "small_to_large_reversal" in self.evidence_codes
        if is_small_to_large:
            if (
                self.point_type not in {"2buy", "2sell"}
                or self.related_point_ids != (self.parent_point_id,)
                or len(self.small_to_large_carrier_unit_ids) != 3
                or len(set(self.small_to_large_carrier_unit_ids)) != 3
                or any(
                    not isinstance(unit_id, str) or not unit_id
                    for unit_id in self.small_to_large_carrier_unit_ids
                )
            ):
                raise ValueError(
                    "小转大二类点必须保留完整的离开、反向、再离开三段载体"
                )
        elif self.small_to_large_carrier_unit_ids:
            raise ValueError(
                "ordinary point cannot carry small-to-large structural evidence"
            )
        if (
            self.structure_anchor_price <= 0
            or self.structure_invalidation_price <= 0
        ):
            raise ValueError("prices must be positive")
        if (
            self.side == "buy"
            and self.structure_invalidation_price > self.structure_anchor_price
        ):
            raise ValueError("buy invalidation cannot be above point anchor")
        if (
            self.side == "sell"
            and self.structure_invalidation_price < self.structure_anchor_price
        ):
            raise ValueError("sell invalidation cannot be below point anchor")
        if self.point_type == "3buy" and (
            self.structure_anchor_price < self.center_zg
            or (self.variant == "boundary_touch")
            != (self.structure_anchor_price == self.center_zg)
        ):
            raise ValueError("三买回抽必须保持在中枢上沿之外")
        if self.point_type == "3sell" and (
            self.structure_anchor_price > self.center_zd
            or (self.variant == "boundary_touch")
            != (self.structure_anchor_price == self.center_zd)
        ):
            raise ValueError("三卖回抽必须保持在中枢下沿之外")
        if self.center_ordinal is not None and self.center_ordinal <= 0:
            raise ValueError("center_ordinal must be positive")
        if len(self.evidence_codes) != len(set(self.evidence_codes)):
            raise ValueError("evidence_codes must be unique")

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def structure_key(self) -> tuple[StructureTower, int, str | None]:
        return self.tower, self.recursive_level, self.center_id


_TERMINAL_SEGMENT_DOCUMENT_FIELDS = (
    "terminal_segment_role",
    "terminal_segment_level",
    "terminal_segment_id",
    "terminal_segment_source_kind",
    "terminal_segment_direction",
    "terminal_segment_state",
    "terminal_segment_start_at",
    "terminal_segment_end_at",
    "terminal_segment_available_at",
)


def _terminal_segment_reference_from_document(
    document: Mapping[str, object],
) -> TerminalSegmentReference | None:
    present = tuple(field in document for field in _TERMINAL_SEGMENT_DOCUMENT_FIELDS)
    if not any(present):
        return None
    if not all(present):
        raise ValueError("terminal segment document is incomplete")
    return TerminalSegmentReference(
        role=document["terminal_segment_role"],  # type: ignore[arg-type]
        structural_level=int(document["terminal_segment_level"]),
        unit_id=str(document["terminal_segment_id"]),
        source_kind=document["terminal_segment_source_kind"],  # type: ignore[arg-type]
        direction=document["terminal_segment_direction"],  # type: ignore[arg-type]
        state=document["terminal_segment_state"],  # type: ignore[arg-type]
        market_start=datetime.fromisoformat(
            str(document["terminal_segment_start_at"])
        ),
        market_end=datetime.fromisoformat(str(document["terminal_segment_end_at"])),
        available_at=datetime.fromisoformat(
            str(document["terminal_segment_available_at"])
        ),
    )


def structural_point_from_document(
    document: Mapping[str, object],
    *,
    code: str | None = None,
) -> StructuralPoint:
    """从标准决策文档恢复经过模型不变量复核的正式结构点。"""

    def strings(name: str) -> tuple[str, ...]:
        raw = document.get(name, ())
        if not isinstance(raw, (list, tuple)) or any(
            not isinstance(value, str) or not value for value in raw
        ):
            raise ValueError(f"structural point {name} is invalid")
        return tuple(raw)

    try:
        anchor_at = datetime.fromisoformat(str(document["anchor_at"]))
        confirmed_at = datetime.fromisoformat(str(document["confirmed_at"]))
        available_at = datetime.fromisoformat(str(document["available_at"]))
        point = StructuralPoint(
            point_id=str(document["point_id"]),
            code=str(document.get("code") or code or ""),
            point_type=document["point_type"],  # type: ignore[arg-type]
            side=document["side"],  # type: ignore[arg-type]
            status=document["status"],  # type: ignore[arg-type]
            variant=document["variant"],  # type: ignore[arg-type]
            source_frequency=str(document["source_frequency"]),
            price_basis_revision=str(document["price_basis_revision"]),
            tower=document["tower"],  # type: ignore[arg-type]
            recursive_level=int(document["recursive_level"]),
            anchor_at=anchor_at,
            confirmed_at=confirmed_at,
            available_at=available_at,
            structure_anchor_price=float(document["anchor_price"]),
            structure_invalidation_price=float(document["invalidation_price"]),
            center_id=(
                None
                if document.get("center_id") is None
                else str(document["center_id"])
            ),
            center_zd=(
                None
                if document.get("center_zd") is None
                else float(document["center_zd"])
            ),
            center_zg=(
                None
                if document.get("center_zg") is None
                else float(document["center_zg"])
            ),
            center_ordinal=(
                None
                if document.get("center_ordinal") is None
                else int(document["center_ordinal"])
            ),
            divergence_kind=(
                None
                if document.get("divergence_kind") is None
                else str(document["divergence_kind"])
            ),
            parent_point_id=(
                None
                if document.get("parent_point_id") is None
                else str(document["parent_point_id"])
            ),
            evidence_codes=strings("evidence_codes"),
            related_point_ids=strings("related_point_ids"),
            small_to_large_carrier_unit_ids=strings(
                "small_to_large_carrier_unit_ids"
            ),
            terminal_segment=_terminal_segment_reference_from_document(document),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("structural point document is invalid") from exc
    if document.get("actionable") is not True:
        raise ValueError("structural point document is not actionable")
    return point


@dataclass(frozen=True, slots=True)
class EntryExecutionBoundary:
    """用于约束一次可选入场的未复权确认 K 线事实。

    这是执行证据，不是结构价格。尤其是一买结构锚点通常是背驰段低点，绝不能
    复用为确认 K 线的可执行价格上界。
    """

    symbol: str
    point_id: str
    source_frequency: str
    confirmation_bar_closed_at: datetime
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    raw_volume: Decimal
    entry_valid_until: datetime
    raw_price_basis_revision: str
    policy_id: str = ENTRY_EXECUTION_BOUNDARY_POLICY_ID
    tick_data_used: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        for field in ("confirmation_bar_closed_at", "entry_valid_until"):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        if (
            not isinstance(self.symbol, str)
            or not self.symbol.strip()
            or not isinstance(self.point_id, str)
            or not self.point_id.strip()
        ):
            raise ValueError("entry execution boundary provenance is incomplete")
        if self.source_frequency != "1m":
            raise ValueError("entry execution boundary requires a 1m locator")
        expected_valid_until = a_share_optional_entry_valid_until(
            self.confirmation_bar_closed_at
        )
        if self.entry_valid_until != expected_valid_until:
            raise ValueError(
                "entry execution boundary validity must equal the frozen "
                "A-share locator-bar TTL"
            )
        prices = (self.raw_open, self.raw_high, self.raw_low, self.raw_close)
        if any(
            not isinstance(value, Decimal)
            or not value.is_finite()
            or value <= 0
            for value in prices
        ):
            raise ValueError("entry execution boundary prices are invalid")
        if (
            self.raw_low > min(self.raw_open, self.raw_close)
            or self.raw_high < max(self.raw_open, self.raw_close)
            or self.raw_low > self.raw_high
            or not isinstance(self.raw_volume, Decimal)
            or not self.raw_volume.is_finite()
            or self.raw_volume < 0
        ):
            raise ValueError("entry execution boundary OHLCV is inconsistent")
        if (
            not isinstance(self.raw_price_basis_revision, str)
            or not self.raw_price_basis_revision.strip()
        ):
            raise ValueError("entry execution raw price basis is required")
        if (
            self.policy_id != ENTRY_EXECUTION_BOUNDARY_POLICY_ID
            or self.tick_data_used
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("entry execution boundary policy changed")

    @property
    def evidence_id(self) -> str:
        return sha256_json(asdict(self))

    def document(self) -> dict[str, object]:
        stable = asdict(self)
        for field in ("confirmation_bar_closed_at", "entry_valid_until"):
            stable[field] = getattr(self, field).isoformat()
        for field in (
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "raw_volume",
        ):
            stable[field] = format(getattr(self, field), "f")
        return {**stable, "evidence_id": self.evidence_id}


def parse_entry_execution_boundary_document(
    raw: object,
) -> EntryExecutionBoundary:
    """解析并独立复核一个可移植的执行边界文档。"""

    field_names = tuple(field.name for field in fields(EntryExecutionBoundary))
    if not isinstance(raw, Mapping) or set(raw) != set(field_names) | {
        "evidence_id"
    }:
        raise ValueError("entry execution boundary document is malformed")
    values = {name: raw[name] for name in field_names}
    try:
        for name in ("confirmation_bar_closed_at", "entry_valid_until"):
            values[name] = datetime.fromisoformat(str(values[name]))
        for name in (
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "raw_volume",
        ):
            values[name] = Decimal(str(values[name]))
        boundary = EntryExecutionBoundary(**values)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            "entry execution boundary document is malformed"
        ) from exc
    if raw.get("evidence_id") != boundary.evidence_id:
        raise ValueError("entry execution boundary document identity changed")
    return boundary


@dataclass(frozen=True, slots=True)
class TimeframeContext:
    frequency: str
    direction: ContextDirection
    disposition: ContextDisposition
    hard_block: bool
    dominant_point_id: str | None
    dominant_point_type: PointType | None
    reason_codes: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if self.hard_block != (self.disposition == "hostile"):
            raise ValueError("hard_block must match hostile disposition")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must be unique")


@dataclass(frozen=True, slots=True)
class SectorAssessment:
    sector_id: str
    sector_name: str
    eligible: bool
    hard_block: bool
    regime: Literal["supportive", "neutral", "hostile"]
    rank_components: tuple[tuple[str, int], ...]
    reason_codes: tuple[str, ...]
    thirty_context: TimeframeContext | None = None
    five_context: TimeframeContext | None = None
    one_context: TimeframeContext | None = None
    # 横向强度只用于排序，不能把不利板块变成可入选板块；它与结构上下文保持
    # 分离，避免 QMT 历史缺失被静默解释成中性得分。
    horizontal_strength: Decimal | None = None
    horizontal_rank: int | None = None
    strength_anchor_session: date | None = None
    strength_member_count: int = 0
    strength_source_revision: str | None = None
    strength_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.eligible == self.hard_block:
            raise ValueError("eligible and hard_block must be opposites")
        names = tuple(name for name, _value in self.rank_components)
        if len(names) != len(set(names)):
            raise ValueError("rank component names must be unique")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must be unique")
        if self.strength_member_count < 0:
            raise ValueError("strength_member_count cannot be negative")
        if self.horizontal_rank is not None and self.horizontal_rank <= 0:
            raise ValueError("horizontal_rank must be positive")
        if self.horizontal_strength is not None and not self.horizontal_strength.is_finite():
            raise ValueError("horizontal_strength must be finite")
        resolved_strength = self.horizontal_strength is not None
        if resolved_strength != (self.horizontal_rank is not None):
            raise ValueError("sector strength and rank must resolve together")
        if resolved_strength and (
            self.strength_anchor_session is None
            or self.strength_member_count <= 0
            or not self.strength_source_revision
        ):
            raise ValueError("resolved sector strength provenance is incomplete")
        if len(self.strength_reason_codes) != len(set(self.strength_reason_codes)):
            raise ValueError("strength_reason_codes must be unique")

    @property
    def rank_score(self) -> int:
        return sum(value for _name, value in self.rank_components)


@dataclass(frozen=True, slots=True)
class RankedSector:
    ordinal: int
    assessment: SectorAssessment

    def __post_init__(self) -> None:
        if self.ordinal <= 0:
            raise ValueError("ordinal must be positive")


@dataclass(frozen=True, slots=True)
class TradeSetup:
    setup_id: str
    point: StructuralPoint | ProvisionalCandidate
    context: TimeframeContext
    sector: SectorAssessment
    started_at: datetime
    price_low: float
    price_high: float
    sector_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "started_at",
            normalize_datetime(self.started_at, "started_at"),
        )
        if self.price_low <= 0 or self.price_high <= 0:
            raise ValueError("setup prices must be positive")
        if self.price_low > self.price_high:
            raise ValueError("price_low cannot exceed price_high")
        if type(self.sector_required) is not bool:
            raise ValueError("sector_required must be a boolean")


@dataclass(frozen=True, slots=True)
class SignalLifecycle:
    signal_id: str
    setup_id: str
    stage: LifecycleStage
    observed_at: datetime
    trigger_point_id: str | None
    reason_codes: tuple[str, ...]
    actionable: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must be unique")


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    hard_block: bool
    blocking_point_ids: tuple[str, ...]
    risk_only_point_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.hard_block != bool(self.blocking_point_ids):
            raise ValueError("hard_block must match blocking points")
        combined = self.blocking_point_ids + self.risk_only_point_ids
        if len(combined) != len(set(combined)):
            raise ValueError("conflict point ids must be unique")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must be unique")


@dataclass(frozen=True, slots=True)
class TradingPolicy:
    require_confirmed_five_minute: bool = True
    # 5 分钟是正式买卖级别。1 分钟只提供段差/精细定位，不是信号成立硬门槛。
    # 字段为兼容既有参数档案而保留，生产冻结值必须为 False。
    require_confirmed_one_minute: bool = False
    require_sector_eligibility: bool = True
    require_thirty_minute_context: bool = True
    minimum_tick: Decimal = Decimal("0.01")
    first_buy_risk_multiplier: Decimal = Decimal("0.50")
    second_buy_risk_multiplier: Decimal = Decimal("1.00")
    third_buy_risk_multiplier: Decimal = Decimal("0.75")
    max_five_minute_setup_age_seconds: int = MAX_FIVE_MINUTE_SETUP_AGE_SECONDS

    def __post_init__(self) -> None:
        flags = (
            self.require_confirmed_five_minute,
            self.require_confirmed_one_minute,
            self.require_sector_eligibility,
            self.require_thirty_minute_context,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("trading policy flags must be booleans")
        decimals = (
            self.minimum_tick,
            self.first_buy_risk_multiplier,
            self.second_buy_risk_multiplier,
            self.third_buy_risk_multiplier,
        )
        if any(not isinstance(value, Decimal) for value in decimals):
            raise TypeError("trading policy price and risk values must be Decimals")
        if any(not value.is_finite() for value in decimals):
            raise ValueError("trading policy price and risk values must be finite")
        if self.minimum_tick <= 0:
            raise ValueError("minimum_tick must be positive")
        if (
            type(self.max_five_minute_setup_age_seconds) is not int
            or self.max_five_minute_setup_age_seconds <= 0
        ):
            raise ValueError(
                "max_five_minute_setup_age_seconds must be a positive integer"
            )
        if any(
            value < 0
            for value in (
                self.first_buy_risk_multiplier,
                self.second_buy_risk_multiplier,
                self.third_buy_risk_multiplier,
            )
        ):
            raise ValueError("risk multipliers cannot be negative")


@dataclass(frozen=True, slots=True)
class EntryDecision:
    allowed: bool
    signal_id: str
    risk_multiplier: Decimal
    structural_stop: Decimal | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExitDecision:
    allowed: bool
    signal_id: str
    action: Literal["none", "reduce_tactical", "exit_full"]
    reason_codes: tuple[str, ...]
