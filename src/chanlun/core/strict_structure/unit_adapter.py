from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind


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
        (normalized / price_quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def _line_price_range(line, quantum, constituents):
    """第 78 课：线段实际区间取其组成笔，结构端点仍保留原坐标。

    ``zs_high/zs_low`` 是其他计算路径的显示字段，不是来源证据。没有声明
    组成笔的合成测试线仍按端点适配；真实线段不能在证据缺失时默默降级。
    """
    first = (_tick(line.start.val, quantum), line.start.k.date)
    last = (_tick(line.end.val, quantum), line.end.k.date)
    start_line = getattr(line, "start_line", None)
    end_line = getattr(line, "end_line", None)
    if start_line is None and end_line is None:
        points = (first, last)
    else:
        if start_line is None or end_line is None or constituents is None:
            raise ValueError("segment requires complete constituent stroke evidence")
        (start, end) = (start_line.index, end_line.index)
        if type(start) is not int or type(end) is not int or end < start:
            raise ValueError("segment constituent indices must be ordered integers")
        try:
            children = tuple((constituents[index] for index in range(start, end + 1)))
        except KeyError as exc:
            raise ValueError("segment constituent stroke evidence has a gap") from exc
        points = [first]
        for offset, child in enumerate(children):
            child_start = (_tick(child.start.val, quantum), child.start.k.date)
            child_end = (_tick(child.end.val, quantum), child.end.k.date)
            if child_start != points[-1] or child_end[1] < child_start[1]:
                raise ValueError("segment constituent strokes must be contiguous")
            expected_direction = (
                line.type if offset % 2 == 0 else "down" if line.type == "up" else "up"
            )
            if child.type != expected_direction:
                raise ValueError("segment constituent stroke directions must alternate")
            points.append(child_end)
        if points[-1] != last or children[-1].type != line.type:
            raise ValueError("segment constituents must exactly cover its endpoints")
    low = min((price for (price, _) in points))
    high = max((price for (price, _) in points))
    low_at = max((moment for (price, moment) in points if price == low))
    high_at = max((moment for (price, moment) in points if price == high))
    return (low, high, low_at, high_at)


def line_to_unit(
    line,
    structural_level: int,
    source_kind: SourceKind,
    price_quantum,
    as_of: datetime,
    registry: UnitLockRegistry,
    *,
    constituents: Mapping | None = None,
) -> ConstituentUnit:
    source_kind = SourceKind(source_kind)
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
    (low_tick, high_tick, low_at, high_at) = _line_price_range(
        line, quantum, constituents
    )
    endpoint_range = (
        min(start_tick, end_tick),
        max(start_tick, end_tick),
        market_end if end_tick <= start_tick else market_start,
        market_end if end_tick >= start_tick else market_start,
    )
    actual_range = (low_tick, high_tick, low_at, high_at)
    range_identity = () if actual_range == endpoint_range else actual_range
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
        *range_identity,
    )
    confirmed_at = registry.confirmed_at(unit_id, locked_at) if locked else None
    available_at = confirmed_at if locked else formed_at or max(as_of, market_end)
    return ConstituentUnit(
        unit_id=unit_id,
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=registry.price_basis_revision,
        direction=line.type,
        start_tick=start_tick,
        end_tick=end_tick,
        low_tick=low_tick,
        high_tick=high_tick,
        market_start=market_start,
        market_end=market_end,
        confirmed_at=confirmed_at,
        available_at=available_at,
        locked=locked,
        child_ids=(),
        forming=forming,
        formed_at=formed_at,
        low_market_time=low_at,
        high_market_time=high_at,
    )


def adapt_lines(
    lines,
    structural_level: int,
    source_kind: SourceKind,
    price_quantum,
    as_of: datetime,
    registry: UnitLockRegistry,
    *,
    constituent_lines=None,
) -> tuple[ConstituentUnit, ...]:
    quantum = _normalize_quantum(price_quantum)
    constituents = None
    if constituent_lines is not None:
        children = tuple(constituent_lines)
        constituents = {item.index: item for item in children}
        if len(constituents) != len(children):
            raise ValueError("constituent stroke indices must be unique")
    return tuple(
        (
            line_to_unit(
                line,
                structural_level,
                source_kind,
                quantum,
                as_of,
                registry,
                constituents=constituents,
            )
            for line in lines
        )
    )
