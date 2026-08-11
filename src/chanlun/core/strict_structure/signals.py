from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import (
    CenterRelation,
    CenterState,
    ConstituentUnit,
    SourceKind,
    StrictPointEvidence,
    StrictPointStatus,
    StrictPointVariant,
    StrictStructureResult,
    TrendKind,
    TrendCenter,
    TrendState,
    TrendType,
    build_strict_point_id,
)
from chanlun.core.strict_structure.strength import (
    MacdStrengthUnavailable,
    compare_divergence,
    compare_terminal_trend_divergence,
)
from chanlun.core.strict_structure.identity import stable_structure_id


def _approaching_point_id(
    *,
    price_basis_revision: str,
    point_type: str,
    structural_level: int,
    anchor_unit_id: str,
    center_id: str | None,
    parent_point_id: str | None,
) -> str:
    return stable_structure_id(
        "chanlun-strict-approaching",
        price_basis_revision,
        point_type,
        structural_level,
        anchor_unit_id,
        center_id,
        parent_point_id,
    )


def _locked_projection(unit: ConstituentUnit) -> ConstituentUnit:
    if unit.locked:
        return unit
    return replace(
        unit,
        locked=True,
        confirmed_at=unit.available_at,
    )


def _divergence_evidence_codes(divergence) -> tuple[str, ...]:
    codes = [
        f"comparison_leg_width_{divergence.comparison_width}",
        "macd_any_indicator_decay",
        f"strength_source_{divergence.strength_source}",
    ]
    if divergence.histogram_area_decayed:
        codes.append("macd_histogram_area_decay")
    if divergence.histogram_peak_decayed:
        codes.append("macd_histogram_peak_decay")
    if divergence.dif_extreme_decayed:
        codes.append("macd_dif_extreme_decay")
    return tuple(codes)


def center_ordinals(
    centers: tuple[TrendCenter, ...],
) -> dict[tuple[str, str], int]:
    """Number each center inside its strict same-direction trend run."""

    values = tuple(centers)
    if len({center.center_id for center in values}) != len(values):
        raise ValueError("center ids must be unique within a structural level")
    output: dict[tuple[str, str], int] = {}
    up_run = 1
    down_run = 1
    previous = None
    for center in values:
        if previous is not None:
            relation = classify_center_relation(previous, center)
            if relation is CenterRelation.UP_TREND:
                up_run += 1
                down_run = 1
            elif relation is CenterRelation.DOWN_TREND:
                down_run += 1
                up_run = 1
            else:
                up_run = down_run = 1
        output[(center.center_id, "up")] = up_run
        output[(center.center_id, "down")] = down_run
        previous = center
    return output


class StrictSignalEngine:
    def __init__(
        self,
        *,
        structure: StrictStructureResult,
        price_quantum: Decimal,
        strength=None,
    ) -> None:
        if not isinstance(structure, StrictStructureResult):
            raise TypeError("structure must be a StrictStructureResult")
        if not isinstance(price_quantum, Decimal) or price_quantum <= 0:
            raise ValueError("price_quantum must be a positive Decimal")
        self.structure = structure
        self.price_quantum = price_quantum
        self.strength = strength

    def confirmed_points(self) -> tuple[StrictPointEvidence, ...]:
        """Return every confirmed 1/2/3 point through one canonical pipeline.

        Callers must not independently combine the three point classes.  In
        particular, second-class recognition receives the exact first-class
        ledger produced in this invocation, so charting, replay and screening
        cannot drift through subtly different ordering or de-duplication.
        """

        first = self.first_class_points()
        candidates = (
            *first,
            *self.second_class_points(first),
            *self.third_class_points(),
        )
        by_id: dict[str, StrictPointEvidence] = {}
        for point in candidates:
            previous = by_id.setdefault(point.point_id, point)
            if previous != point:
                raise ValueError("strict point id maps to conflicting evidence")
        return tuple(
            sorted(
                by_id.values(),
                key=lambda point: (
                    point.available_at,
                    point.structural_level,
                    point.point_type,
                    point.point_id,
                ),
            )
        )

    def third_class_points(self) -> tuple[StrictPointEvidence, ...]:
        output = []
        for level in self.structure.levels:
            centers = tuple(level.center_result.centers)
            ordinals = center_ordinals(centers)
            for center in centers:
                if center.source_kind is SourceKind.STROKE_OBSERVATION:
                    continue
                if center.price_basis_revision != self.structure.price_basis_revision:
                    raise ValueError("strict point cannot cross price basis")
                if (
                    center.physically_completed
                    and center.completion_direction == "up"
                ):
                    point = self._third_class_point(
                        center,
                        direction="up",
                        ordinal=ordinals[(center.center_id, "up")],
                    )
                elif (
                    center.physically_completed
                    and center.completion_direction == "down"
                ):
                    point = self._third_class_point(
                        center,
                        direction="down",
                        ordinal=ordinals[(center.center_id, "down")],
                    )
                else:
                    point = None
                if point is not None:
                    output.append(point)
        return tuple(
            sorted(
                output,
                key=lambda point: (
                    point.available_at,
                    point.structural_level,
                    point.point_type,
                    point.point_id,
                ),
            )
        )

    def approaching_points(self, as_of: datetime) -> tuple[StrictPointEvidence, ...]:
        if not isinstance(as_of, datetime):
            raise TypeError("as_of must be a datetime")
        output = []
        for level in self.structure.levels:
            active = level.units[level.center_result.locked_unit_count :]
            if not active:
                continue
            tail = active[0]
            if tail.locked:
                raise ValueError("active structural tail must be unlocked")
            if tail.available_at > as_of:
                raise ValueError("active structural tail is available after as_of")
            third = self._approaching_third_class(level, tail)
            if third is not None:
                output.append(third)
            first = self._approaching_first_class(level, tail)
            if first is not None:
                output.append(first)
            output.extend(self._approaching_second_class(level, tail))

        unique = {}
        for point in output:
            if point.available_at > as_of:
                raise ValueError("approaching point is available after as_of")
            unique.setdefault(point.point_id, point)
        return tuple(
            sorted(
                unique.values(),
                key=lambda point: (
                    point.available_at,
                    point.structural_level,
                    point.point_type,
                    point.point_id,
                ),
            )
        )

    def _approaching_third_class(
        self,
        level,
        tail: ConstituentUnit,
    ) -> StrictPointEvidence | None:
        centers = tuple(level.center_result.centers)
        ordinals = center_ordinals(centers)
        for center in reversed(centers):
            if center.source_kind is SourceKind.STROKE_OBSERVATION:
                continue
            leave = center.pending_leave_unit
            if leave is None or not leave.locked:
                continue
            if (
                leave.end_tick != tail.start_tick
                or tail.market_start < leave.market_end
            ):
                continue
            if center.state is CenterState.ONGOING and leave.direction == "up":
                if tail.direction != "down" or tail.low_tick < center.zg_tick:
                    continue
                point_type = "3buy"
                side = "buy"
                anchor_tick = tail.low_tick
                invalidation_tick = center.zg_tick
                boundary_tick = center.zg_tick
                ordinal = ordinals[(center.center_id, "up")]
            elif center.state is CenterState.ONGOING and leave.direction == "down":
                if tail.direction != "up" or tail.high_tick > center.zd_tick:
                    continue
                point_type = "3sell"
                side = "sell"
                anchor_tick = tail.high_tick
                invalidation_tick = center.zd_tick
                boundary_tick = center.zd_tick
                ordinal = ordinals[(center.center_id, "down")]
            else:
                continue
            variant = (
                StrictPointVariant.BOUNDARY_TOUCH
                if anchor_tick == boundary_tick
                else StrictPointVariant.STANDARD
            )
            return StrictPointEvidence(
                point_id=_approaching_point_id(
                    price_basis_revision=center.price_basis_revision,
                    point_type=point_type,
                    structural_level=center.structural_level,
                    anchor_unit_id=tail.unit_id,
                    center_id=center.center_id,
                    parent_point_id=None,
                ),
                point_type=point_type,
                side=side,
                status=StrictPointStatus.APPROACHING,
                variant=variant,
                structural_level=center.structural_level,
                source_kind=center.source_kind,
                price_basis_revision=center.price_basis_revision,
                anchor_unit_id=tail.unit_id,
                anchor_at=tail.market_end,
                confirmed_at=None,
                available_at=max(center.available_at, tail.available_at),
                price_quantum=self.price_quantum,
                anchor_tick=anchor_tick,
                invalidation_tick=invalidation_tick,
                center_id=center.center_id,
                center_zd_tick=center.zd_tick,
                center_zg_tick=center.zg_tick,
                center_ordinal=ordinal,
                divergence=None,
                parent_point_id=None,
                evidence_codes=(
                    "formal_center",
                    "complete_leave",
                    "live_first_return",
                    "core_boundary_currently_held",
                ),
                missing_conditions=("terminal_unit_locked",),
            )
        return None

    def _approaching_first_class(
        self,
        level,
        tail: ConstituentUnit,
    ) -> StrictPointEvidence | None:
        if self.strength is None:
            return None
        for trend in reversed(level.trend_types):
            if (
                trend.kind is not TrendKind.TREND
                or len(trend.centers) < 2
                or trend.terminal_divergence is not None
                or tail.source_kind is SourceKind.STROKE_OBSERVATION
            ):
                continue
            last_center = trend.centers[-1]
            unit_index = {item.unit_id: index for index, item in enumerate(level.units)}
            tail_index = unit_index.get(tail.unit_id)
            if tail_index is None:
                continue
            projected_tail = _locked_projection(tail)
            if (
                last_center.state is CenterState.ONGOING
                and last_center.pending_leave_unit is None
            ):
                previous_index = unit_index.get(last_center.body_units[-1].unit_id)
                if previous_index is None or tail_index != previous_index + 1:
                    continue
                try:
                    projected_center = replace(
                        last_center,
                        pending_leave_unit=projected_tail,
                        available_at=max(
                            last_center.available_at,
                            projected_tail.available_at,
                        ),
                    )
                except ValueError:
                    continue
            elif last_center.state is CenterState.COMPLETED:
                ret = last_center.completion_return_unit
                previous_index = (
                    None if ret is None else unit_index.get(ret.unit_id)
                )
                if previous_index is None or tail_index != previous_index + 1:
                    continue
                projected_center = last_center
            else:
                continue
            projected_units = list(level.units)
            projected_units[tail_index] = projected_tail
            try:
                compared = compare_terminal_trend_divergence(
                    (*trend.centers[:-1], projected_center),
                    tuple(projected_units),
                    self.strength,
                    trend_start_unit_id=trend.constituent_units[0].unit_id,
                )
            except MacdStrengthUnavailable:
                continue
            if compared is None:
                continue
            divergence, signal = compared
            if not divergence.is_divergent:
                continue
            if signal.unit_id != tail.unit_id:
                raise ValueError("approaching first-class segment anchor changed")
            if divergence.direction == "down":
                point_type = "1buy"
                side = "buy"
                anchor_tick = divergence.anchor_tick
            else:
                point_type = "1sell"
                side = "sell"
                anchor_tick = divergence.anchor_tick
            return StrictPointEvidence(
                point_id=_approaching_point_id(
                    price_basis_revision=trend.price_basis_revision,
                    point_type=point_type,
                    structural_level=trend.structural_level,
                    anchor_unit_id=tail.unit_id,
                    center_id=last_center.center_id,
                    parent_point_id=None,
                ),
                point_type=point_type,
                side=side,
                status=StrictPointStatus.APPROACHING,
                variant=StrictPointVariant.STANDARD,
                structural_level=trend.structural_level,
                source_kind=tail.source_kind,
                price_basis_revision=trend.price_basis_revision,
                anchor_unit_id=tail.unit_id,
                anchor_at=tail.market_end,
                confirmed_at=None,
                available_at=max(
                    trend.available_at,
                    last_center.available_at,
                    tail.available_at,
                    divergence.available_at,
                ),
                price_quantum=self.price_quantum,
                anchor_tick=anchor_tick,
                invalidation_tick=anchor_tick,
                center_id=last_center.center_id,
                center_zd_tick=last_center.zd_tick,
                center_zg_tick=last_center.zg_tick,
                center_ordinal=None,
                divergence=divergence,
                parent_point_id=None,
                evidence_codes=(
                    "formal_trend_prefix",
                    "two_separated_centers",
                    "live_width_matched_departure_leg",
                    *_divergence_evidence_codes(divergence),
                    "temporary_trend_divergence",
                ),
                missing_conditions=("terminal_unit_locked",),
            )
        return None

    def _approaching_second_class(
        self,
        level,
        tail: ConstituentUnit,
    ) -> tuple[StrictPointEvidence, ...]:
        output = []
        for parent in self.first_class_points():
            if parent.structural_level != level.structural_level:
                continue
            matches = [
                index
                for index, unit in enumerate(level.units)
                if unit.unit_id == parent.anchor_unit_id
            ]
            if len(matches) != 1:
                raise ValueError("first-class anchor must occur once in level units")
            index = matches[0]
            if len(level.units) <= index + 2:
                continue
            signal = level.units[index]
            rebound = level.units[index + 1]
            pullback = level.units[index + 2]
            if (
                pullback.unit_id != tail.unit_id
                or pullback.locked
                or not rebound.locked
            ):
                continue
            if (
                signal.direction == rebound.direction
                or signal.direction != pullback.direction
                or signal.end_tick != rebound.start_tick
                or rebound.end_tick != pullback.start_tick
            ):
                continue
            held = (
                pullback.low_tick >= parent.anchor_tick
                if parent.side == "buy"
                else pullback.high_tick <= parent.anchor_tick
            )
            divergence = None
            if held:
                variant = StrictPointVariant.STRICT
            else:
                if self.strength is None:
                    continue
                try:
                    divergence = compare_divergence(
                        signal,
                        _locked_projection(pullback),
                        self.strength,
                        kind="consolidation",
                    )
                except ValueError as exc:
                    if any(
                        marker in str(exc)
                        for marker in (
                            "MACD",
                            "directional MACD bars",
                            "market interval",
                        )
                    ):
                        continue
                    raise
                if not divergence.is_divergent:
                    continue
                variant = StrictPointVariant.WEAK_DIVERGENCE
            if parent.side == "buy":
                point_type = "2buy"
                side = "buy"
                anchor_tick = pullback.low_tick
            else:
                point_type = "2sell"
                side = "sell"
                anchor_tick = pullback.high_tick
            invalidation_tick = (
                parent.anchor_tick
                if variant is StrictPointVariant.STRICT
                else anchor_tick
            )
            output.append(
                StrictPointEvidence(
                    point_id=_approaching_point_id(
                        price_basis_revision=parent.price_basis_revision,
                        point_type=point_type,
                        structural_level=parent.structural_level,
                        anchor_unit_id=pullback.unit_id,
                        center_id=parent.center_id,
                        parent_point_id=parent.point_id,
                    ),
                    point_type=point_type,
                    side=side,
                    status=StrictPointStatus.APPROACHING,
                    variant=variant,
                    structural_level=parent.structural_level,
                    source_kind=pullback.source_kind,
                    price_basis_revision=parent.price_basis_revision,
                    anchor_unit_id=pullback.unit_id,
                    anchor_at=pullback.market_end,
                    confirmed_at=None,
                    available_at=max(
                        parent.available_at,
                        pullback.available_at,
                        (
                            pullback.available_at
                            if divergence is None
                            else divergence.available_at
                        ),
                    ),
                    price_quantum=self.price_quantum,
                    anchor_tick=anchor_tick,
                    invalidation_tick=invalidation_tick,
                    center_id=parent.center_id,
                    center_zd_tick=parent.center_zd_tick,
                    center_zg_tick=parent.center_zg_tick,
                    center_ordinal=None,
                    divergence=divergence,
                    parent_point_id=parent.point_id,
                    evidence_codes=(
                        "confirmed_first_class_parent",
                        "complete_adjacent_rebound",
                        "live_first_pullback",
                    ),
                    missing_conditions=("terminal_unit_locked",),
                )
            )
        return tuple(output)

    def first_class_points(self) -> tuple[StrictPointEvidence, ...]:
        output: dict[tuple[int, str, str], StrictPointEvidence] = {}
        for level in self.structure.levels:
            for trend in level.completed_trends:
                if (
                    trend.state is not TrendState.COMPLETE
                    or trend.kind is not TrendKind.TREND
                    or len(trend.centers) < 2
                ):
                    continue
                divergence = trend.terminal_divergence
                if divergence is None:
                    continue
                signal = trend.terminal_unit
                if (
                    not signal.locked
                    or signal.direction != trend.direction
                    or signal.source_kind is SourceKind.STROKE_OBSERVATION
                ):
                    continue
                if not divergence.is_divergent:
                    continue
                point = self._first_class_point(
                    trend,
                    signal,
                    divergence,
                )
                key = (
                    point.structural_level,
                    point.point_type,
                    point.anchor_unit_id,
                )
                previous = output.get(key)
                if previous is not None and previous.point_id != point.point_id:
                    raise ValueError("first-class point identity is not stable")
                output.setdefault(key, point)
        return tuple(
            sorted(
                output.values(),
                key=lambda point: (
                    point.available_at,
                    point.structural_level,
                    point.point_type,
                    point.point_id,
                ),
            )
        )

    def second_class_points(
        self,
        first_points: tuple[StrictPointEvidence, ...] | None = None,
        *,
        lower_level_first_points: tuple[StrictPointEvidence, ...] = (),
    ) -> tuple[StrictPointEvidence, ...]:
        parents = (
            self.first_class_points() if first_points is None else tuple(first_points)
        )
        if len({point.point_id for point in parents}) != len(parents):
            raise ValueError("first-class parent ids must be unique")
        lower_points = tuple(lower_level_first_points)
        if len({point.point_id for point in lower_points}) != len(lower_points):
            raise ValueError("lower-level point ids must be unique")
        if any(
            point.status is not StrictPointStatus.CONFIRMED
            or point.point_type not in {"1buy", "1sell"}
            for point in lower_points
        ):
            raise ValueError("lower-level references must be confirmed first points")
        all_first_by_id = {point.point_id: point for point in parents}
        for point in lower_points:
            previous = all_first_by_id.setdefault(point.point_id, point)
            if previous != point:
                raise ValueError("first-class id maps to conflicting evidence")
        all_first = tuple(all_first_by_id.values())
        levels = {level.structural_level: level for level in self.structure.levels}
        output: dict[tuple[int, str, str], StrictPointEvidence] = {}
        for parent in parents:
            if (
                parent.status is not StrictPointStatus.CONFIRMED
                or parent.point_type not in {"1buy", "1sell"}
            ):
                raise ValueError("second-class parent must be a confirmed first point")
            level = levels.get(parent.structural_level)
            if level is None:
                raise ValueError("second-class parent level is missing")
            if parent.price_basis_revision != self.structure.price_basis_revision:
                raise ValueError("second-class parent crosses price basis")
            matches = [
                index
                for index, unit in enumerate(level.units)
                if unit.unit_id == parent.anchor_unit_id
            ]
            if len(matches) != 1:
                raise ValueError("first-class anchor must occur once in level units")
            anchor_index = matches[0]
            if len(level.units) < anchor_index + 3:
                continue
            signal = level.units[anchor_index]
            rebound = level.units[anchor_index + 1]
            pullback = level.units[anchor_index + 2]
            if not rebound.locked or not pullback.locked:
                continue
            if (
                signal.direction == rebound.direction
                or signal.direction != pullback.direction
                or signal.end_tick != rebound.start_tick
                or rebound.end_tick != pullback.start_tick
                or rebound.market_start < signal.market_end
                or pullback.market_start < rebound.market_end
            ):
                continue
            if (
                signal.source_kind is SourceKind.STROKE_OBSERVATION
                or rebound.source_kind is not signal.source_kind
                or pullback.source_kind is not signal.source_kind
                or signal.price_basis_revision != parent.price_basis_revision
                or rebound.price_basis_revision != parent.price_basis_revision
                or pullback.price_basis_revision != parent.price_basis_revision
            ):
                raise ValueError("second-class sequence source or price basis mismatch")

            if parent.side == "buy":
                held = pullback.low_tick >= parent.anchor_tick
            else:
                held = pullback.high_tick <= parent.anchor_tick
            divergence = None
            if held:
                variant = StrictPointVariant.STRICT
            else:
                if self.strength is None:
                    raise ValueError(
                        "weak second-class point requires a strength provider"
                    )
                try:
                    divergence = compare_divergence(
                        signal,
                        pullback,
                        self.strength,
                        kind="consolidation",
                    )
                except ValueError as exc:
                    unavailable_markers = (
                        "MACD",
                        "directional MACD bars",
                        "market interval must align",
                    )
                    if any(marker in str(exc) for marker in unavailable_markers):
                        continue
                    raise
                if not divergence.is_divergent:
                    continue
                variant = StrictPointVariant.WEAK_DIVERGENCE
            point = self._second_class_point(
                parent,
                pullback,
                variant=variant,
                divergence=divergence,
                related_point_ids=tuple(
                    sorted(
                        point.point_id
                        for point in all_first
                        if point.structural_level == parent.structural_level - 1
                        and point.side == parent.side
                        and point.price_basis_revision == parent.price_basis_revision
                        and pullback.market_start
                        <= point.anchor_at
                        <= pullback.market_end
                        and point.available_at <= pullback.available_at
                    )
                ),
            )
            output.setdefault(
                (point.structural_level, point.point_type, point.anchor_unit_id),
                point,
            )

        # L053's small-level-to-large-level reversal has no same-level first
        # point.  A confirmed lower-level first point closes its containing
        # higher-level unit; the immediate complete rebound and first pullback
        # then form the higher-level second point.  Ordinary same-level-parent
        # evidence wins if both paths identify the same anchor.
        for point in self._small_to_large_second_points(
            all_first,
            levels,
            self.third_class_points(),
        ):
            output.setdefault(
                (point.structural_level, point.point_type, point.anchor_unit_id),
                point,
            )
        return tuple(
            sorted(
                output.values(),
                key=lambda point: (
                    point.available_at,
                    point.structural_level,
                    point.point_type,
                    point.point_id,
                ),
            )
        )

    def _second_class_point(
        self,
        parent: StrictPointEvidence,
        pullback: ConstituentUnit,
        *,
        variant: StrictPointVariant,
        divergence,
        related_point_ids: tuple[str, ...],
    ) -> StrictPointEvidence:
        if pullback.confirmed_at is None:
            raise ValueError("second-class pullback requires confirmation")
        if parent.side == "buy":
            point_type = "2buy"
            side = "buy"
            anchor_tick = pullback.low_tick
        else:
            point_type = "2sell"
            side = "sell"
            anchor_tick = pullback.high_tick
        invalidation_tick = (
            parent.anchor_tick if variant is StrictPointVariant.STRICT else anchor_tick
        )
        confirmed_at = max(
            parent.confirmed_at,
            pullback.confirmed_at,
            pullback.confirmed_at if divergence is None else divergence.confirmed_at,
        )
        available_at = max(
            parent.available_at,
            pullback.available_at,
            pullback.available_at if divergence is None else divergence.available_at,
        )
        point_id = build_strict_point_id(
            price_basis_revision=parent.price_basis_revision,
            point_type=point_type,
            structural_level=parent.structural_level,
            anchor_unit_id=pullback.unit_id,
            center_id=parent.center_id,
            parent_point_id=parent.point_id,
        )
        return StrictPointEvidence(
            point_id=point_id,
            point_type=point_type,
            side=side,
            status=StrictPointStatus.CONFIRMED,
            variant=variant,
            structural_level=parent.structural_level,
            source_kind=pullback.source_kind,
            price_basis_revision=parent.price_basis_revision,
            anchor_unit_id=pullback.unit_id,
            anchor_at=pullback.market_end,
            confirmed_at=confirmed_at,
            available_at=available_at,
            price_quantum=self.price_quantum,
            anchor_tick=anchor_tick,
            invalidation_tick=invalidation_tick,
            center_id=parent.center_id,
            center_zd_tick=parent.center_zd_tick,
            center_zg_tick=parent.center_zg_tick,
            center_ordinal=None,
            divergence=divergence,
            parent_point_id=parent.point_id,
            evidence_codes=(
                "confirmed_first_class_parent",
                "complete_adjacent_rebound",
                "complete_first_pullback",
                (
                    "prior_extreme_held"
                    if variant is StrictPointVariant.STRICT
                    else "consolidation_divergence"
                ),
            ),
            related_point_ids=related_point_ids,
        )

    @staticmethod
    def _lower_descendant_ids(unit, lower_level) -> frozenset[str]:
        descendants = set(unit.child_ids)
        changed = True
        while changed:
            changed = False
            for trend in lower_level.trend_types:
                if trend.trend_id not in descendants:
                    continue
                before = len(descendants)
                for child in trend.constituent_units:
                    descendants.add(child.unit_id)
                    descendants.update(child.child_ids)
                changed = changed or len(descendants) != before
        return frozenset(descendants)

    @classmethod
    def _recursive_descendant_ids(cls, unit, levels) -> frozenset[str]:
        """Expand one recursive carrier to every auditable lower-level leaf.

        A higher-level unit may directly contain lower-level units, a locked
        trend id, or an associative same-level combination.  The graph is
        therefore expanded by stable identity instead of assuming that the
        small reversal belongs to the immediately adjacent level.
        """

        children_by_id: dict[str, tuple[str, ...]] = {}

        def register(identifier: str, children) -> None:
            values = tuple(children)
            previous = children_by_id.setdefault(identifier, values)
            if previous != values:
                raise ValueError("recursive carrier id maps to conflicting children")

        for level in levels.values():
            for candidate in level.units:
                register(candidate.unit_id, candidate.child_ids)
            for trend in level.trend_types:
                register(
                    trend.trend_id,
                    (item.unit_id for item in trend.constituent_units),
                )

        descendants: set[str] = set()
        pending = list(unit.child_ids)
        while pending:
            identifier = pending.pop()
            if identifier in descendants:
                continue
            descendants.add(identifier)
            pending.extend(children_by_id.get(identifier, ()))
        return frozenset(descendants)

    @classmethod
    def _contains_lower_anchor(cls, unit, levels, anchor_unit_id: str) -> bool:
        return anchor_unit_id in cls._recursive_descendant_ids(unit, levels)

    @classmethod
    def _last_lower_reverse_third(
        cls,
        parent,
        lower_level,
        signal,
        rebound,
        pullback,
        lower_third_points,
    ):
        signal_children = cls._lower_descendant_ids(signal, lower_level)
        rebound_children = cls._lower_descendant_ids(rebound, lower_level)
        pullback_children = cls._lower_descendant_ids(pullback, lower_level)
        movement_children = signal_children | rebound_children
        reversal_children = rebound_children | pullback_children
        if not rebound_children or not movement_children:
            return None

        candidates = tuple(
            center
            for center in lower_level.center_result.centers
            if signal.market_start
            <= center.body_start_market_time
            <= center.established_market_time
            <= rebound.market_end
            and center.entry_unit.unit_id in movement_children
            and all(
                item.unit_id in movement_children
                for item in (
                    *center.establishment_units,
                    *center.body_units,
                    *center.extension_units,
                )
            )
        )
        if not candidates:
            return None
        last_center = max(
            candidates,
            key=lambda center: (
                center.body_start_market_time,
                center.established_market_time,
                center.center_id,
            ),
        )
        if (
            not last_center.physically_completed
            or last_center.completion_leave_unit is None
            or last_center.completion_return_unit is None
            or last_center.available_at > pullback.available_at
            or last_center.completion_leave_unit.unit_id not in rebound_children
            or last_center.completion_return_unit.unit_id not in reversal_children
            or last_center.completion_return_unit.market_end > pullback.market_end
        ):
            return None
        expected_type = "3buy" if parent.side == "buy" else "3sell"
        matches = tuple(
            point
            for point in lower_third_points
            if point.structural_level == lower_level.structural_level
            and point.center_id == last_center.center_id
            and point.point_type == expected_type
            and point.status is StrictPointStatus.CONFIRMED
            and point.anchor_unit_id == last_center.completion_return_unit.unit_id
            and rebound.market_start <= point.anchor_at <= pullback.market_end
            and point.available_at <= pullback.available_at
        )
        if len(matches) > 1:
            raise ValueError("last lower-level center has duplicate third points")
        return None if not matches else matches[0]

    def _small_to_large_second_points(
        self,
        lower_first_points,
        levels,
        lower_third_points,
    ):
        output = []
        for parent in lower_first_points:
            for target_level_number in sorted(
                level_number
                for level_number in levels
                if level_number > parent.structural_level
            ):
                target = levels[target_level_number]
                lower_level = levels.get(target_level_number - 1)
                if lower_level is None:
                    continue
                matches = [
                    index
                    for index, unit in enumerate(target.units)
                    if self._contains_lower_anchor(
                        unit,
                        levels,
                        parent.anchor_unit_id,
                    )
                ]
                if len(matches) != 1:
                    continue
                anchor_index = matches[0]
                if len(target.units) < anchor_index + 3:
                    continue
                signal, rebound, pullback = target.units[
                    anchor_index : anchor_index + 3
                ]
                expected_signal_direction = "down" if parent.side == "buy" else "up"
                signal_extreme_matches = (
                    signal.low_tick == parent.anchor_tick
                    if parent.side == "buy"
                    else signal.high_tick == parent.anchor_tick
                )
                if (
                    not signal.locked
                    or not rebound.locked
                    or not pullback.locked
                    or signal.direction != expected_signal_direction
                    or signal.market_end != parent.anchor_at
                    or signal.end_tick != parent.anchor_tick
                    or not signal_extreme_matches
                    or signal.direction == rebound.direction
                    or signal.direction != pullback.direction
                    or signal.end_tick != rebound.start_tick
                    or rebound.end_tick != pullback.start_tick
                    or rebound.market_start < signal.market_end
                    or pullback.market_start < rebound.market_end
                ):
                    continue
                if (
                    signal.source_kind is SourceKind.STROKE_OBSERVATION
                    or rebound.source_kind is not signal.source_kind
                    or pullback.source_kind is not signal.source_kind
                    or signal.price_basis_revision != parent.price_basis_revision
                    or rebound.price_basis_revision != parent.price_basis_revision
                    or pullback.price_basis_revision != parent.price_basis_revision
                ):
                    raise ValueError("small-to-large second-class evidence mismatch")
                reverse_third = self._last_lower_reverse_third(
                    parent,
                    lower_level,
                    signal,
                    rebound,
                    pullback,
                    lower_third_points,
                )
                if reverse_third is None:
                    # L044: every promoted target uses the dynamic last center
                    # of its own direct sub-level.  A still smaller first point
                    # is only a possible turn, never sufficient proof alone.
                    continue
                held = (
                    pullback.low_tick >= parent.anchor_tick
                    if parent.side == "buy"
                    else pullback.high_tick <= parent.anchor_tick
                )
                divergence = None
                if held:
                    variant = StrictPointVariant.STRICT
                else:
                    if self.strength is None:
                        raise ValueError(
                            "weak small-to-large second-class point requires "
                            "a strength provider"
                        )
                    try:
                        divergence = compare_divergence(
                            signal,
                            pullback,
                            self.strength,
                            kind="consolidation",
                        )
                    except ValueError as exc:
                        if any(
                            marker in str(exc)
                            for marker in (
                                "MACD",
                                "directional MACD bars",
                                "market interval must align",
                            )
                        ):
                            continue
                        raise
                    if not divergence.is_divergent:
                        continue
                    variant = StrictPointVariant.WEAK_DIVERGENCE
                output.append(
                    self._small_to_large_second_class_point(
                        parent,
                        target_level_number,
                        signal,
                        rebound,
                        pullback,
                        variant=variant,
                        divergence=divergence,
                        reverse_third=reverse_third,
                    )
                )
        return tuple(output)

    def _small_to_large_second_class_point(
        self,
        parent: StrictPointEvidence,
        structural_level: int,
        signal: ConstituentUnit,
        rebound: ConstituentUnit,
        pullback: ConstituentUnit,
        *,
        variant: StrictPointVariant,
        divergence,
        reverse_third: StrictPointEvidence,
    ) -> StrictPointEvidence:
        if pullback.confirmed_at is None:
            raise ValueError("small-to-large pullback requires confirmation")
        if reverse_third.center_id is None:
            raise ValueError("small-to-large reverse third requires its center")
        point_type = "2buy" if parent.side == "buy" else "2sell"
        anchor_tick = pullback.low_tick if parent.side == "buy" else pullback.high_tick
        invalidation_tick = (
            parent.anchor_tick if variant is StrictPointVariant.STRICT else anchor_tick
        )
        confirmed_at = max(
            parent.confirmed_at,
            reverse_third.confirmed_at,
            pullback.confirmed_at,
            pullback.confirmed_at if divergence is None else divergence.confirmed_at,
        )
        available_at = max(
            parent.available_at,
            reverse_third.available_at,
            pullback.available_at,
            pullback.available_at if divergence is None else divergence.available_at,
        )
        point_id = build_strict_point_id(
            price_basis_revision=parent.price_basis_revision,
            point_type=point_type,
            structural_level=structural_level,
            anchor_unit_id=pullback.unit_id,
            center_id=None,
            parent_point_id=parent.point_id,
        )
        return StrictPointEvidence(
            point_id=point_id,
            point_type=point_type,
            side=parent.side,
            status=StrictPointStatus.CONFIRMED,
            variant=variant,
            structural_level=structural_level,
            source_kind=pullback.source_kind,
            price_basis_revision=parent.price_basis_revision,
            anchor_unit_id=pullback.unit_id,
            anchor_at=pullback.market_end,
            confirmed_at=confirmed_at,
            available_at=available_at,
            price_quantum=self.price_quantum,
            anchor_tick=anchor_tick,
            invalidation_tick=invalidation_tick,
            center_id=None,
            center_zd_tick=None,
            center_zg_tick=None,
            center_ordinal=None,
            divergence=divergence,
            parent_point_id=parent.point_id,
            evidence_codes=(
                "confirmed_lower_level_first_class_parent",
                "small_to_large_reversal",
                "last_lower_level_center_reverse_third_class",
                "complete_adjacent_rebound",
                "complete_first_pullback",
                (
                    "prior_extreme_held"
                    if variant is StrictPointVariant.STRICT
                    else "consolidation_divergence"
                ),
            ),
            related_point_ids=(parent.point_id, reverse_third.point_id),
            small_to_large_carrier_unit_ids=(
                signal.unit_id,
                rebound.unit_id,
                pullback.unit_id,
            ),
            small_to_large_last_center_id=reverse_third.center_id,
        )

    def _first_class_point(
        self,
        trend: TrendType,
        signal: ConstituentUnit,
        divergence,
    ) -> StrictPointEvidence:
        last_center = trend.centers[-1]
        if trend.direction == "down":
            point_type = "1buy"
            side = "buy"
            anchor_tick = signal.low_tick
        else:
            point_type = "1sell"
            side = "sell"
            anchor_tick = signal.high_tick
        if trend.confirmed_at is None or signal.confirmed_at is None:
            raise ValueError("completed trend requires confirmation")
        available_at = max(
            signal.available_at,
            last_center.available_at,
            trend.available_at,
            divergence.available_at,
        )
        point_id = build_strict_point_id(
            price_basis_revision=trend.price_basis_revision,
            point_type=point_type,
            structural_level=trend.structural_level,
            anchor_unit_id=signal.unit_id,
            center_id=last_center.center_id,
            parent_point_id=None,
        )
        return StrictPointEvidence(
            point_id=point_id,
            point_type=point_type,
            side=side,
            status=StrictPointStatus.CONFIRMED,
            variant=StrictPointVariant.STANDARD,
            structural_level=trend.structural_level,
            source_kind=signal.source_kind,
            price_basis_revision=trend.price_basis_revision,
            anchor_unit_id=signal.unit_id,
            anchor_at=signal.market_end,
            confirmed_at=max(
                trend.confirmed_at,
                signal.confirmed_at,
                divergence.confirmed_at,
            ),
            available_at=available_at,
            price_quantum=self.price_quantum,
            anchor_tick=anchor_tick,
            invalidation_tick=anchor_tick,
            center_id=last_center.center_id,
            center_zd_tick=last_center.zd_tick,
            center_zg_tick=last_center.zg_tick,
            center_ordinal=None,
            divergence=divergence,
            parent_point_id=None,
            evidence_codes=(
                "formal_trend",
                "two_separated_centers",
                "width_matched_entry_departure_legs",
                "confirmed_same_level_boundary",
                *_divergence_evidence_codes(divergence),
                "trend_divergence",
            ),
        )

    def _third_class_point(
        self,
        center: TrendCenter,
        *,
        direction: str,
        ordinal: int,
    ) -> StrictPointEvidence | None:
        leave = center.completion_leave_unit
        ret = center.completion_return_unit
        if (
            leave is None
            or ret is None
            or not leave.locked
            or not ret.locked
            or center.completed_at is None
        ):
            return None

        if direction == "up":
            if (
                leave.direction != "up"
                or ret.direction != "down"
                or ret.low_tick < center.zg_tick
            ):
                return None
            point_type = "3buy"
            side = "buy"
            anchor_tick = ret.low_tick
            invalidation_tick = center.zg_tick
            boundary_tick = center.zg_tick
        else:
            if (
                leave.direction != "down"
                or ret.direction != "up"
                or ret.high_tick > center.zd_tick
            ):
                return None
            point_type = "3sell"
            side = "sell"
            anchor_tick = ret.high_tick
            invalidation_tick = center.zd_tick
            boundary_tick = center.zd_tick

        variant = (
            StrictPointVariant.BOUNDARY_TOUCH
            if anchor_tick == boundary_tick
            else StrictPointVariant.STANDARD
        )
        available_at = max(center.available_at, ret.available_at)
        point_id = build_strict_point_id(
            price_basis_revision=center.price_basis_revision,
            point_type=point_type,
            structural_level=center.structural_level,
            anchor_unit_id=ret.unit_id,
            center_id=center.center_id,
            parent_point_id=None,
        )
        return StrictPointEvidence(
            point_id=point_id,
            point_type=point_type,
            side=side,
            status=StrictPointStatus.CONFIRMED,
            variant=variant,
            structural_level=center.structural_level,
            source_kind=center.source_kind,
            price_basis_revision=center.price_basis_revision,
            anchor_unit_id=ret.unit_id,
            anchor_at=ret.market_end,
            confirmed_at=center.completed_at,
            available_at=available_at,
            price_quantum=self.price_quantum,
            anchor_tick=anchor_tick,
            invalidation_tick=invalidation_tick,
            center_id=center.center_id,
            center_zd_tick=center.zd_tick,
            center_zg_tick=center.zg_tick,
            center_ordinal=ordinal,
            divergence=None,
            parent_point_id=None,
            evidence_codes=(
                "formal_center",
                "complete_leave",
                "complete_first_return",
                "core_boundary_held",
            ),
        )
