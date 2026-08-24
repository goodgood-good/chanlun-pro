from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from typing import Literal

import numpy as np

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    ConstituentUnit,
    Direction,
    DivergenceEvidence,
    SourceKind,
    TrendCenter,
)


@dataclass(frozen=True, slots=True)
class StrengthSnapshot:
    unit_id: str
    direction: Direction
    histogram_area: float | None
    histogram_peak: float | None
    dif_extreme: float
    source: Literal["macd"]
    available_at: datetime

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise ValueError("unit_id is required")
        if self.direction not in ("up", "down"):
            raise ValueError("direction must be up or down")
        values = tuple(
            value
            for value in (
                self.histogram_area,
                self.histogram_peak,
                self.dif_extreme,
            )
            if value is not None
        )
        if any(not isfinite(float(value)) for value in values):
            raise ValueError("strength values must be finite")
        if self.histogram_area is not None and self.histogram_area <= 0:
            raise ValueError("histogram_area must be positive")
        if (
            self.histogram_peak is not None
            and self.direction == "up"
            and self.histogram_peak <= 0
        ):
            raise ValueError("up strength peak must be positive")
        if (
            self.histogram_peak is not None
            and self.direction == "down"
            and self.histogram_peak >= 0
        ):
            raise ValueError("down strength peak must be negative")
        if self.source != "macd":
            raise ValueError("unsupported MACD strength source")


class MacdStrengthUnavailable(ValueError):
    """请求的结构单元没有与其对齐的方向性 MACD 切片。"""


class FormalDivergenceUnavailable(ValueError):
    """比较腿仍含未锁定单元，因而尚不能发布正式背驰。"""


@dataclass(frozen=True, slots=True)
class ComparisonMeasurement:
    """三段背驰比较腿使用的临时市场区间。

    比较腿方向由第一段和末段来源单元决定。同向的“进入—反向—再进入”序列，
    从首段起价到末段终价的净位移未必保持该方向，因此不能如实用
    ``ConstituentUnit`` 表达。此视图保留真实端点和完整价格包络，同时提供力度
    数据源所需的区间与方向。
    """

    unit_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    direction: Direction
    start_tick: int
    end_tick: int
    low_tick: int
    high_tick: int
    market_start: datetime
    market_end: datetime
    confirmed_at: datetime
    available_at: datetime
    locked: bool
    child_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise ValueError("comparison measurement unit_id is required")
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("comparison measurement level must be non-negative")
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("comparison measurement price basis is required")
        if self.direction not in ("up", "down"):
            raise ValueError("comparison measurement direction must be up or down")
        ticks = (self.start_tick, self.end_tick, self.low_tick, self.high_tick)
        if any(type(tick) is not int for tick in ticks):
            raise TypeError("comparison measurement ticks must be integers")
        if (
            self.low_tick > self.high_tick
            or not self.low_tick <= self.start_tick <= self.high_tick
            or not self.low_tick <= self.end_tick <= self.high_tick
        ):
            raise ValueError("comparison measurement endpoints must fit its range")
        if self.market_end < self.market_start:
            raise ValueError("comparison measurement time range is reversed")
        if not self.locked:
            raise ValueError("comparison measurement must be locked")
        if self.confirmed_at < self.market_end:
            raise ValueError("comparison confirmation precedes its market interval")
        if self.available_at < self.confirmed_at:
            raise ValueError("comparison availability precedes confirmation")
        child_ids = tuple(self.child_ids)
        object.__setattr__(self, "child_ids", child_ids)
        if (
            len(child_ids) != 3
            or len(set(child_ids)) != 3
            or any(not isinstance(child_id, str) or not child_id for child_id in child_ids)
        ):
            raise ValueError("comparison measurement requires three unique source units")


@dataclass(frozen=True, slots=True)
class ComparisonLeg:
    """背驰比较使用的一段或三段同级别走势。

    ``measurement_unit`` 是完整市场时间区间上的不可变视图；``units`` 保留
    精确来源证明，使聚合三段腿永远不会被误认为一个递归来源单元。
    """

    units: tuple[ConstituentUnit, ...]
    measurement_unit: ConstituentUnit | ComparisonMeasurement

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", tuple(self.units))
        if len(self.units) not in (1, 3):
            raise ValueError("comparison leg width must be one or three")
        if len({item.unit_id for item in self.units}) != len(self.units):
            raise ValueError("comparison leg source units must be unique")
        terminal = self.units[-1]
        if (
            self.measurement_unit.direction != terminal.direction
            or self.measurement_unit.structural_level != terminal.structural_level
            or self.measurement_unit.source_kind is not terminal.source_kind
            or self.measurement_unit.price_basis_revision
            != terminal.price_basis_revision
        ):
            raise ValueError("comparison measurement must match its terminal unit")
        if len(self.units) == 1 and self.measurement_unit != terminal:
            raise ValueError("one-unit comparison leg must preserve the source unit")
        if len(self.units) == 3 and self.measurement_unit.child_ids != tuple(
            item.unit_id for item in self.units
        ):
            raise ValueError("three-unit comparison measurement lost its source proof")

    @property
    def width(self) -> int:
        return len(self.units)

    @property
    def terminal_unit(self) -> ConstituentUnit:
        return self.units[-1]


class MacdStrengthProvider:
    """在来源 ``CL`` 的原生 MACD 上测量每个结构单元。

    递归层级表示结构关系，不等于固定的物理 K 线周期。L0、L1 以及更高层
    因而读取同一条原生序列，差别只来自各单元覆盖的精确来源 K 线区间。
    """

    def __init__(self, cd) -> None:
        self._dates = tuple(kline.date for kline in cd.get_src_klines())
        if not self._dates:
            raise ValueError("source K-line dates are required")
        if any(
            self._dates[index] >= self._dates[index + 1]
            for index in range(len(self._dates) - 1)
        ):
            raise ValueError("source K-line dates must be strictly increasing")

        index_values = cd.get_idx()
        if not isinstance(index_values, dict):
            raise ValueError("native MACD index result is invalid")
        native = index_values.get("macd")
        if not isinstance(native, dict):
            raise ValueError("native MACD index result is unavailable")
        self._validate_series(native, "native MACD")
        self._hist_series = np.asarray(native["hist"], dtype=float).copy()
        self._dif_series = np.asarray(native["dif"], dtype=float).copy()

    def _validate_series(self, values: dict, label: str) -> None:
        for key in ("hist", "dif"):
            if key not in values:
                raise ValueError(f"{label} requires {key}")
            array = np.asarray(values[key], dtype=float)
            if array.ndim != 1 or len(array) != len(self._dates):
                raise ValueError(f"{label} must align one-to-one with source K lines")

    def snapshot(
        self, unit: ConstituentUnit | ComparisonMeasurement
    ) -> StrengthSnapshot:
        left = bisect_left(self._dates, unit.market_start)
        right = bisect_right(self._dates, unit.market_end)
        if (
            left >= right
            or left >= len(self._dates)
            or self._dates[left] != unit.market_start
            or self._dates[right - 1] != unit.market_end
        ):
            raise MacdStrengthUnavailable(
                "unit market interval must align with source K lines"
            )

        selected_indexes = np.arange(left, right, dtype=int)
        hist = self._hist_series[selected_indexes]
        dif = self._dif_series[selected_indexes]
        if not np.isfinite(hist).all() or not np.isfinite(dif).all():
            raise ValueError("MACD slice must be finite")
        if unit.direction == "up":
            directional = hist[hist > 0]
            area = None if directional.size == 0 else float(directional.sum())
            peak = None if directional.size == 0 else float(directional.max())
            dif_extreme = float(dif.max())
        else:
            directional = hist[hist < 0]
            area = None if directional.size == 0 else float(abs(directional.sum()))
            peak = None if directional.size == 0 else float(directional.min())
            dif_extreme = float(dif.min())
        return StrengthSnapshot(
            unit_id=unit.unit_id,
            direction=unit.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif_extreme,
            source="macd",
            available_at=max(unit.available_at, self._dates[right - 1]),
        )


def _comparison_measurement(
    leg_units: tuple[ConstituentUnit, ...],
) -> ConstituentUnit | ComparisonMeasurement:
    if len(leg_units) == 1:
        return leg_units[0]
    if len(leg_units) != 3:
        raise ValueError("comparison measurement requires one or three units")
    first, _reverse, terminal = leg_units
    child_ids = tuple(item.unit_id for item in leg_units)
    confirmed_at = tuple(item.confirmed_at for item in leg_units)
    if any(value is None for value in confirmed_at):
        raise ValueError("comparison measurement requires locked source units")
    return ComparisonMeasurement(
        unit_id=stable_structure_id(
            "chanlun-comparison-leg",
            terminal.price_basis_revision,
            terminal.structural_level,
            terminal.source_kind.value,
            terminal.direction,
            child_ids,
        ),
        structural_level=terminal.structural_level,
        source_kind=terminal.source_kind,
        price_basis_revision=terminal.price_basis_revision,
        direction=terminal.direction,
        start_tick=first.start_tick,
        end_tick=terminal.end_tick,
        low_tick=min(item.low_tick for item in leg_units),
        high_tick=max(item.high_tick for item in leg_units),
        market_start=first.market_start,
        market_end=terminal.market_end,
        confirmed_at=max(value for value in confirmed_at if value is not None),
        available_at=max(item.available_at for item in leg_units),
        locked=True,
        child_ids=child_ids,
    )


def _comparison_leg(
    leg_units: tuple[ConstituentUnit, ...],
) -> ComparisonLeg:
    return ComparisonLeg(
        units=leg_units,
        measurement_unit=_comparison_measurement(leg_units),
    )


def _is_contiguous_three_leg(values: tuple[ConstituentUnit, ...]) -> bool:
    if len(values) != 3:
        return False
    first, reverse, terminal = values
    return (
        all(item.locked and item.confirmed_at is not None for item in values)
        and first.direction == terminal.direction
        and reverse.direction != first.direction
        and all(
            previous.structural_level == current.structural_level
            and previous.source_kind is current.source_kind
            and previous.price_basis_revision == current.price_basis_revision
            and previous.end_tick == current.start_tick
            and current.market_start >= previous.market_end
            for previous, current in zip(values, values[1:])
        )
    )


def comparison_leg_from_units(units) -> ComparisonLeg:
    """从来源证据构建精确的一段或三段背驰比较腿。"""

    values = tuple(units)
    if len(values) not in (1, 3):
        raise ValueError("comparison leg width must be one or three")
    if len(values) == 3 and not _is_contiguous_three_leg(values):
        raise ValueError(
            "three-unit comparison leg must be contiguous enter/reverse/re-enter"
        )
    return _comparison_leg(values)


def center_entry_comparison_leg(
    center: TrendCenter,
    units,
    *,
    not_before_unit_id: str | None = None,
) -> ComparisonLeg | None:
    """返回中枢正式的一段或三段进入走势。

    三段进入是以 ``center.entry_unit`` 结束的同级别“进入—反向—再进入”序列。
    第一段必须严格位于冻结中枢区间的进入侧之外；仅触碰 ``ZD`` 或 ``ZG`` 也
    视为重叠，此时进入段保持一段宽度。
    """

    if center.source_kind.value == "stroke_observation" or center.entry_unit is None:
        return None
    values = tuple(units)
    index = {item.unit_id: offset for offset, item in enumerate(values)}
    entry_index = index.get(center.entry_unit.unit_id)
    lower_bound = 0 if not_before_unit_id is None else index.get(not_before_unit_id)
    if entry_index is None or lower_bound is None or entry_index < lower_bound:
        return None
    entry = values[entry_index]
    if entry != center.entry_unit or not entry.locked or entry.confirmed_at is None:
        return None
    if entry_index - 2 >= lower_bound:
        candidate = values[entry_index - 2 : entry_index + 1]
        first = candidate[0]
        strictly_outside = (
            first.high_tick < center.zd_tick
            if entry.direction == "up"
            else first.low_tick > center.zg_tick
        )
        if _is_contiguous_three_leg(candidate) and strictly_outside:
            return _comparison_leg(candidate)
    return _comparison_leg((entry,))


def center_departure_comparison_leg(
    center: TrendCenter,
    units,
    *,
    width: int,
) -> ComparisonLeg | None:
    """返回与进入腿所选宽度完全一致的离开腿。"""

    if width not in (1, 3):
        raise ValueError("departure comparison width must be one or three")
    if center.source_kind.value == "stroke_observation":
        return None
    signal = center.lifecycle_leave_unit
    if signal is None or not signal.locked or signal.confirmed_at is None:
        return None
    if width == 1:
        return _comparison_leg((signal,))

    values = tuple(units)
    index = {item.unit_id: offset for offset, item in enumerate(values)}
    leave_index = index.get(signal.unit_id)
    if (
        center.completion_leave_unit != signal
        or center.completion_return_unit is None
        or leave_index is None
        or leave_index + 2 >= len(values)
    ):
        return None
    candidate = values[leave_index : leave_index + 3]
    leave, ret, terminal = candidate
    if (
        ret != center.completion_return_unit
        or not _is_contiguous_three_leg(candidate)
    ):
        return None
    # A three-unit departure is one comparison leg.  Its price extreme belongs
    # to the whole ``leave -> return -> terminal`` interval, not necessarily to
    # the terminal unit by itself.  Requiring the terminal unit to extend the
    # first leave incorrectly rejects the common second-test shape (a higher
    # low after a downward leave, or a lower high after an upward leave).
    # ``compare_comparison_legs`` validates the aggregate leg extreme below.
    return _comparison_leg(candidate)


def compare_terminal_trend_divergence(
    centers,
    units,
    provider,
    *,
    trend_start_unit_id: str,
) -> tuple[DivergenceEvidence, ConstituentUnit] | None:
    """使用末端中枢进入腿确定的宽度比较 A/C 段。"""

    values = tuple(centers)
    if len(values) < 2 or provider is None:
        return None
    source_units = tuple(units)
    earlier = center_entry_comparison_leg(
        values[-1],
        source_units,
        not_before_unit_id=trend_start_unit_id,
    )
    later = (
        None
        if earlier is None
        else center_departure_comparison_leg(
            values[-1],
            source_units,
            width=earlier.width,
        )
    )
    if (
        earlier is None
        or later is None
        or earlier.measurement_unit.direction != later.measurement_unit.direction
        or earlier.measurement_unit.market_end > later.measurement_unit.market_start
    ):
        return None
    evidence = compare_comparison_legs(earlier, later, provider, kind="trend")
    terminal = later.terminal_unit
    unit_index = {item.unit_id: index for index, item in enumerate(source_units)}
    start_index = unit_index.get(trend_start_unit_id)
    terminal_index = unit_index.get(terminal.unit_id)
    signal_start_index = unit_index.get(later.units[0].unit_id)
    if start_index is None or terminal_index is None or start_index >= terminal_index:
        raise ValueError("trend divergence span is missing from source units")
    if signal_start_index is None or signal_start_index <= start_index:
        raise ValueError("trend divergence signal leg is missing its prior span")
    prior_units = source_units[start_index:signal_start_index]
    signal = later.measurement_unit
    makes_trend_extreme = (
        signal.high_tick > max(item.high_tick for item in prior_units)
        if terminal.direction == "up"
        else signal.low_tick < min(item.low_tick for item in prior_units)
    )
    if not makes_trend_extreme:
        evidence = replace(evidence, price_extreme_confirmed=False)
    return evidence, terminal


def compare_divergence(
    earlier: ConstituentUnit,
    later: ConstituentUnit,
    provider,
    *,
    kind: Literal["trend", "consolidation"],
) -> DivergenceEvidence:
    return compare_comparison_legs(
        _comparison_leg((earlier,)),
        _comparison_leg((later,)),
        provider,
        kind=kind,
    )


def compare_comparison_legs(
    earlier_leg: ComparisonLeg,
    later_leg: ComparisonLeg,
    provider,
    *,
    kind: Literal["trend", "consolidation"],
) -> DivergenceEvidence:
    if earlier_leg.width != later_leg.width:
        raise ValueError("divergence comparison legs must have the same width")
    earlier = earlier_leg.measurement_unit
    later = later_leg.measurement_unit
    if earlier.unit_id == later.unit_id:
        raise ValueError("divergence units must be distinct")
    if (
        earlier.structural_level != later.structural_level
        or earlier.source_kind is not later.source_kind
        or earlier.price_basis_revision != later.price_basis_revision
    ):
        raise ValueError("divergence units must share level, source, and price basis")
    if not earlier.locked or not later.locked:
        raise FormalDivergenceUnavailable(
            "formal divergence requires locked units"
        )
    if earlier.confirmed_at is None or later.confirmed_at is None:
        raise ValueError("formal divergence requires confirmation timestamps")
    if earlier.direction != later.direction:
        raise ValueError("divergence units must share direction")
    if earlier.market_end > later.market_start:
        raise ValueError("divergence units must be time ordered")

    earlier_strength = provider.snapshot(earlier)
    later_strength = provider.snapshot(later)
    if (
        earlier_strength.unit_id != earlier.unit_id
        or later_strength.unit_id != later.unit_id
        or earlier_strength.direction != earlier.direction
        or later_strength.direction != later.direction
    ):
        raise ValueError("strength snapshot does not match its structural unit")
    if earlier_strength.source != later_strength.source:
        raise ValueError("divergence strength sources must match")

    if later.direction == "up":
        new_extreme = later.high_tick > earlier.high_tick
        peak_decayed = (
            earlier_strength.histogram_peak is not None
            and later_strength.histogram_peak is not None
            and later_strength.histogram_peak < earlier_strength.histogram_peak
        )
        dif_decayed = later_strength.dif_extreme < earlier_strength.dif_extreme
    else:
        new_extreme = later.low_tick < earlier.low_tick
        peak_decayed = (
            earlier_strength.histogram_peak is not None
            and later_strength.histogram_peak is not None
            and later_strength.histogram_peak > earlier_strength.histogram_peak
        )
        dif_decayed = later_strength.dif_extreme > earlier_strength.dif_extreme

    area_decayed = (
        earlier_strength.histogram_area is not None
        and later_strength.histogram_area is not None
        and later_strength.histogram_area < earlier_strength.histogram_area
    )

    return DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence",
            later.price_basis_revision,
            later.structural_level,
            later.source_kind.value,
            kind,
            later.direction,
            tuple(item.unit_id for item in earlier_leg.units),
            tuple(item.unit_id for item in later_leg.units),
        ),
        structural_level=later.structural_level,
        source_kind=later.source_kind,
        price_basis_revision=later.price_basis_revision,
        kind=kind,
        direction=later.direction,
        compare_unit_id=earlier_leg.terminal_unit.unit_id,
        signal_unit_id=later_leg.terminal_unit.unit_id,
        anchor_at=later_leg.terminal_unit.market_end,
        anchor_tick=(
            later_leg.terminal_unit.high_tick
            if later.direction == "up"
            else later_leg.terminal_unit.low_tick
        ),
        confirmed_at=later_leg.terminal_unit.confirmed_at,
        available_at=max(
            earlier_strength.available_at,
            later_strength.available_at,
        ),
        price_extreme_confirmed=new_extreme,
        histogram_area_decayed=area_decayed,
        histogram_peak_decayed=peak_decayed,
        dif_extreme_decayed=dif_decayed,
        strength_source=later_strength.source,
        compare_leg_unit_ids=tuple(item.unit_id for item in earlier_leg.units),
        signal_leg_unit_ids=tuple(item.unit_id for item in later_leg.units),
    )
