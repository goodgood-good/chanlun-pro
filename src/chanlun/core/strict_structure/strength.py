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
    """The requested structural unit has no aligned directional MACD slice."""


@dataclass(frozen=True, slots=True)
class ComparisonMeasurement:
    """Transient market interval for a three-unit divergence leg.

    The leg direction is owned by its first and terminal source units.  A
    same-direction ``enter/reverse/re-enter`` sequence need not have the same
    *net* displacement from the first start price to the terminal end price,
    so it cannot be represented honestly by ``ConstituentUnit``.  This view
    keeps the real endpoints and full price envelope while supplying the
    interval and direction needed by the strength provider.
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
    """One- or three-unit same-level movement used by a divergence comparison.

    ``measurement_unit`` is an immutable view over the complete market-time
    interval.  ``units`` retains the exact source proof so an aggregate
    three-unit leg can never be mistaken for one recursive source unit.
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
    """Read source-aligned MACD for one unified structural-level policy.

    Physical level 0 uses the native MACD of the source K-line frequency.
    Recursive level N uses the Nth causal partial higher-frequency series, so
    structural recursion never changes the meaning of level 0 evidence.
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

        self._series_by_level = {}
        index_values = cd.get_idx()
        if not isinstance(index_values, dict):
            raise ValueError("native MACD index result is invalid")
        native = index_values.get("macd")
        if not isinstance(native, dict):
            raise ValueError("native MACD index result is unavailable")
        self._validate_series(native, "native MACD")
        self._series_by_level[0] = (
            np.asarray(native["hist"], dtype=float).copy(),
            np.asarray(native["dif"], dtype=float).copy(),
            self._dates,
            None,
        )

        level_series = getattr(cd, "_strict_htf_macd_by_level", None)
        if level_series is None:
            return
        if not isinstance(level_series, dict):
            raise ValueError("causal HTF MACD level map is invalid")
        for level, candidate in level_series.items():
            if type(level) is not int or level < 0 or not isinstance(candidate, dict):
                raise ValueError("causal HTF MACD level entry is invalid")
            bucket_keys = tuple(candidate.get("bucket_keys", ()))
            if (
                candidate.get("algorithm") != "causal-partial-htf"
                or tuple(candidate.get("dates", ())) != self._dates
                or tuple(candidate.get("known_at", ())) != self._dates
                or len(bucket_keys) != len(self._dates)
                or any(
                    bucket_keys[index] > bucket_keys[index + 1]
                    for index in range(len(bucket_keys) - 1)
                )
            ):
                raise ValueError("causal HTF MACD context is invalid")
            self._validate_series(candidate, "causal level HTF MACD")
            self._series_by_level[level + 1] = (
                np.asarray(candidate["hist"], dtype=float).copy(),
                np.asarray(candidate["dif"], dtype=float).copy(),
                tuple(candidate["known_at"]),
                bucket_keys,
            )

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
        selected = self._series_by_level.get(unit.structural_level)
        if selected is None:
            raise MacdStrengthUnavailable(
                "MACD is unavailable for structural level"
            )
        hist_series, dif_series, known_at, bucket_keys = selected
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

        selected_indexes = tuple(range(left, right))
        if bucket_keys is not None:
            # ``causal-partial-htf`` emits one provisional value per source
            # bar so a live endpoint is available immediately.  MACD area is
            # nevertheless an HTF-bar area: count each covered target bucket
            # once, using the last value visible at this unit's endpoint.
            last_by_bucket: dict[object, int] = {}
            for index in selected_indexes:
                last_by_bucket[bucket_keys[index]] = index
            selected_indexes = tuple(sorted(last_by_bucket.values()))
        hist = hist_series[np.asarray(selected_indexes, dtype=int)]
        dif = dif_series[np.asarray(selected_indexes, dtype=int)]
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
            available_at=max(
                unit.available_at,
                *(known_at[index] for index in selected_indexes),
            ),
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
    """Build an exact one- or three-unit divergence leg from source proof."""

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
    """Return the center's formal one- or three-unit incoming movement.

    A three-unit entry is the same-level ``enter/reverse/re-enter`` sequence
    ending at ``center.entry_unit``.  Its first unit must be strictly outside
    the frozen center interval on the incoming side.  Merely touching ``ZD``
    or ``ZG`` counts as overlap and therefore leaves the entry one unit wide.
    """

    if center.source_kind.value == "stroke_observation":
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
    """Return a departure with the exact width selected by the entry leg."""

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
    terminal_extends = (
        terminal.high_tick > max(leave.high_tick, ret.high_tick)
        if terminal.direction == "up"
        else terminal.low_tick < min(leave.low_tick, ret.low_tick)
    )
    if not terminal_extends:
        return None
    return _comparison_leg(candidate)


def compare_terminal_trend_divergence(
    centers,
    units,
    provider,
    *,
    trend_start_unit_id: str,
) -> tuple[DivergenceEvidence, ConstituentUnit] | None:
    """Compare A/C with the width selected by the terminal center entry."""

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
    if start_index is None or terminal_index is None or start_index >= terminal_index:
        raise ValueError("trend divergence span is missing from source units")
    prior_units = source_units[start_index:terminal_index]
    makes_trend_extreme = (
        terminal.high_tick > max(item.high_tick for item in prior_units)
        if terminal.direction == "up"
        else terminal.low_tick < min(item.low_tick for item in prior_units)
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
        raise ValueError("formal divergence requires locked units")
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
