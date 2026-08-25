from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    ConstituentUnit,
    SourceKind,
    TrendState,
    TrendType,
)


class UnitLockRegistry:
    """记录每个稳定单元首次获得的因果确认时间。"""

    def __init__(self, price_basis_revision: str) -> None:
        if (
            not isinstance(price_basis_revision, str)
            or not price_basis_revision.strip()
            or price_basis_revision != price_basis_revision.strip()
        ):
            raise ValueError("price_basis_revision is required")
        self.price_basis_revision = price_basis_revision
        self._confirmed_at: dict[str, datetime] = {}

    def confirmed_at(self, unit_id: str, locked_at: datetime) -> datetime:
        previous = self._confirmed_at.setdefault(unit_id, locked_at)
        if previous != locked_at:
            raise ValueError("locked unit confirmation time changed")
        return previous


def _normalize_quantum(price_quantum) -> Decimal:
    try:
        value = Decimal(str(price_quantum))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("price_quantum must be positive") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("price_quantum must be positive")
    return value


def _tick(value, price_quantum: Decimal) -> int:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("line endpoint must be a finite price") from exc
    if not normalized.is_finite():
        raise ValueError("line endpoint must be a finite price")
    return int(
        (normalized / price_quantum).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def line_to_unit(
    line,
    structural_level: int,
    source_kind: SourceKind,
    price_quantum,
    as_of: datetime,
    registry: UnitLockRegistry,
) -> ConstituentUnit:
    source_kind = SourceKind(source_kind)
    if source_kind is SourceKind.TREND_TYPE:
        raise ValueError("line adapter does not build trend-type units")
    quantum = _normalize_quantum(price_quantum)
    market_start = line.start.k.date
    market_end = line.end.k.date
    if market_end > as_of:
        raise ValueError("line endpoint cannot exceed as_of")
    locked = bool(line.is_done())
    forming = bool(getattr(line, "forming", False))
    if locked and forming:
        raise ValueError("done line cannot still be forming")
    locked_at = getattr(line, "locked_at", None)
    if locked and locked_at is None:
        raise ValueError("done line requires causal locked_at")
    if not locked and locked_at is not None:
        raise ValueError("unfinished line cannot have locked_at")
    if locked and locked_at < market_end:
        raise ValueError("locked_at must not precede line end")
    if locked and locked_at > as_of:
        raise ValueError("locked_at cannot exceed as_of")
    formed_at = getattr(line, "formed_at", None)
    if forming and formed_at is not None:
        raise ValueError("forming line cannot have formed_at")
    if not forming:
        # Legacy/synthetic locked lines did not carry a separate geometry
        # timestamp.  Their lock is a safe (although conservative) fallback.
        formed_at = formed_at or locked_at
        if formed_at is not None and formed_at < market_end:
            raise ValueError("formed_at must not precede line end")
        if formed_at is not None and formed_at > as_of:
            raise ValueError("formed_at cannot exceed as_of")
        if locked_at is not None and formed_at > locked_at:
            raise ValueError("formed_at cannot exceed locked_at")

    start_index = line.start.k.k_index
    end_index = line.end.k.k_index
    start_tick = _tick(line.start.val, quantum)
    end_tick = _tick(line.end.val, quantum)
    unit_id = stable_structure_id(
        "chanlun-unit",
        registry.price_basis_revision,
        structural_level,
        source_kind,
        line.type,
        start_index,
        end_index,
        start_tick,
        end_tick,
    )
    confirmed_at = registry.confirmed_at(unit_id, locked_at) if locked else None
    available_at = confirmed_at if locked else (formed_at or max(as_of, market_end))
    return ConstituentUnit(
        unit_id=unit_id,
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=registry.price_basis_revision,
        direction=line.type,
        start_tick=start_tick,
        end_tick=end_tick,
        low_tick=min(start_tick, end_tick),
        high_tick=max(start_tick, end_tick),
        market_start=market_start,
        market_end=market_end,
        confirmed_at=confirmed_at,
        available_at=available_at,
        locked=locked,
        child_ids=(),
        forming=forming,
        formed_at=formed_at,
    )


def adapt_lines(
    lines,
    structural_level: int,
    source_kind: SourceKind,
    price_quantum,
    as_of: datetime,
    registry: UnitLockRegistry,
) -> tuple[ConstituentUnit, ...]:
    quantum = _normalize_quantum(price_quantum)
    return tuple(
        line_to_unit(
            line,
            structural_level,
            source_kind,
            quantum,
            as_of,
            registry,
        )
        for line in lines
    )


def trend_type_to_unit(trend: TrendType) -> ConstituentUnit:
    if not trend.locked:
        raise ValueError("only locked trend types can recurse")
    return ConstituentUnit(
        unit_id=trend.trend_id,
        structural_level=trend.structural_level + 1,
        source_kind=SourceKind.TREND_TYPE,
        price_basis_revision=trend.price_basis_revision,
        direction=trend.direction,
        start_tick=trend.start_tick,
        end_tick=trend.end_tick,
        low_tick=trend.low_tick,
        high_tick=trend.high_tick,
        market_start=trend.market_start,
        market_end=trend.market_end,
        confirmed_at=trend.confirmed_at,
        available_at=trend.available_at,
        locked=True,
        child_ids=tuple(item.unit_id for item in trend.constituent_units),
        forming=False,
        formed_at=trend.confirmed_at,
    )


def trend_type_to_observation_unit(trend: TrendType) -> ConstituentUnit:
    """把尚未锁定的当前走势转换为高级别只读观察单元。"""

    if trend.state is TrendState.LOCKED:
        raise ValueError("locked trend type must use the formal unit adapter")
    return ConstituentUnit(
        unit_id=trend.trend_id,
        structural_level=trend.structural_level + 1,
        source_kind=SourceKind.TREND_TYPE,
        price_basis_revision=trend.price_basis_revision,
        direction=trend.direction,
        start_tick=trend.start_tick,
        end_tick=trend.end_tick,
        low_tick=trend.low_tick,
        high_tick=trend.high_tick,
        market_start=trend.market_start,
        market_end=trend.market_end,
        confirmed_at=None,
        available_at=trend.available_at,
        locked=False,
        child_ids=tuple(item.unit_id for item in trend.constituent_units),
        forming=trend.state is TrendState.FORMING,
        formed_at=(trend.confirmed_at if trend.state is TrendState.COMPLETE else None),
    )


def build_recursive_unit_stream(
    current_trends: tuple[TrendType, ...],
    protected_after_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[ConstituentUnit, ...], frozenset[str]]:
    """构造高级别正式前缀及其连续的未锁定观察尾部。"""

    # 延迟导入，避免单元适配器与同级别结合模块在加载阶段形成循环依赖。
    from chanlun.core.strict_structure.center_machine import validate_unit_sequence
    from chanlun.core.strict_structure.same_level_decomposition import (
        combine_same_level_trends,
    )

    trends = tuple(current_trends)
    unlocked_seen = False
    for trend in trends:
        if trend.state is not TrendState.LOCKED:
            unlocked_seen = True
        elif unlocked_seen:
            raise ValueError("locked recursive trends must form a prefix")

    locked = tuple(trend for trend in trends if trend.state is TrendState.LOCKED)
    observations = tuple(
        trend for trend in trends if trend.state is not TrendState.LOCKED
    )
    locked_units = tuple(trend_type_to_unit(trend) for trend in locked)
    decomposition = combine_same_level_trends(
        locked_units,
        frozenset(),
        protected_after_ids,
    )
    output = list(decomposition.units)

    for trend in observations:
        candidate = trend_type_to_observation_unit(trend)
        try:
            validate_unit_sequence(
                tuple((*output, candidate)),
                candidate.structural_level,
                SourceKind.TREND_TYPE,
                frozenset(),
            )
        except ValueError as exc:
            if str(exc) == "unit directions must alternate":
                # 同向的未锁定走势将来可能按结合律并入前一单元。在身份冻结之前
                # 不制造高级别观察单元，避免临时合并改写正式递归前缀。
                break
            raise
        output.append(candidate)
    return tuple(output), frozenset()


__all__ = (
    "UnitLockRegistry",
    "adapt_lines",
    "build_recursive_unit_stream",
    "line_to_unit",
    "trend_type_to_observation_unit",
    "trend_type_to_unit",
)
