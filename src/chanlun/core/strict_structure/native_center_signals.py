"""User-declared 1/2/3 buy/sell signal policy for one physical K-line period.

The policy does not claim the retired recursive movement partition or the
original-text proof of a formal Chan point. Its own signals are confirmed only
when their source segments and local conditions are confirmed.
"""

from __future__ import annotations

from datetime import datetime

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.center_geometry import return_outside_core
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import CenterRelation, ConstituentUnit, SourceKind, TrendCenter
from chanlun.core.strict_structure.strength import (
    FormalDivergenceUnavailable,
    MacdStrengthProvider,
    MacdStrengthUnavailable,
    compare_terminal_trend_divergence,
)

NATIVE_SIGNAL_RULE_BASIS = "user_declared_native_center_macd_v4"


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _alert(
    kind: str, unit: ConstituentUnit, center: TrendCenter, *,
    related_unit_ids: tuple[str, ...], evidence_codes: tuple[str, ...],
    parent_point_id: str | None = None, divergence: dict | None = None,
    anchor_at: datetime | None = None, anchor_tick: int | None = None,
    price_anchor_unit_id: str | None = None,
    source_known_at: datetime | None = None,
    center_ordinal: int | None = None,
) -> dict[str, object]:
    side = "buy" if kind.endswith("buy") else "sell"
    if anchor_tick is None:
        anchor_tick = unit.low_tick if side == "buy" else unit.high_tick
    if anchor_at is None:
        anchor_at = unit.price_anchor_time(side)
    # This is the identity schema, independent of NATIVE_SIGNAL_RULE_BASIS.
    # Unchanged center/member evidence keeps its ID across rule revisions.
    point_id = stable_structure_id(
        "native-center-signal-v3", center.price_basis_revision, kind,
        center.center_id, related_unit_ids,
    )
    center_known_at = (
        center.completion_available_at if kind.startswith("3")
        else center.established_at
    )
    if center_known_at is None or unit.confirmed_at is None:
        raise ValueError("confirmed center signal requires locked source clocks")
    confirmed_at = max(
        value for value in (unit.available_at, center_known_at, source_known_at)
        if value is not None
    )
    available_at = max(unit.available_at, confirmed_at)
    return {
        "schema": "chanlun-chart-point",
        "render_kind": "point_confirmed",
        "render_id": point_id,
        "point_id": point_id,
        "point_type": kind,
        "side": side,
        "status": "confirmed",
        "strict_status": "confirmed",
        "evidence_scope": "native_center_signal",
        "rule_basis": NATIVE_SIGNAL_RULE_BASIS,
        "member_basis": "native_locked_segment",
        "formal_point_issued": True,
        "tradable": True,
        "structural_level": 0,
        "source_kind": "segment",
        "price_basis_revision": center.price_basis_revision,
        "anchor_unit_id": unit.unit_id,
        "price_anchor_unit_id": price_anchor_unit_id or unit.unit_id,
        "anchor_at": _epoch(anchor_at),
        "anchor_tick": anchor_tick,
        "invalidation_tick": (
            center.zg_tick if kind == "3buy" else
            center.zd_tick if kind == "3sell" else anchor_tick
        ),
        "confirmed_at": _epoch(confirmed_at),
        "available_at": _epoch(available_at),
        "center_id": center.center_id,
        "center_zd_tick": center.zd_tick,
        "center_zg_tick": center.zg_tick,
        "center_ordinal": center_ordinal,
        "parent_point_id": parent_point_id,
        "related_unit_ids": list(related_unit_ids),
        "evidence_codes": list(evidence_codes),
        "missing_conditions": [],
        "divergence": divergence,
        "points": [{"time": _epoch(anchor_at), "price_tick": anchor_tick}],
    }


def _center_sources_known(center, unit_by_id, as_of):
    roles = (*center.establishment_units, *center.body_units,
             *center.failed_departure_units, *center.supersession_bridge_units,
             center.entry_unit, center.completion_leave_unit,
             center.completion_return_unit)
    return (
        center.structural_level == 0 and center.source_kind is SourceKind.SEGMENT
        and all(
            unit_by_id.get(unit.unit_id) == unit
            and unit.source_kind is SourceKind.SEGMENT
            and unit.structural_level == 0
            and unit.price_basis_revision == center.price_basis_revision
            and unit.locked and unit.confirmed_at is not None
            and unit.market_start < unit.market_end
            and unit.available_at <= as_of
            for unit in roles if unit is not None
        )
    )


def build_native_center_signals(
    units: tuple[ConstituentUnit, ...],
    centers: tuple[TrendCenter, ...],
    strength: MacdStrengthProvider | None,
    *,
    as_of: datetime,
) -> list[dict[str, object]]:
    """Confirm local-policy points only from causal center and leg evidence.

    A first-class point needs two separated same-grade centers and a completed
    three-segment departure after the later center's third-class return. The
    MACD comparison uses the later center's actual entry and departure legs.
    """

    values = tuple(units)
    visible = tuple(center for center in centers if center.available_at <= as_of)
    unit_index = {unit.unit_id: index for index, unit in enumerate(values)}
    if len(unit_index) != len(values):
        raise ValueError("native signal source unit identities must be unique")
    unit_by_id = {unit.unit_id: unit for unit in values}
    valid_centers = {
        center.center_id for center in visible
        if _center_sources_known(center, unit_by_id, as_of)
    }
    center_by_id = {center.center_id: center for center in visible}
    signals: list[dict[str, object]] = []
    first_by_index: dict[int, dict[str, object]] = {}
    relation_by_center: dict[str, CenterRelation] = {}
    ordinal_by_center: dict[str, tuple[int, int]] = {}
    up_run = down_run = 1
    for index, center in enumerate(visible):
        if index:
            try:
                relation = (
                    classify_center_relation(visible[index - 1], center)
                    if {visible[index - 1].center_id, center.center_id} <= valid_centers
                    else CenterRelation.RECOMPOSITION_PENDING
                )
            except ValueError:
                relation = CenterRelation.RECOMPOSITION_PENDING
            relation_by_center[center.center_id] = relation
            if relation is CenterRelation.UP_TREND:
                up_run += 1
                down_run = 1
            elif relation is CenterRelation.DOWN_TREND:
                down_run += 1
                up_run = 1
            else:
                up_run = down_run = 1
        ordinal_by_center[center.center_id] = (up_run, down_run)

    if strength is not None:
        for previous, center in zip(visible, visible[1:]):
            relation = relation_by_center[center.center_id]
            direction = (
                "up" if relation is CenterRelation.UP_TREND else
                "down" if relation is CenterRelation.DOWN_TREND else None
            )
            if (direction is None or not center.physically_completed
                    or center.completion_direction != direction):
                continue
            start = previous.entry_unit or previous.body_units[0]
            try:
                compared = compare_terminal_trend_divergence(
                    (previous, center), values, strength,
                    trend_start_unit_id=start.unit_id,
                )
            except (FormalDivergenceUnavailable, MacdStrengthUnavailable,
                    TypeError, ValueError):
                continue
            if compared is None:
                continue
            divergence, terminal = compared
            terminal_index = unit_index.get(terminal.unit_id)
            price_index = unit_index.get(divergence.price_anchor_unit_id)
            if (terminal_index is None or price_index is None or terminal.available_at > as_of
                    or terminal.direction != direction
                    or not divergence.is_divergent
                    or not divergence.histogram_area_decayed
                    or (divergence.anchor_tick >= center.zd_tick if direction == "down"
                        else divergence.anchor_tick <= center.zg_tick)):
                continue
            kind = "1buy" if direction == "down" else "1sell"
            signal = _alert(
                kind, terminal, center,
                related_unit_ids=tuple((*divergence.compare_leg_unit_ids,
                                        *divergence.signal_leg_unit_ids)),
                evidence_codes=("two_separated_same_grade_centers",
                                "completed_third_class_departure",
                                "new_trend_price_extreme",
                                "native_macd_histogram_area_decay",
                                "user_declared_signal_policy"),
                divergence={
                    "kind": "native_two_center_divergence",
                    "compare_unit_id": divergence.compare_unit_id,
                    "signal_unit_id": divergence.signal_unit_id,
                    "compare_leg_unit_ids": list(divergence.compare_leg_unit_ids),
                    "signal_leg_unit_ids": list(divergence.signal_leg_unit_ids),
                    "histogram_area_decayed": divergence.histogram_area_decayed,
                    "histogram_peak_decayed": divergence.histogram_peak_decayed,
                    "dif_extreme_decayed": divergence.dif_extreme_decayed,
                    "first_center_id": previous.center_id,
                    "last_center_id": center.center_id,
                },
                anchor_at=divergence.anchor_at,
                anchor_tick=divergence.anchor_tick,
                price_anchor_unit_id=divergence.price_anchor_unit_id,
                source_known_at=divergence.available_at,
            )
            signals.append(signal)
            first_by_index[price_index] = signal

    for index, first in first_by_index.items():
        if index + 2 >= len(values):
            continue
        source, rebound, retest = values[index:index + 3]
        if (any(not unit.locked or unit.confirmed_at is None
                or unit.available_at > as_of for unit in (source, rebound, retest))
                or rebound.direction == source.direction
                or retest.direction != source.direction
                or source.market_end != rebound.market_start
                or rebound.market_end != retest.market_start
                or source.end_tick != rebound.start_tick
                or rebound.end_tick != retest.start_tick
                or (retest.low_tick < first["anchor_tick"] if source.direction == "down"
                    else retest.high_tick > first["anchor_tick"])):
            continue
        center = center_by_id[first["center_id"]]
        signals.append(_alert(
            "2buy" if source.direction == "down" else "2sell", retest, center,
            related_unit_ids=(source.unit_id, rebound.unit_id, retest.unit_id),
            evidence_codes=("confirmed_first_class_parent", "first_retest_from_price_anchor",
                            "first_complete_retest_held",
                            "user_declared_signal_policy"),
            parent_point_id=first["point_id"],
            source_known_at=max(
                datetime.fromtimestamp(first["available_at"], source.available_at.tzinfo),
                source.available_at, rebound.available_at, retest.available_at,
            ),
        ))

    for center in visible:
        if center.center_id not in valid_centers:
            continue
        leave, retest = center.completion_leave_unit, center.completion_return_unit
        if not center.physically_completed or leave is None or retest is None:
            continue
        leave_index = unit_index.get(leave.unit_id)
        if (leave_index is None or leave_index + 1 >= len(values)
                or values[leave_index] != leave
                or values[leave_index + 1] != retest
                or not leave.locked or not retest.locked
                or retest.available_at > as_of
                or leave.market_end != retest.market_start
                or leave.end_tick != retest.start_tick):
            continue
        if (leave.direction == "up" and retest.direction == "down"
                and return_outside_core(direction="up", low_tick=retest.low_tick,
                                        high_tick=retest.high_tick, zd_tick=center.zd_tick, zg_tick=center.zg_tick)):
            kind = "3buy"
            ordinal = ordinal_by_center[center.center_id][0]
        elif (leave.direction == "down" and retest.direction == "up"
              and return_outside_core(direction="down", low_tick=retest.low_tick,
                                      high_tick=retest.high_tick, zd_tick=center.zd_tick, zg_tick=center.zg_tick)):
            kind = "3sell"
            ordinal = ordinal_by_center[center.center_id][1]
        else:
            continue
        signals.append(_alert(
            kind, retest, center,
            related_unit_ids=(leave.unit_id, retest.unit_id),
            evidence_codes=("native_center", "independent_leave",
                            "first_complete_return_held_core",
                            "user_declared_signal_policy"),
            center_ordinal=ordinal,
        ))

    signals.sort(key=lambda item: (item["available_at"], item["point_id"]))
    selected = signals[-100:]
    selected_ids = {item["point_id"] for item in selected}
    by_id = {item["point_id"]: item for item in signals}
    for item in tuple(selected):
        parent_id = item["parent_point_id"]
        if parent_id is not None and parent_id not in selected_ids:
            selected.append(by_id[parent_id])
            selected_ids.add(parent_id)
    selected.sort(key=lambda item: (item["available_at"], item["point_id"]))
    return selected
