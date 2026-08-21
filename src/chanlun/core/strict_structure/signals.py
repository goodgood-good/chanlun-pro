from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from chanlun.core.strict_structure.divergence import (
    compare_center_consolidation_divergence,
)
from chanlun.core.strict_structure.models import (
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
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.point_rules import (
    approaching_third_class_point_ledger,
    build_approaching_point_id,
    center_ordinals,
    classify_third_class_geometry,
)
from chanlun.core.strict_structure.strength import (
    FormalDivergenceUnavailable,
    MacdStrengthUnavailable,
    compare_divergence,
    compare_terminal_trend_divergence,
)


def _locked_projection(unit: ConstituentUnit) -> ConstituentUnit:
    if unit.locked:
        return unit
    return replace(
        unit,
        locked=True,
        confirmed_at=unit.available_at,
        forming=False,
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


class StrictSignalEngine:
    def __init__(
        self,
        *,
        structure: StrictStructureResult,
        price_quantum: Decimal,
        strength=None,
        projection_cache=None,
    ) -> None:
        if not isinstance(structure, StrictStructureResult):
            raise TypeError("structure must be a StrictStructureResult")
        if not isinstance(price_quantum, Decimal) or price_quantum <= 0:
            raise ValueError("price_quantum must be a positive Decimal")
        self.structure = structure
        self.price_quantum = price_quantum
        self.strength = strength
        self.projection_cache = projection_cache

    def confirmed_points(
        self,
        *,
        structure: StrictStructureResult | None = None,
    ) -> tuple[StrictPointEvidence, ...]:
        """通过唯一标准流水线返回全部已确认的一、二、三类买卖点。

        调用方不得自行拼接三类买卖点。二类点识别必须接收本次调用生成的精确
        一类点账本，使图表、回放和选股不会因排序或去重细节不同而发生漂移。
        """

        calculation_structure = self.structure if structure is None else structure
        if not isinstance(calculation_structure, StrictStructureResult):
            raise TypeError("structure must be a StrictStructureResult")
        first = self.first_class_points(structure=calculation_structure)
        candidates = (
            *first,
            *self.second_class_points(first, structure=calculation_structure),
            *self.third_class_points(structure=calculation_structure),
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

    def third_class_points(
        self,
        *,
        structure: StrictStructureResult | None = None,
    ) -> tuple[StrictPointEvidence, ...]:
        calculation_structure = self.structure if structure is None else structure
        if not isinstance(calculation_structure, StrictStructureResult):
            raise TypeError("structure must be a StrictStructureResult")
        output = []
        for level in calculation_structure.levels:
            centers = tuple(level.center_result.centers)
            ordinals = center_ordinals(
                centers,
                level.decomposition_boundaries,
            )
            for center in centers:
                if center.source_kind is SourceKind.STROKE_OBSERVATION:
                    continue
                if (
                    center.price_basis_revision
                    != calculation_structure.price_basis_revision
                ):
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
        confirmed_points = self.confirmed_points()
        # Level zero is the physical trading/segment-difference lane. Rebuild
        # it through the latest causally formed segment under an explicit
        # projection. The same formal rules then produce all six classes,
        # eliminating the old asymmetry where only third-class points had a
        # completed-geometry ledger.
        output = list(
            self._projected_level_zero_points(
                confirmed_points=confirmed_points,
            )
        )
        level_zero_units = {
            unit.unit_id: unit
            for level in self.structure.levels
            if level.structural_level == 0
            for unit in level.units
        }
        # The forming tail keeps the lightweight live rules.  Only completed
        # geometry needs the formal projection; recomputing a projected trend
        # on every incoming K-line would make realtime monitoring unusable.
        output.extend(
            point
            for point in approaching_third_class_point_ledger(
                self.structure,
                price_quantum=self.price_quantum,
            )
            if point.structural_level > 0
            or (
                point.anchor_unit_id in level_zero_units
                and level_zero_units[point.anchor_unit_id].forming
            )
        )
        confirmed_first_points = tuple(
            point for point in confirmed_points if point.point_type in {"1buy", "1sell"}
        )
        levels = {level.structural_level: level for level in self.structure.levels}
        active_by_level: dict[int, tuple[ConstituentUnit, ...]] = {}
        approaching_first_points: list[StrictPointEvidence] = [
            point for point in output if point.point_type in {"1buy", "1sell"}
        ]

        # Recursive levels retain the active-suffix preview.  Level zero has
        # already rebuilt its latest completed segment above and only needs
        # the lightweight rule for the sole forming tail here.
        for level in self.structure.levels:
            active = level.units[level.center_result.locked_unit_count :]
            if not active:
                continue
            if any(unit.locked for unit in active):
                raise ValueError("active structural suffix must be unlocked")
            active_by_level[level.structural_level] = tuple(active)
            if level.structural_level == 0:
                first_carriers = active[-1:] if active[-1].forming else ()
            else:
                first_carriers = active
            for unit in first_carriers:
                if unit.available_at > as_of:
                    raise ValueError("active structural unit is available after as_of")
                # A solid preview without a causal formation witness is still
                # display-only.  The actual forming tail remains eligible for
                # an explicit non-actionable approaching point.
                if not unit.forming and unit.formed_at is None:
                    continue
                first = self._approaching_first_class(level, unit)
                if first is not None:
                    approaching_first_points.append(first)

        output.extend(
            point
            for point in approaching_first_points
            if point not in output
        )
        first_points = (*confirmed_first_points, *approaching_first_points)
        for level in self.structure.levels:
            active = active_by_level.get(level.structural_level)
            if not active:
                continue
            active_unit_ids = frozenset(unit.unit_id for unit in active)
            second_candidates = self._approaching_second_class(
                level,
                active_unit_ids,
                first_points,
                levels,
            )
            small_to_large_candidates = (
                self._approaching_small_to_large_second_class(
                    level,
                    active_unit_ids,
                    first_points,
                    levels,
                )
            )
            if level.structural_level == 0:
                forming_ids = {
                    unit.unit_id for unit in active if unit.forming
                }
                second_candidates = tuple(
                    point
                    for point in second_candidates
                    if point.anchor_unit_id in forming_ids
                )
                small_to_large_candidates = tuple(
                    point
                    for point in small_to_large_candidates
                    if point.anchor_unit_id in forming_ids
                )
            output.extend(second_candidates)
            output.extend(small_to_large_candidates)

        unique = {}
        for point in output:
            if point.available_at > as_of:
                raise ValueError("approaching point is available after as_of")
            previous = unique.setdefault(point.point_id, point)
            if previous != point:
                raise ValueError("盘中买卖点身份映射到冲突证据")
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

    def _projected_level_zero_points(
        self,
        *,
        confirmed_points: tuple[StrictPointEvidence, ...],
    ) -> tuple[StrictPointEvidence, ...]:
        """Run the formal six-point rules on one causal level-zero projection.

        The projection changes evidence state only in a temporary structure;
        the immutable audit structure is never rewritten.  A formed segment is
        locked at its preserved ``formed_at``. The sole forming tail is not
        part of this formal projection and remains a non-actionable preview.
        """

        if not self.structure.levels:
            return ()
        level = self.structure.levels[0]
        if level.structural_level != 0:
            raise ValueError("strict structure level zero is missing")
        forming = tuple(unit for unit in level.units if unit.forming)
        if len(forming) > 1 or (forming and forming[0] is not level.units[-1]):
            raise ValueError("only the terminal level-zero unit may be forming")
        target = next(
            (
                unit
                for unit in reversed(level.units)
                if not unit.forming
                and (unit.locked or unit.formed_at is not None)
            ),
            None,
        )
        if target is None or target.locked:
            return ()

        target_index = next(
            index for index, unit in enumerate(level.units) if unit is target
        )
        cache_key = (
            "strict-level-zero-projected-points-v2",
            self.structure.price_basis_revision,
            str(self.price_quantum),
            tuple(point.point_id for point in confirmed_points),
            tuple(
                (
                    unit.unit_id,
                    unit.locked,
                    unit.forming,
                    unit.formed_at,
                    unit.confirmed_at,
                    unit.available_at,
                )
                for unit in level.units[: target_index + 1]
            ),
        )
        if self.projection_cache is not None:
            cached = self.projection_cache.pop(cache_key, None)
            if cached is not None:
                self.projection_cache[cache_key] = cached
                return cached
        projected_units = []
        causal_floor = None
        for unit in level.units[: target_index + 1]:
            if unit.locked:
                projected_units.append(unit)
                causal_floor = (
                    unit.available_at
                    if causal_floor is None
                    else max(causal_floor, unit.available_at)
                )
                continue
            if unit.forming:
                return ()
            else:
                if unit.formed_at is None:
                    # A solid chart preview without a causal witness must not
                    # become a trade fact merely because a rebuild ran now.
                    return ()
                projected_at = unit.formed_at
            if causal_floor is not None:
                projected_at = max(projected_at, causal_floor)
            causal_floor = projected_at
            projected_units.append(
                replace(
                    unit,
                    locked=True,
                    forming=False,
                    confirmed_at=projected_at,
                    available_at=projected_at,
                    formed_at=projected_at,
                )
            )
        if not projected_units:
            return ()

        projected_structure = StrictRecursiveEngine(
            max_levels=1,
            center_prefix_cache=self.projection_cache,
        ).calculate(
            tuple(projected_units),
            price_basis_revision=self.structure.price_basis_revision,
            strength=self.strength,
        )
        if not projected_structure.levels:
            return ()
        projected_points = self.confirmed_points(
            structure=projected_structure,
        )
        targets = tuple(
            point
            for point in projected_points
            if point.structural_level == 0
            and point.anchor_unit_id == target.unit_id
        )
        if not targets:
            return ()
        result = self._convert_projected_point_graph(
            projected_points,
            targets,
            confirmed_points=confirmed_points,
            missing_condition="terminal_unit_audit_lock",
        )
        if self.projection_cache is not None:
            self.projection_cache[cache_key] = result
            while len(self.projection_cache) > 512:
                self.projection_cache.popitem(last=False)
        return result

    def _convert_projected_point_graph(
        self,
        projected_points: tuple[StrictPointEvidence, ...],
        targets: tuple[StrictPointEvidence, ...],
        *,
        confirmed_points: tuple[StrictPointEvidence, ...],
        missing_condition: str,
    ) -> tuple[StrictPointEvidence, ...]:
        """Convert target points and unresolved parent evidence to previews."""

        projected_by_id = {point.point_id: point for point in projected_points}
        confirmed_by_id = {point.point_id: point for point in confirmed_points}
        target_ids = {point.point_id for point in targets}
        converted: dict[str, StrictPointEvidence] = {}

        def convert(point_id: str) -> StrictPointEvidence:
            confirmed = confirmed_by_id.get(point_id)
            if confirmed is not None:
                return confirmed
            existing = converted.get(point_id)
            if existing is not None:
                return existing
            raw = projected_by_id.get(point_id)
            if raw is None:
                raise ValueError("projected point dependency is missing")
            parent = (
                None
                if raw.parent_point_id is None
                else convert(raw.parent_point_id)
            )
            related = tuple(convert(point_id) for point_id in raw.related_point_ids)
            parent_id = None if parent is None else parent.point_id
            approaching_id = build_approaching_point_id(
                price_basis_revision=raw.price_basis_revision,
                point_type=raw.point_type,
                structural_level=raw.structural_level,
                anchor_unit_id=raw.anchor_unit_id,
                center_id=raw.center_id,
                parent_point_id=parent_id,
            )
            value = replace(
                raw,
                point_id=approaching_id,
                status=StrictPointStatus.APPROACHING,
                confirmed_at=None,
                parent_point_id=parent_id,
                related_point_ids=tuple(point.point_id for point in related),
                evidence_codes=tuple(
                    dict.fromkeys(
                        (*raw.evidence_codes, "projected_geometric_structure")
                    )
                ),
                missing_conditions=(
                    missing_condition
                    if raw.point_id in target_ids
                    else "terminal_unit_audit_lock",
                ),
            )
            converted[point_id] = value
            return value

        for target in targets:
            convert(target.point_id)
        return tuple(
            sorted(
                converted.values(),
                key=lambda point: (
                    point.available_at,
                    point.structural_level,
                    point.point_type,
                    point.point_id,
                ),
            )
        )

    def _approaching_first_class(
        self,
        level,
        tail: ConstituentUnit,
    ) -> StrictPointEvidence | None:
        if self.strength is None:
            return None
        for trend in reversed(level.trend_types):
            if (
                trend.terminal_divergence is not None
                or tail.source_kind is SourceKind.STROKE_OBSERVATION
            ):
                continue
            if (
                trend.kind is TrendKind.TREND
                and len(trend.centers) < 2
            ) or (
                trend.kind is TrendKind.CONSOLIDATION
                and len(trend.centers) != 1
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
                if trend.kind is TrendKind.TREND:
                    compared = compare_terminal_trend_divergence(
                        (*trend.centers[:-1], projected_center),
                        tuple(projected_units),
                        self.strength,
                        trend_start_unit_id=trend.constituent_units[0].unit_id,
                    )
                else:
                    divergence = (
                        compare_center_consolidation_divergence(
                            projected_center,
                            tuple(projected_units),
                            self.strength,
                            movement_start_unit_id=(
                                trend.constituent_units[0].unit_id
                            ),
                        )
                    )
                    compared = (
                        None
                        if (
                            divergence is None
                            or divergence.signal_unit_id
                            != projected_tail.unit_id
                        )
                        else (divergence, projected_tail)
                    )
            except (FormalDivergenceUnavailable, MacdStrengthUnavailable):
                continue
            if compared is None:
                continue
            divergence, signal = compared
            if not divergence.is_divergent:
                continue
            # 已完成中枢仍可能保留一个锚在历史离开段上的背驰比较；它不是当前
            # 未锁定尾段的“接近一买/一卖”。这里只接受由当前尾段自身产生且时间、
            # 极值完全一致的临时背驰，避免把旧锚点套到新尾段上。
            expected_anchor_tick = (
                tail.low_tick if divergence.direction == "down" else tail.high_tick
            )
            if (
                signal.unit_id != tail.unit_id
                or divergence.signal_unit_id != tail.unit_id
                or divergence.anchor_at != tail.market_end
                or divergence.anchor_tick != expected_anchor_tick
            ):
                continue
            if divergence.direction == "down":
                point_type = "1buy"
                side = "buy"
                anchor_tick = divergence.anchor_tick
            else:
                point_type = "1sell"
                side = "sell"
                anchor_tick = divergence.anchor_tick
            return StrictPointEvidence(
                point_id=build_approaching_point_id(
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
                    (
                        "formal_trend_prefix"
                        if trend.kind is TrendKind.TREND
                        else "formal_consolidation_prefix"
                    ),
                    (
                        "two_separated_centers"
                        if trend.kind is TrendKind.TREND
                        else "single_center_consolidation"
                    ),
                    "live_width_matched_departure_leg",
                    *_divergence_evidence_codes(divergence),
                    f"temporary_{divergence.kind}_divergence",
                ),
                missing_conditions=("terminal_unit_locked",),
            )
        return None

    def _approaching_second_class(
        self,
        level,
        active_unit_ids: frozenset[str],
        first_points: tuple[StrictPointEvidence, ...],
        levels,
    ) -> tuple[StrictPointEvidence, ...]:
        output = []
        for parent in first_points:
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
                pullback.unit_id not in active_unit_ids
                or not self._second_class_carrier_is_valid(
                    signal,
                    rebound,
                    pullback,
                    pullback_locked=False,
                    signal_completed=self._recursive_observation_is_complete(
                        signal,
                        levels,
                    ),
                    rebound_completed=self._recursive_observation_is_complete(
                        rebound,
                        levels,
                    ),
                )
            ):
                continue
            self._validate_second_class_source(parent, signal, rebound, pullback)
            classified = self._classify_second_class_variant(
                parent,
                signal,
                pullback,
                live=True,
            )
            if classified is None:
                continue
            variant, divergence = classified
            output.append(
                self._approaching_second_class_point(
                    parent,
                    parent.structural_level,
                    signal,
                    rebound,
                    pullback,
                    variant=variant,
                    divergence=divergence,
                    small_to_large=False,
                )
            )
        return tuple(output)

    def _approaching_small_to_large_second_class(
        self,
        level,
        active_unit_ids: frozenset[str],
        first_points: tuple[StrictPointEvidence, ...],
        levels,
    ) -> tuple[StrictPointEvidence, ...]:
        """返回低级别一类点触发的高级别盘中二类点候选。"""

        output = []
        for parent in first_points:
            if parent.structural_level >= level.structural_level:
                continue
            matches = [
                index
                for index, unit in enumerate(level.units)
                if self._contains_lower_anchor(unit, levels, parent.anchor_unit_id)
            ]
            if len(matches) != 1 or len(level.units) < matches[0] + 3:
                continue
            signal, rebound, pullback = level.units[matches[0] : matches[0] + 3]
            expected_direction = "down" if parent.side == "buy" else "up"
            extreme_matches = (
                signal.low_tick == parent.anchor_tick
                if parent.side == "buy"
                else signal.high_tick == parent.anchor_tick
            )
            if (
                pullback.unit_id not in active_unit_ids
                or not self._second_class_carrier_is_valid(
                    signal,
                    rebound,
                    pullback,
                    pullback_locked=False,
                    signal_completed=self._recursive_observation_is_complete(
                        signal,
                        levels,
                    ),
                    rebound_completed=self._recursive_observation_is_complete(
                        rebound,
                        levels,
                    ),
                )
                or signal.direction != expected_direction
                or signal.market_end != parent.anchor_at
                or signal.end_tick != parent.anchor_tick
                or not extreme_matches
            ):
                continue
            self._validate_second_class_source(parent, signal, rebound, pullback)
            classified = self._classify_second_class_variant(
                parent,
                signal,
                pullback,
                live=True,
            )
            if classified is None:
                continue
            variant, divergence = classified
            output.append(
                self._approaching_second_class_point(
                    parent,
                    level.structural_level,
                    signal,
                    rebound,
                    pullback,
                    variant=variant,
                    divergence=divergence,
                    small_to_large=True,
                )
            )
        return tuple(output)

    @staticmethod
    def _second_class_carrier_is_valid(
        signal: ConstituentUnit,
        rebound: ConstituentUnit,
        pullback: ConstituentUnit,
        *,
        pullback_locked: bool,
        signal_completed: bool | None = None,
        rebound_completed: bool | None = None,
    ) -> bool:
        """用同一套三段几何校验正式与盘中二类点。"""

        signal_ready = signal.locked if signal_completed is None else signal_completed
        rebound_ready = (
            rebound.locked if rebound_completed is None else rebound_completed
        )
        return (
            signal_ready
            and rebound_ready
            and pullback.locked is pullback_locked
            and signal.direction != rebound.direction
            and signal.direction == pullback.direction
            and signal.end_tick == rebound.start_tick
            and rebound.end_tick == pullback.start_tick
            and rebound.market_start >= signal.market_end
            and pullback.market_start >= rebound.market_end
        )

    @staticmethod
    def _validate_second_class_source(
        parent: StrictPointEvidence,
        signal: ConstituentUnit,
        rebound: ConstituentUnit,
        pullback: ConstituentUnit,
    ) -> None:
        """确保二类点三段载体同源、同价格口径且可以交易。"""

        if (
            signal.source_kind is SourceKind.STROKE_OBSERVATION
            or rebound.source_kind is not signal.source_kind
            or pullback.source_kind is not signal.source_kind
            or signal.price_basis_revision != parent.price_basis_revision
            or rebound.price_basis_revision != parent.price_basis_revision
            or pullback.price_basis_revision != parent.price_basis_revision
        ):
            raise ValueError("second-class sequence source or price basis mismatch")

    def _classify_second_class_variant(
        self,
        parent: StrictPointEvidence,
        signal: ConstituentUnit,
        pullback: ConstituentUnit,
        *,
        live: bool,
    ):
        """按统一的守前低/前高或盘整背驰规则判定二类点。"""

        held = (
            pullback.low_tick >= parent.anchor_tick
            if parent.side == "buy"
            else pullback.high_tick <= parent.anchor_tick
        )
        if held:
            return StrictPointVariant.STRICT, None
        if self.strength is None:
            return None
        comparison_pullback = _locked_projection(pullback) if live else pullback
        try:
            divergence = compare_divergence(
                signal,
                comparison_pullback,
                self.strength,
                kind="consolidation",
            )
        except (FormalDivergenceUnavailable, MacdStrengthUnavailable):
            return None
        if not divergence.is_divergent:
            return None
        return StrictPointVariant.WEAK_DIVERGENCE, divergence

    @staticmethod
    def _recursive_observation_is_complete(unit: ConstituentUnit, levels) -> bool:
        """返回未锁定递归单元是否来自低一级已完成走势快照。"""

        if unit.locked:
            return True
        if not unit.forming and unit.formed_at is not None:
            return True
        source_level = levels.get(unit.structural_level - 1)
        if source_level is None:
            return False
        matches = tuple(
            trend for trend in source_level.trend_types if trend.trend_id == unit.unit_id
        )
        return len(matches) == 1 and matches[0].complete

    def _approaching_second_class_point(
        self,
        parent: StrictPointEvidence,
        structural_level: int,
        signal: ConstituentUnit,
        rebound: ConstituentUnit,
        pullback: ConstituentUnit,
        *,
        variant: StrictPointVariant,
        divergence,
        small_to_large: bool,
    ) -> StrictPointEvidence:
        """从已通过统一判定的三段载体构造不可交易的盘中二类点。"""

        point_type = "2buy" if parent.side == "buy" else "2sell"
        anchor_tick = pullback.low_tick if parent.side == "buy" else pullback.high_tick
        center_id = None if small_to_large else parent.center_id
        parent_is_confirmed = parent.status is StrictPointStatus.CONFIRMED
        evidence_codes = (
            (
                "confirmed_lower_level_first_class_parent"
                if small_to_large
                and parent_is_confirmed
                else "formed_lower_level_first_class_parent"
                if small_to_large
                else "confirmed_first_class_parent"
                if parent_is_confirmed
                else "formed_first_class_parent"
            ),
            *(("small_to_large_reversal",) if small_to_large else ()),
            "complete_adjacent_rebound",
            "live_first_pullback",
            (
                "prior_extreme_currently_held"
                if variant is StrictPointVariant.STRICT
                else "temporary_consolidation_divergence"
            ),
        )
        return StrictPointEvidence(
            point_id=build_approaching_point_id(
                price_basis_revision=parent.price_basis_revision,
                point_type=point_type,
                structural_level=structural_level,
                anchor_unit_id=pullback.unit_id,
                center_id=center_id,
                parent_point_id=parent.point_id,
            ),
            point_type=point_type,
            side=parent.side,
            status=StrictPointStatus.APPROACHING,
            variant=variant,
            structural_level=structural_level,
            source_kind=pullback.source_kind,
            price_basis_revision=parent.price_basis_revision,
            anchor_unit_id=pullback.unit_id,
            anchor_at=pullback.market_end,
            confirmed_at=None,
            available_at=max(
                parent.available_at,
                pullback.available_at,
                pullback.available_at if divergence is None else divergence.available_at,
            ),
            price_quantum=self.price_quantum,
            anchor_tick=anchor_tick,
            invalidation_tick=(
                parent.anchor_tick
                if variant is StrictPointVariant.STRICT
                else anchor_tick
            ),
            center_id=center_id,
            center_zd_tick=None if small_to_large else parent.center_zd_tick,
            center_zg_tick=None if small_to_large else parent.center_zg_tick,
            center_ordinal=None,
            divergence=divergence,
            parent_point_id=parent.point_id,
            evidence_codes=evidence_codes,
            missing_conditions=("terminal_unit_locked",),
            related_point_ids=(parent.point_id,) if small_to_large else (),
            small_to_large_carrier_unit_ids=(
                (signal.unit_id, rebound.unit_id, pullback.unit_id)
                if small_to_large
                else ()
            ),
        )

    def first_class_points(
        self,
        *,
        structure: StrictStructureResult | None = None,
    ) -> tuple[StrictPointEvidence, ...]:
        """返回所有由正式趋势背驰或盘整背驰确认的一类买卖点。"""

        calculation_structure = self.structure if structure is None else structure
        if not isinstance(calculation_structure, StrictStructureResult):
            raise TypeError("structure must be a StrictStructureResult")
        output: dict[tuple[int, str, str], StrictPointEvidence] = {}
        for level in calculation_structure.levels:
            for trend in level.completed_trends:
                if (
                    trend.state is not TrendState.COMPLETE
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
        structure: StrictStructureResult | None = None,
    ) -> tuple[StrictPointEvidence, ...]:
        calculation_structure = self.structure if structure is None else structure
        if not isinstance(calculation_structure, StrictStructureResult):
            raise TypeError("structure must be a StrictStructureResult")
        parents = (
            self.first_class_points(structure=calculation_structure)
            if first_points is None
            else tuple(first_points)
        )
        if len({point.point_id for point in parents}) != len(parents):
            raise ValueError("first-class parent ids must be unique")
        levels = {
            level.structural_level: level
            for level in calculation_structure.levels
        }
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
            if (
                parent.price_basis_revision
                != calculation_structure.price_basis_revision
            ):
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
            if not self._second_class_carrier_is_valid(
                signal,
                rebound,
                pullback,
                pullback_locked=True,
            ):
                continue
            self._validate_second_class_source(parent, signal, rebound, pullback)
            classified = self._classify_second_class_variant(
                parent,
                signal,
                pullback,
                live=False,
            )
            if classified is None:
                continue
            variant, divergence = classified
            point = self._second_class_point(
                parent,
                pullback,
                variant=variant,
                divergence=divergence,
                related_point_ids=tuple(
                    sorted(
                        point.point_id
                        for point in parents
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

        # 小转大没有同级一类点。下一级一类点确认其所在的高一级离开单元后，
        # 紧邻的完整反弹与第一次回抽就按普通二类点的同一套规则确认高一级
        # 二类点；不再额外要求下一级先出现三类点。若两条路径落在同一锚点，
        # 优先保留拥有同级一类点父证据的普通二类点。
        for point in self._small_to_large_second_points(
            parents,
            levels,
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

    @classmethod
    def _recursive_descendant_ids(cls, unit, levels) -> frozenset[str]:
        """把递归载体展开为所有可审计的低级别叶子单元。"""

        children_by_id: dict[str, tuple[str, ...]] = {}

        def register(identifier: str, children) -> None:
            values = tuple(children)
            previous = children_by_id.setdefault(identifier, values)
            if previous != values:
                raise ValueError("recursive carrier id maps to conflicting children")

        for level in levels.values():
            for candidate in level.units:
                register(candidate.unit_id, candidate.child_ids)
            for trend in (*level.trend_types, *level.completed_trends):
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

    def _small_to_large_second_points(
        self,
        lower_first_points,
        levels,
    ):
        output = []
        for parent in lower_first_points:
            for target_level_number in sorted(
                level_number
                for level_number in levels
                if level_number > parent.structural_level
            ):
                target = levels[target_level_number]
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
                    not self._second_class_carrier_is_valid(
                        signal,
                        rebound,
                        pullback,
                        pullback_locked=True,
                    )
                    or signal.direction != expected_signal_direction
                    or signal.market_end != parent.anchor_at
                    or signal.end_tick != parent.anchor_tick
                    or not signal_extreme_matches
                ):
                    continue
                self._validate_second_class_source(
                    parent,
                    signal,
                    rebound,
                    pullback,
                )
                classified = self._classify_second_class_variant(
                    parent,
                    signal,
                    pullback,
                    live=False,
                )
                if classified is None:
                    continue
                variant, divergence = classified
                output.append(
                    self._small_to_large_second_class_point(
                        parent,
                        target_level_number,
                        signal,
                        rebound,
                        pullback,
                        variant=variant,
                        divergence=divergence,
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
    ) -> StrictPointEvidence:
        if pullback.confirmed_at is None:
            raise ValueError("small-to-large pullback requires confirmation")
        point_type = "2buy" if parent.side == "buy" else "2sell"
        anchor_tick = pullback.low_tick if parent.side == "buy" else pullback.high_tick
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
                "complete_adjacent_rebound",
                "complete_first_pullback",
                (
                    "prior_extreme_held"
                    if variant is StrictPointVariant.STRICT
                    else "consolidation_divergence"
                ),
            ),
            related_point_ids=(parent.point_id,),
            small_to_large_carrier_unit_ids=(
                signal.unit_id,
                rebound.unit_id,
                pullback.unit_id,
            ),
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
                (
                    "formal_trend"
                    if trend.kind is TrendKind.TREND
                    else "formal_consolidation_movement"
                ),
                (
                    "two_separated_centers"
                    if trend.kind is TrendKind.TREND
                    else "single_center_consolidation"
                ),
                "width_matched_entry_departure_legs",
                "confirmed_same_level_boundary",
                *_divergence_evidence_codes(divergence),
                f"{divergence.kind}_divergence",
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
        classified = classify_third_class_geometry(
            zd_tick=center.zd_tick,
            zg_tick=center.zg_tick,
            leave=leave,
            return_unit=ret,
        )
        if classified is None or classified[0] != direction:
            return None
        (
            _direction,
            point_type,
            side,
            anchor_tick,
            invalidation_tick,
            variant,
        ) = classified
        available_at = center.completion_available_at
        if available_at is None:
            raise ValueError("completed center requires immutable completion time")
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
