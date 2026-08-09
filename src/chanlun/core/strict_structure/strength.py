from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from typing import Literal

import numpy as np

from chanlun.core.strict_structure.identity import (
    complete_c_measurement_id,
    stable_structure_id,
)
from chanlun.core.strict_structure.models import (
    CenterState,
    ConstituentUnit,
    Direction,
    DivergenceEvidence,
    TrendCenter,
)


@dataclass(frozen=True, slots=True)
class StrengthSnapshot:
    unit_id: str
    direction: Direction
    histogram_area: float
    histogram_peak: float
    dif_extreme: float
    source: Literal["macd_htf", "macd_native"]
    available_at: datetime

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise ValueError("unit_id is required")
        if self.direction not in ("up", "down"):
            raise ValueError("direction must be up or down")
        values = (self.histogram_area, self.histogram_peak, self.dif_extreme)
        if any(not isfinite(float(value)) for value in values):
            raise ValueError("strength values must be finite")
        if self.histogram_area <= 0:
            raise ValueError("histogram_area must be positive")
        if self.direction == "up" and self.histogram_peak <= 0:
            raise ValueError("up strength peak must be positive")
        if self.direction == "down" and self.histogram_peak >= 0:
            raise ValueError("down strength peak must be negative")
        if self.source not in ("macd_htf", "macd_native"):
            raise ValueError("unsupported MACD strength source")


class MacdStrengthUnavailable(ValueError):
    """The requested structural unit has no aligned directional MACD slice."""


class MacdStrengthProvider:
    """Read a causal, source-bar-aligned MACD strength series.

    Formal structural evidence always uses native MACD.  The legacy HTF
    interpolation rewrites already visible source bars while its current
    higher-timeframe bucket is still open, so it is available only through an
    explicit diagnostic opt-in and must never back a locked divergence.
    """

    def __init__(self, cd, *, allow_noncausal_htf: bool = False) -> None:
        self._dates = tuple(kline.date for kline in cd.get_src_klines())
        if not self._dates:
            raise ValueError("source K-line dates are required")
        if any(
            self._dates[index] >= self._dates[index + 1]
            for index in range(len(self._dates) - 1)
        ):
            raise ValueError("source K-line dates must be strictly increasing")

        native = cd.get_idx()["macd"]
        self._validate_series(native, "native MACD")
        chosen = native
        source: Literal["macd_htf", "macd_native"] = "macd_native"

        htf = getattr(cd, "_htf_macd", None)
        if (
            allow_noncausal_htf
            and isinstance(htf, dict)
            and tuple(htf.get("dates", ())) == self._dates
        ):
            try:
                self._validate_series(htf, "HTF MACD")
            except ValueError:
                pass
            else:
                chosen = htf
                source = "macd_htf"

        self._hist = np.asarray(chosen["hist"], dtype=float).copy()
        self._dif = np.asarray(chosen["dif"], dtype=float).copy()
        self._source = source

    def _validate_series(self, values: dict, label: str) -> None:
        for key in ("hist", "dif"):
            if key not in values:
                raise ValueError(f"{label} requires {key}")
            array = np.asarray(values[key], dtype=float)
            if array.ndim != 1 or len(array) != len(self._dates):
                raise ValueError(f"{label} must align one-to-one with source K lines")

    def snapshot(self, unit: ConstituentUnit) -> StrengthSnapshot:
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

        hist = self._hist[left:right]
        dif = self._dif[left:right]
        if not np.isfinite(hist).all() or not np.isfinite(dif).all():
            raise ValueError("MACD slice must be finite")
        if unit.direction == "up":
            directional = hist[hist > 0]
            if directional.size == 0:
                raise MacdStrengthUnavailable(
                    "unit has no directional MACD bars"
                )
            area = float(directional.sum())
            peak = float(directional.max())
            dif_extreme = float(dif.max())
        else:
            directional = hist[hist < 0]
            if directional.size == 0:
                raise MacdStrengthUnavailable(
                    "unit has no directional MACD bars"
                )
            area = float(abs(directional.sum()))
            peak = float(directional.min())
            dif_extreme = float(dif.min())
        return StrengthSnapshot(
            unit_id=unit.unit_id,
            direction=unit.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif_extreme,
            source=self._source,
            available_at=unit.available_at,
        )


def completed_center_departure_leg(
    center: TrendCenter,
    units,
) -> ConstituentUnit | None:
    """Return the completed same-direction unit after a center's third point.

    A center's external leave alone is not the complete comparable trend leg.
    The leg must also contain the first outside return (the third-class event)
    and the immediately following same-direction completed unit.  The latter
    is the comparable lower-level走势; ``child_ids`` records the entire
    leave/return/signal proof without incorrectly measuring MACD over the
    opposite-direction return.
    """

    values = tuple(units)
    if (
        center.state is not CenterState.COMPLETED
        or center.completion_leave_unit is None
        or center.completion_return_unit is None
        or center.source_kind.value == "stroke_observation"
    ):
        return None
    index = {item.unit_id: offset for offset, item in enumerate(values)}
    leave_index = index.get(center.completion_leave_unit.unit_id)
    return_index = index.get(center.completion_return_unit.unit_id)
    if (
        leave_index is None
        or return_index != leave_index + 1
        or return_index + 1 >= len(values)
    ):
        return None
    terminal_index = return_index + 1
    leg_units = values[leave_index : terminal_index + 1]
    leave, ret, terminal = leg_units
    if (
        not all(item.locked and item.confirmed_at is not None for item in leg_units)
        or leave.direction != terminal.direction
        or ret.direction == leave.direction
        or any(
            current.market_start < previous.market_end
            or previous.end_tick != current.start_tick
            for previous, current in zip(leg_units, leg_units[1:])
        )
    ):
        return None
    prior_extreme = (
        max(item.high_tick for item in leg_units[:-1])
        if terminal.direction == "up"
        else min(item.low_tick for item in leg_units[:-1])
    )
    terminal_extreme = terminal.high_tick if terminal.direction == "up" else terminal.low_tick
    if (
        terminal_extreme <= prior_extreme
        if terminal.direction == "up"
        else terminal_extreme >= prior_extreme
    ):
        # The divergence boundary is the terminal endpoint.  If the earlier
        # leave already owns c's extreme, the following unit has not yet
        # completed a new comparable c leg.
        return None
    child_ids = tuple(item.unit_id for item in leg_units)
    return ConstituentUnit(
        unit_id=complete_c_measurement_id(
            price_basis_revision=terminal.price_basis_revision,
            structural_level=terminal.structural_level,
            source_kind=terminal.source_kind.value,
            child_unit_ids=child_ids,
        ),
        structural_level=terminal.structural_level,
        source_kind=terminal.source_kind,
        price_basis_revision=terminal.price_basis_revision,
        direction=terminal.direction,
        # This object is an auditable strength-measurement view.  Endpoint
        # geometry remains the raw terminal unit, while the range and time
        # window cover leave -> outside first return -> terminal.
        start_tick=terminal.start_tick,
        end_tick=terminal.end_tick,
        low_tick=min(item.low_tick for item in leg_units),
        high_tick=max(item.high_tick for item in leg_units),
        market_start=leave.market_start,
        market_end=terminal.market_end,
        confirmed_at=max(item.confirmed_at for item in leg_units),
        available_at=max(item.available_at for item in leg_units),
        locked=True,
        child_ids=child_ids,
    )


def compare_terminal_trend_divergence(
    centers,
    units,
    provider,
    *,
    trend_start_unit_id: str,
) -> tuple[DivergenceEvidence, ConstituentUnit] | None:
    """Compare the complete departure legs after the last two same-level centers."""

    values = tuple(centers)
    if len(values) < 2 or provider is None:
        return None
    earlier = completed_center_departure_leg(values[-2], units)
    later = completed_center_departure_leg(values[-1], units)
    if (
        earlier is None
        or later is None
        or earlier.direction != later.direction
        or earlier.market_end > later.market_start
    ):
        return None
    evidence = compare_divergence(earlier, later, provider, kind="trend")
    unit_index = {item.unit_id: index for index, item in enumerate(units)}
    start_index = unit_index.get(trend_start_unit_id)
    terminal_index = unit_index.get(later.child_ids[-1])
    if start_index is None or terminal_index is None or start_index >= terminal_index:
        raise ValueError("trend divergence span is missing from source units")
    prior_units = tuple(units)[start_index:terminal_index]
    makes_trend_extreme = (
        later.high_tick > max(item.high_tick for item in prior_units)
        if later.direction == "up"
        else later.low_tick < min(item.low_tick for item in prior_units)
    )
    if not makes_trend_extreme:
        evidence = replace(evidence, price_extreme_confirmed=False)
    return evidence, later


def compare_divergence(
    earlier: ConstituentUnit,
    later: ConstituentUnit,
    provider,
    *,
    kind: Literal["trend", "consolidation"],
) -> DivergenceEvidence:
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
        peak_decayed = later_strength.histogram_peak < earlier_strength.histogram_peak
        dif_decayed = later_strength.dif_extreme < earlier_strength.dif_extreme
    else:
        new_extreme = later.low_tick < earlier.low_tick
        peak_decayed = later_strength.histogram_peak > earlier_strength.histogram_peak
        dif_decayed = later_strength.dif_extreme > earlier_strength.dif_extreme

    return DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence/v3",
            later.price_basis_revision,
            later.structural_level,
            later.source_kind.value,
            kind,
            later.direction,
            earlier.unit_id,
            later.unit_id,
        ),
        structural_level=later.structural_level,
        source_kind=later.source_kind,
        price_basis_revision=later.price_basis_revision,
        kind=kind,
        direction=later.direction,
        compare_unit_id=earlier.unit_id,
        signal_unit_id=later.unit_id,
        anchor_at=later.market_end,
        anchor_tick=(
            later.high_tick if later.direction == "up" else later.low_tick
        ),
        confirmed_at=later.confirmed_at,
        available_at=max(
            earlier_strength.available_at,
            later_strength.available_at,
        ),
        price_extreme_confirmed=new_extreme,
        histogram_area_decayed=(
            later_strength.histogram_area < earlier_strength.histogram_area
        ),
        histogram_peak_decayed=peak_decayed,
        dif_extreme_decayed=dif_decayed,
        strength_source=later_strength.source,
    )
