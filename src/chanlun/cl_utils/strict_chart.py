"""Strict, atomic chart serialization for source-faithful Chanlun evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.level_catalog import recursive_level_labels
from chanlun.core.strict_structure.models import (
    CenterPreview,
    CenterPreviewState,
    CenterState,
    ConstituentUnit,
    DivergenceEvidence,
    SourceKind,
    StrictEvidenceResult,
    StrictPointEvidence,
    StrictPointStatus,
    TrendCenter,
    TrendType,
)
from chanlun.core.strict_structure.unit_adapter import (
    UnitLockRegistry,
    adapt_lines,
)


CHART_STRUCTURE_SCHEMA = "chanlun-chart-structure/v5"
CHART_CENTER_SCHEMA = "chanlun-chart-center/v5"
_ACTIVE_CENTER_STATES = frozenset({CenterState.ONGOING})
_DISPLAY_SEGMENT_CENTER_ALGORITHM = "chanlun-display-segment-zhongshu/v1"


def aware_datetime_to_epoch_seconds(value: datetime) -> int:
    """Convert an aware datetime to an exact UTC Unix-second coordinate."""

    if not isinstance(value, datetime):
        raise TypeError("chart time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("chart time must be timezone-aware")
    if value.microsecond:
        raise ValueError("chart time must have whole-second precision")
    return int(value.astimezone(timezone.utc).timestamp())


def _optional_epoch(value: datetime | None) -> int | None:
    return None if value is None else aware_datetime_to_epoch_seconds(value)


def _unit_audit_payload(unit: ConstituentUnit) -> dict[str, object]:
    """Serialize one strict unit for center entry/leave audit display."""

    if not isinstance(unit, ConstituentUnit):
        raise TypeError("center audit unit must be a ConstituentUnit")
    return {
        "unit_id": unit.unit_id,
        "direction": unit.direction,
        "start_time": aware_datetime_to_epoch_seconds(unit.market_start),
        "end_time": aware_datetime_to_epoch_seconds(unit.market_end),
        "start_tick": unit.start_tick,
        "end_tick": unit.end_tick,
        "low_tick": unit.low_tick,
        "high_tick": unit.high_tick,
        "locked": unit.locked,
    }


def _center_payload(
    center: TrendCenter,
    *,
    render_kind: str,
    tradable: bool,
) -> dict[str, object]:
    if not isinstance(center, TrendCenter):
        raise TypeError("center must be a TrendCenter")
    if center.zd_tick >= center.zg_tick:
        raise ValueError("formal chart center requires positive core")
    leaving_unit = (
        center.completion_leave_unit
        or center.pending_leave_unit
        or center.initial_exit_unit
    )
    return {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": render_kind,
        "center_id": center.center_id,
        "render_id": (
            f"{center.center_id}@{center.body_revision}@{center.state.value}"
        ),
        "body_revision": center.body_revision,
        "structural_level": center.structural_level,
        "source_kind": center.source_kind.value,
        "state": center.state.value,
        "tradable": tradable,
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(
                    center.core_body_start_market_time
                ),
                "price_tick": center.zg_tick,
            },
            {
                "time": aware_datetime_to_epoch_seconds(
                    center.core_body_end_market_time
                ),
                "price_tick": center.zd_tick,
            },
        ],
        "core": {"zd_tick": center.zd_tick, "zg_tick": center.zg_tick},
        "envelope": {"dd_tick": center.dd_tick, "gg_tick": center.gg_tick},
        "entry_unit_id": center.entry_unit.unit_id,
        "core_unit_ids": [unit.unit_id for unit in center.core_units],
        "initial_exit_unit_id": center.initial_exit_unit.unit_id,
        "initial_unit_ids": [unit.unit_id for unit in center.initial_units],
        "body_unit_ids": [unit.unit_id for unit in center.body_units],
        "extension_unit_ids": [
            unit.unit_id for unit in center.extension_units
        ],
        "pending_leave_unit_id": (
            None
            if center.pending_leave_unit is None
            else center.pending_leave_unit.unit_id
        ),
        "completion_leave_unit_id": (
            None
            if center.completion_leave_unit is None
            else center.completion_leave_unit.unit_id
        ),
        "completion_return_unit_id": (
            None
            if center.completion_return_unit is None
            else center.completion_return_unit.unit_id
        ),
        "completion_direction": center.completion_direction,
        "entering_segment": _unit_audit_payload(center.entry_unit),
        "leaving_segment": _unit_audit_payload(leaving_unit),
        "established_market_time": aware_datetime_to_epoch_seconds(
            center.established_market_time
        ),
        "established_at": aware_datetime_to_epoch_seconds(center.established_at),
        "completed_at": _optional_epoch(center.completed_at),
        "available_at": aware_datetime_to_epoch_seconds(center.available_at),
    }


def strict_center_to_chart_dict(center: TrendCenter) -> dict[str, object]:
    """Serialize a formal center without changing its source geometry."""

    if center.source_kind is SourceKind.STROKE_OBSERVATION:
        raise ValueError("formal serializer rejects stroke observation")
    if not center.tradable:
        raise ValueError("formal chart center must be tradable")
    return _center_payload(
        center,
        render_kind="formal_center",
        tradable=True,
    )


def center_observation_to_chart_dict(
    center: TrendCenter,
) -> dict[str, object]:
    """Serialize an explicitly non-tradable stroke-center observation."""

    if center.source_kind is not SourceKind.STROKE_OBSERVATION:
        raise ValueError("center observation requires stroke_observation source")
    return _center_payload(
        center,
        render_kind="center_observation",
        tradable=False,
    )


def _price_tick(value, quantum: Decimal) -> int:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("center price must be finite") from exc
    if not normalized.is_finite():
        raise ValueError("center price must be finite")
    return int(
        (normalized / quantum).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _display_line_key(line) -> tuple[object, ...]:
    try:
        return (
            type(line).__name__,
            line.type,
            line.start.k.k_index,
            line.end.k.k_index,
        )
    except AttributeError as exc:
        raise ValueError("display center line has incomplete geometry") from exc


def display_segment_center_observations_to_chart_dicts(
    centers,
    lines,
    *,
    price_basis_revision: str,
    price_quantum: Decimal,
    as_of: datetime,
) -> tuple[dict[str, object], ...]:
    """Serialize UI centers against the exact segments displayed by the chart.

    These boxes are a non-tradable chart observation layer.  Formal recursive
    centers and their evidence remain sourced from the fixed strict runtime.
    """

    center_values = tuple(centers)
    if not center_values:
        return ()
    line_values = tuple(lines)
    if not line_values:
        raise ValueError("display segment centers require displayed segments")
    if not isinstance(price_quantum, Decimal):
        raise TypeError("price_quantum must be a Decimal")
    quantum_text = _canonical_quantum(price_quantum)
    quantum = Decimal(quantum_text)
    source_closed_epoch = aware_datetime_to_epoch_seconds(as_of)
    registry = UnitLockRegistry(price_basis_revision)
    units = adapt_lines(
        line_values,
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_quantum=quantum,
        as_of=as_of,
        registry=registry,
    )
    by_identity = {
        id(line): unit for line, unit in zip(line_values, units, strict=True)
    }
    by_geometry: dict[tuple[object, ...], ConstituentUnit] = {}
    for line, unit in zip(line_values, units, strict=True):
        key = _display_line_key(line)
        previous = by_geometry.setdefault(key, unit)
        if previous.unit_id != unit.unit_id:
            raise ValueError("display segments contain duplicate geometry")

    def source_unit(line) -> ConstituentUnit:
        unit = by_identity.get(id(line))
        if unit is None:
            unit = by_geometry.get(_display_line_key(line))
        if unit is None:
            raise ValueError("segment center references a non-displayed segment")
        return unit

    payloads: list[dict[str, object]] = []
    for center in center_values:
        body_lines = tuple(getattr(center, "lines", ()) or ())
        if len(body_lines) < 3:
            raise ValueError("display segment center requires three body segments")
        body_units = tuple(source_unit(line) for line in body_lines)
        entry_line = (
            getattr(center, "entry", None)
            or getattr(center, "start", None)
            or body_lines[0]
        )
        leaving_line = (
            getattr(center, "exit", None)
            or getattr(center, "end", None)
            or body_lines[-1]
        )
        entry_unit = source_unit(entry_line)
        leaving_unit = source_unit(leaving_line)
        core_units = body_units[:3]
        initial_units = body_units[: min(5, len(body_units))]
        initial_exit_unit = initial_units[-1]
        zd_tick = _price_tick(getattr(center, "zd", None), quantum)
        zg_tick = _price_tick(getattr(center, "zg", None), quantum)
        if zd_tick >= zg_tick:
            raise ValueError("display segment center requires a positive core")
        dd_value = getattr(center, "dd", None)
        gg_value = getattr(center, "gg", None)
        dd_tick = (
            min(unit.low_tick for unit in body_units)
            if dd_value is None
            else _price_tick(dd_value, quantum)
        )
        gg_tick = (
            max(unit.high_tick for unit in body_units)
            if gg_value is None
            else _price_tick(gg_value, quantum)
        )
        start_line = getattr(center, "start", None)
        end_line = getattr(center, "end", None)
        core_start = (
            start_line.end.k.date
            if start_line is not None
            else body_lines[0].start.k.date
        )
        core_end = (
            end_line.start.k.date
            if end_line is not None
            else body_lines[-1].end.k.date
        )
        core_start_epoch = aware_datetime_to_epoch_seconds(core_start)
        core_end_epoch = aware_datetime_to_epoch_seconds(core_end)
        if core_start_epoch > core_end_epoch:
            raise ValueError("display segment center has reversed chart time")
        done = bool(getattr(center, "done", False))
        state = "completed" if done else "ongoing"
        body_unit_ids = tuple(unit.unit_id for unit in body_units)
        center_id = stable_structure_id(
            _DISPLAY_SEGMENT_CENTER_ALGORITHM,
            price_basis_revision,
            entry_unit.unit_id,
            tuple(unit.unit_id for unit in core_units),
            zd_tick,
            zg_tick,
        )
        body_revision = len(body_unit_ids)
        render_fingerprint = stable_structure_id(
            "chanlun-display-segment-zhongshu-render/v1",
            center_id,
            body_unit_ids,
            leaving_unit.unit_id,
            state,
        )
        established_at = max(unit.available_at for unit in initial_units)
        available_at = max(
            established_at,
            leaving_unit.available_at,
            *(unit.available_at for unit in body_units),
        )
        if available_at > as_of:
            raise ValueError("display segment center cannot exceed source close")
        payloads.append(
            {
                "schema": CHART_CENTER_SCHEMA,
                "render_kind": "center_observation",
                "center_id": center_id,
                "render_id": (
                    f"{center_id}@{body_revision}@{state}@{render_fingerprint}"
                ),
                "body_revision": body_revision,
                "structural_level": 0,
                "source_kind": SourceKind.SEGMENT.value,
                "state": state,
                "tradable": False,
                "origin": "display_cl_segment_zhongshu",
                "algorithm_revision": _DISPLAY_SEGMENT_CENTER_ALGORITHM,
                "tower": "xd",
                "points": [
                    {"time": core_start_epoch, "price_tick": zg_tick},
                    {"time": core_end_epoch, "price_tick": zd_tick},
                ],
                "core": {"zd_tick": zd_tick, "zg_tick": zg_tick},
                "envelope": {"dd_tick": dd_tick, "gg_tick": gg_tick},
                "entry_unit_id": entry_unit.unit_id,
                "core_unit_ids": [unit.unit_id for unit in core_units],
                "initial_exit_unit_id": initial_exit_unit.unit_id,
                "initial_unit_ids": [unit.unit_id for unit in initial_units],
                "body_unit_ids": list(body_unit_ids),
                "extension_unit_ids": [
                    unit.unit_id for unit in body_units[len(initial_units):]
                ],
                "pending_leave_unit_id": (
                    None if done else leaving_unit.unit_id
                ),
                "completion_leave_unit_id": (
                    leaving_unit.unit_id if done else None
                ),
                "completion_return_unit_id": None,
                "completion_direction": leaving_unit.direction if done else None,
                "entering_segment": _unit_audit_payload(entry_unit),
                "leaving_segment": _unit_audit_payload(leaving_unit),
                "established_market_time": aware_datetime_to_epoch_seconds(
                    initial_exit_unit.market_end
                ),
                "established_at": aware_datetime_to_epoch_seconds(
                    established_at
                ),
                "completed_at": (
                    aware_datetime_to_epoch_seconds(leaving_unit.available_at)
                    if done
                    else None
                ),
                "available_at": aware_datetime_to_epoch_seconds(available_at),
                "done": done,
                "linestyle": "0" if done else "1",
                "line_count": len(body_units),
            }
        )
    if any(int(item["available_at"]) > source_closed_epoch for item in payloads):
        raise ValueError("display segment center is not causally visible")
    return tuple(payloads)


def active_center_projection_to_chart_dict(
    center: TrendCenter,
    source_closed_at: datetime,
) -> dict[str, object]:
    """Build the single display box for an active center through source close."""

    if center.source_kind is SourceKind.STROKE_OBSERVATION:
        raise ValueError("formal center projection rejects stroke observation")
    if center.state not in _ACTIVE_CENTER_STATES:
        raise ValueError("center projection requires an active center")
    closed_epoch = aware_datetime_to_epoch_seconds(source_closed_at)
    body_end_epoch = aware_datetime_to_epoch_seconds(
        center.core_body_end_market_time
    )
    if closed_epoch < body_end_epoch:
        raise ValueError("source close cannot precede center core body end")
    core_start_epoch = aware_datetime_to_epoch_seconds(
        center.core_body_start_market_time
    )
    leaving_unit = center.pending_leave_unit or center.initial_exit_unit
    return {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": "center_projection",
        "center_id": center.center_id,
        "render_id": (
            f"{center.center_id}@{center.body_revision}@{center.state.value}"
            f"@projection@{closed_epoch}"
        ),
        "body_revision": center.body_revision,
        "structural_level": center.structural_level,
        "source_kind": center.source_kind.value,
        "state": center.state.value,
        "tradable": False,
        "points": [
            {"time": core_start_epoch, "price_tick": center.zg_tick},
            {"time": closed_epoch, "price_tick": center.zd_tick},
        ],
        "core": {"zd_tick": center.zd_tick, "zg_tick": center.zg_tick},
        "entering_segment": _unit_audit_payload(center.entry_unit),
        "leaving_segment": _unit_audit_payload(leaving_unit),
        "source_center_render_id": (
            f"{center.center_id}@{center.body_revision}@{center.state.value}"
        ),
        "available_at": aware_datetime_to_epoch_seconds(center.available_at),
    }


def strict_center_preview_to_chart_dict(
    preview: CenterPreview,
    units: tuple[ConstituentUnit, ...],
    source_closed_at: datetime,
) -> dict[str, object]:
    """Serialize one non-tradable provisional center lifecycle."""

    if not isinstance(preview, CenterPreview):
        raise TypeError("preview must be a CenterPreview")
    if (
        preview.state not in (
            CenterPreviewState.FORMING,
            CenterPreviewState.COMPLETED,
        )
        or len(preview.unit_ids) < 5
        or preview.zd_tick is None
        or preview.zg_tick is None
        or preview.zd_tick >= preview.zg_tick
    ):
        raise ValueError("chart center preview requires a positive center core")

    values = tuple(units)
    by_id = {item.unit_id: item for item in values}
    if len(by_id) != len(values):
        raise ValueError("chart center preview units require unique ids")
    try:
        body = tuple(by_id[item_id] for item_id in preview.unit_ids)
        completion_return = (
            None
            if preview.completion_return_unit_id is None
            else by_id[preview.completion_return_unit_id]
        )
    except KeyError as exc:
        raise ValueError("chart center preview unit is absent from level") from exc
    initial = body[:5]
    if any(
        item.structural_level != preview.structural_level
        or item.source_kind is not preview.source_kind
        or item.price_basis_revision != preview.price_basis_revision
        for item in body + (() if completion_return is None else (completion_return,))
    ):
        raise ValueError("chart center preview unit context mismatch")
    unlocked_seen = False
    lifecycle_units = body + (() if completion_return is None else (completion_return,))
    for item in lifecycle_units:
        if not item.locked:
            unlocked_seen = True
        elif unlocked_seen:
            raise ValueError("chart center preview units must have a locked prefix")
    if not unlocked_seen:
        raise ValueError("chart center preview requires provisional units")
    for previous, current in zip(lifecycle_units, lifecycle_units[1:]):
        if (
            previous.direction == current.direction
            or previous.end_tick != current.start_tick
            or current.market_start < previous.market_end
        ):
            raise ValueError("chart center preview lifecycle is disconnected")
    expected_zd = max(item.low_tick for item in initial[1:4])
    expected_zg = min(item.high_tick for item in initial[1:4])
    if (preview.zd_tick, preview.zg_tick) != (expected_zd, expected_zg):
        raise ValueError("chart center preview core does not match its seed")
    # The five seed roles are strict: entry, three core units and leave.  The
    # old-center leave may be reused only as the new entry, never as a core
    # unit, so every preview body unit must positively overlap its fixed core.
    if any(
        max(item.low_tick, preview.zd_tick)
        >= min(item.high_tick, preview.zg_tick)
        for item in body
    ):
        raise ValueError("chart center preview body must overlap its core")
    if source_closed_at < lifecycle_units[-1].market_end:
        raise ValueError("source close cannot precede center preview tail")
    if preview.state is CenterPreviewState.COMPLETED:
        if completion_return is None:
            raise ValueError("completed chart preview requires a return unit")
        completion_leave = body[-1]
        if (
            completion_leave.direction != initial[0].direction
            or (
                completion_leave.end_tick <= preview.zg_tick
                if completion_leave.direction == "up"
                else completion_leave.end_tick >= preview.zd_tick
            )
        ):
            raise ValueError("completed chart preview leave geometry is invalid")
        return_stays_outside = (
            completion_return.low_tick >= preview.zg_tick
            if completion_leave.direction == "up"
            else completion_return.high_tick <= preview.zd_tick
        )
        if not return_stays_outside:
            raise ValueError("completed chart preview return must stay outside its core")
    elif completion_return is not None:
        raise ValueError("forming chart preview cannot retain a return unit")

    preview_id = stable_structure_id(
        "chanlun-center-preview/v1",
        preview.price_basis_revision,
        preview.structural_level,
        preview.source_kind.value,
        preview.unit_ids[:5],
        preview.zd_tick,
        preview.zg_tick,
    )
    closed_epoch = aware_datetime_to_epoch_seconds(source_closed_at)
    initial_unit_ids = [item.unit_id for item in initial]
    completed = preview.state is CenterPreviewState.COMPLETED
    leaving_unit = body[-1]
    end_epoch = (
        aware_datetime_to_epoch_seconds(leaving_unit.market_start)
        if completed
        else closed_epoch
    )
    return {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": "center_preview",
        "center_id": preview_id,
        "preview_id": preview_id,
        "render_id": (
            f"{preview_id}@{preview.state.value}@{len(body) - 5}"
            f"@{preview.completion_return_unit_id or closed_epoch}"
        ),
        "body_revision": len(body) - 5,
        "structural_level": preview.structural_level,
        "source_kind": preview.source_kind.value,
        "state": preview.state.value,
        "tradable": False,
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(initial[1].market_start),
                "price_tick": preview.zg_tick,
            },
            {"time": end_epoch, "price_tick": preview.zd_tick},
        ],
        "core": {"zd_tick": preview.zd_tick, "zg_tick": preview.zg_tick},
        "envelope": {
            "dd_tick": min(item.low_tick for item in body),
            "gg_tick": max(item.high_tick for item in body),
        },
        "entry_unit_id": initial[0].unit_id,
        "core_unit_ids": [item.unit_id for item in initial[1:4]],
        "initial_exit_unit_id": initial[4].unit_id,
        "initial_unit_ids": initial_unit_ids,
        "body_unit_ids": [item.unit_id for item in body],
        "extension_unit_ids": [item.unit_id for item in body[5:]],
        "pending_leave_unit_id": (
            leaving_unit.unit_id
            if (
                not completed
                and leaving_unit.direction == initial[0].direction
                and (
                    leaving_unit.end_tick > preview.zg_tick
                    if leaving_unit.direction == "up"
                    else leaving_unit.end_tick < preview.zd_tick
                )
            )
            else None
        ),
        "completion_leave_unit_id": leaving_unit.unit_id if completed else None,
        "completion_return_unit_id": (
            None if completion_return is None else completion_return.unit_id
        ),
        "completion_direction": leaving_unit.direction if completed else None,
        "entering_segment": _unit_audit_payload(initial[0]),
        "leaving_segment": _unit_audit_payload(leaving_unit),
        "established_market_time": aware_datetime_to_epoch_seconds(
            initial[4].market_end
        ),
        "established_at": _optional_epoch(initial[4].confirmed_at),
        # Geometry is complete, but an unlocked return has no formal
        # confirmation timestamp.  Keep this null so consumers cannot mistake
        # a provisional third-class shape for a confirmed trading signal.
        "completed_at": None,
        "available_at": aware_datetime_to_epoch_seconds(preview.available_at),
    }


def strict_trend_to_chart_dict(trend: TrendType) -> dict[str, object]:
    """Serialize one strict current or completed trend snapshot."""

    if not isinstance(trend, TrendType):
        raise TypeError("trend must be a TrendType")
    terminal_id = trend.terminal_unit.unit_id
    return {
        "schema": "chanlun-chart-trend/v3",
        "render_kind": "strict_trend",
        "trend_id": trend.trend_id,
        "render_id": f"{trend.trend_id}@{trend.state.value}@{terminal_id}",
        "structural_level": trend.structural_level,
        "source_kind": trend.terminal_unit.source_kind.value,
        "state": trend.state.value,
        "kind": trend.kind.value,
        "direction": trend.direction,
        "tradable": trend.terminal_unit.source_kind is not SourceKind.STROKE_OBSERVATION,
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(trend.market_start),
                "price_tick": trend.start_tick,
            },
            {
                "time": aware_datetime_to_epoch_seconds(trend.market_end),
                "price_tick": trend.end_tick,
            },
        ],
        "range": {"low_tick": trend.low_tick, "high_tick": trend.high_tick},
        "center_ids": [center.center_id for center in trend.centers],
        "constituent_unit_ids": [
            unit.unit_id for unit in trend.constituent_units
        ],
        "confirmed_at": _optional_epoch(trend.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(trend.available_at),
    }


def strict_divergence_to_chart_dict(
    divergence: DivergenceEvidence,
) -> dict[str, object]:
    """Serialize one independent, level-scoped formal divergence."""

    if not isinstance(divergence, DivergenceEvidence):
        raise TypeError("divergence must be a DivergenceEvidence")
    if divergence.source_kind is SourceKind.STROKE_OBSERVATION:
        raise ValueError("formal divergence rejects stroke observation")
    metrics = {
        "price_extreme_confirmed": divergence.price_extreme_confirmed,
        "histogram_area_decayed": divergence.histogram_area_decayed,
        "histogram_peak_decayed": divergence.histogram_peak_decayed,
        "dif_extreme_decayed": divergence.dif_extreme_decayed,
        "strength_source": divergence.strength_source,
        "is_divergent": divergence.is_divergent,
    }
    return {
        "schema": "chanlun-chart-divergence/v4",
        "render_kind": "strict_divergence",
        "render_id": divergence.divergence_id,
        "divergence_id": divergence.divergence_id,
        "kind": divergence.kind,
        "direction": divergence.direction,
        "structural_level": divergence.structural_level,
        "source_kind": divergence.source_kind.value,
        "price_basis_revision": divergence.price_basis_revision,
        "compare_unit_id": divergence.compare_unit_id,
        "signal_unit_id": divergence.signal_unit_id,
        "anchor_at": aware_datetime_to_epoch_seconds(divergence.anchor_at),
        "anchor_tick": divergence.anchor_tick,
        "confirmed_at": aware_datetime_to_epoch_seconds(
            divergence.confirmed_at
        ),
        "available_at": aware_datetime_to_epoch_seconds(
            divergence.available_at
        ),
        "metrics": metrics,
        "tradable": True,
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(divergence.anchor_at),
                "price_tick": divergence.anchor_tick,
            }
        ],
    }


def _divergence_payload(point: StrictPointEvidence) -> dict[str, object] | None:
    divergence = point.divergence
    if divergence is None:
        return None
    payload = strict_divergence_to_chart_dict(divergence)
    return {
        key: value
        for key, value in payload.items()
        if key not in {"schema", "render_kind", "render_id", "tradable", "points"}
    }


def strict_point_to_chart_dict(
    point: StrictPointEvidence,
) -> dict[str, object]:
    """Serialize a confirmed or approaching strict point without reclassification."""

    if not isinstance(point, StrictPointEvidence):
        raise TypeError("point must be StrictPointEvidence")
    status = point.status.value
    evidence_revision = "sha256:" + stable_structure_id(
        "chanlun-chart-point-evidence/v3",
        point.point_id,
        status,
        point.variant.value,
        point.available_at,
        point.evidence_codes,
        point.missing_conditions,
        point.related_point_ids,
    )
    render_kind = (
        "point_confirmed"
        if point.status is StrictPointStatus.CONFIRMED
        else "point_approaching"
    )
    render_id = (
        point.point_id
        if point.status is StrictPointStatus.CONFIRMED
        else f"{point.point_id}@{evidence_revision}"
    )
    return {
        "schema": "chanlun-chart-point/v3",
        "render_kind": render_kind,
        "render_id": render_id,
        "point_id": point.point_id,
        "point_type": point.point_type,
        "side": point.side,
        "status": status,
        "variant": point.variant.value,
        "structural_level": point.structural_level,
        "source_kind": point.source_kind.value,
        "price_basis_revision": point.price_basis_revision,
        "anchor_unit_id": point.anchor_unit_id,
        "anchor_at": aware_datetime_to_epoch_seconds(point.anchor_at),
        "confirmed_at": _optional_epoch(point.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(point.available_at),
        "anchor_tick": point.anchor_tick,
        "invalidation_tick": point.invalidation_tick,
        "center_id": point.center_id,
        "center_zd_tick": point.center_zd_tick,
        "center_zg_tick": point.center_zg_tick,
        "center_ordinal": point.center_ordinal,
        "parent_point_id": point.parent_point_id,
        "divergence": _divergence_payload(point),
        "evidence_codes": list(point.evidence_codes),
        "missing_conditions": list(point.missing_conditions),
        "related_point_ids": list(point.related_point_ids),
        "evidence_revision": evidence_revision,
        "tradable": point.status is StrictPointStatus.CONFIRMED,
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(point.anchor_at),
                "price_tick": point.anchor_tick,
            }
        ],
    }


def _canonical_quantum(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("structure_price_quantum must be a positive Decimal")
    return format(value.normalize(), "f")


def _tick_price(tick: int, quantum: Decimal) -> float:
    if type(tick) is not int:
        raise TypeError("price_tick must be an integer")
    value = Decimal(tick) * quantum
    if not value.is_finite():
        raise ValueError("chart price must be finite")
    return float(value)


def _with_prices(value: object, quantum: Decimal) -> object:
    if isinstance(value, list):
        return [_with_prices(item, quantum) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _with_prices(item, quantum) for key, item in value.items()}
    tick_price_names = {
        "price_tick": "price",
        "anchor_tick": "anchor_price",
        "invalidation_tick": "invalidation_price",
        "center_zd_tick": "center_zd_price",
        "center_zg_tick": "center_zg_price",
        "zd_tick": "zd_price",
        "zg_tick": "zg_price",
        "dd_tick": "dd_price",
        "gg_tick": "gg_price",
        "low_tick": "low_price",
        "high_tick": "high_price",
        "start_tick": "start_price",
        "end_tick": "end_price",
    }
    for tick_name, price_name in tick_price_names.items():
        tick = value.get(tick_name)
        if tick is not None:
            result[price_name] = _tick_price(tick, quantum)
    return result


def _visible(values: Iterable[object], source_closed_at: datetime) -> tuple:
    return tuple(
        item
        for item in values
        if getattr(item, "available_at") <= source_closed_at
    )


def _sorted_payloads(values: Iterable[dict[str, object]], id_name: str) -> list:
    return sorted(
        values,
        key=lambda item: (
            int(item.get("available_at", 0)),
            str(item.get(id_name, "")),
            str(item.get("render_id", "")),
        ),
    )


def _revision(namespace: str, *parts: object) -> str:
    return "sha256:" + stable_structure_id(namespace, *parts)


def build_strict_structure_snapshot(
    evidence: StrictEvidenceResult,
    *,
    interval: str,
    display_center_observation_payloads: Iterable[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build one authoritative, window-independent strict chart snapshot.

    ``display_center_observation_payloads`` may provide a separate,
    non-tradable chart layer derived from the displayed segments. Formal
    centers, trends, signals and stroke evidence remain sourced from
    ``evidence``.
    """

    if not isinstance(evidence, StrictEvidenceResult):
        raise TypeError("evidence must be StrictEvidenceResult")
    if not isinstance(interval, str) or not interval.strip():
        raise ValueError("interval is required")
    if interval != evidence.source_frequency:
        raise ValueError("display interval must equal strict source frequency")
    if evidence.structure.price_basis_revision != evidence.price_basis_revision:
        raise ValueError("strict structure price basis mismatch")
    quantum = evidence.structure_price_quantum
    quantum_text = _canonical_quantum(quantum)
    source_closed_epoch = aware_datetime_to_epoch_seconds(
        evidence.source_closed_at
    )
    labels = recursive_level_labels(interval)

    confirmed_by_level: dict[int, list[StrictPointEvidence]] = {}
    approaching_by_level: dict[int, list[StrictPointEvidence]] = {}
    divergences_by_level: dict[int, list[DivergenceEvidence]] = {}
    for point in _visible(evidence.confirmed_points, evidence.source_closed_at):
        confirmed_by_level.setdefault(point.structural_level, []).append(point)
    for point in _visible(evidence.approaching_points, evidence.source_closed_at):
        approaching_by_level.setdefault(point.structural_level, []).append(point)
    for divergence in _visible(evidence.divergences, evidence.source_closed_at):
        divergences_by_level.setdefault(
            divergence.structural_level,
            [],
        ).append(divergence)

    known_levels = {level.structural_level for level in evidence.structure.levels}
    point_levels = set(confirmed_by_level) | set(approaching_by_level)
    if not point_levels.issubset(known_levels):
        raise ValueError("strict point level is absent from structure snapshot")
    if not set(divergences_by_level).issubset(known_levels):
        raise ValueError("strict divergence level is absent from structure snapshot")
    if any(
        type(level.structural_level) is not int
        or level.structural_level < 0
        or level.structural_level >= len(labels)
        for level in evidence.structure.levels
    ):
        raise ValueError("strict structure level is outside display catalog")

    levels: list[dict[str, object]] = []
    for level in evidence.structure.levels:
        centers = _visible(
            level.center_result.centers,
            evidence.source_closed_at,
        )
        center_previews = tuple(
            preview
            for preview in _visible(
                level.center_result.previews,
                evidence.source_closed_at,
            )
            if preview.state in (
                CenterPreviewState.FORMING,
                CenterPreviewState.COMPLETED,
            )
            and len(preview.unit_ids) >= 5
            and preview.zd_tick is not None
            and preview.zg_tick is not None
            and preview.zd_tick < preview.zg_tick
        )
        current_trends = _visible(
            level.trend_types,
            evidence.source_closed_at,
        )
        completed_trends = _visible(
            level.completed_trends,
            evidence.source_closed_at,
        )
        center_payloads = _sorted_payloads(
            (strict_center_to_chart_dict(center) for center in centers),
            "center_id",
        )
        preview_payloads = _sorted_payloads(
            (
                strict_center_preview_to_chart_dict(
                    preview,
                    level.units,
                    evidence.source_closed_at,
                )
                for preview in center_previews
            ),
            "center_id",
        )
        active_center = (
            centers[-1]
            if centers and centers[-1].state in _ACTIVE_CENTER_STATES
            else None
        )
        projections = _sorted_payloads(
            ()
            if active_center is None or preview_payloads
            else (
                active_center_projection_to_chart_dict(
                    active_center,
                    evidence.source_closed_at,
                ),
            ),
            "center_id",
        )
        current_payloads = _sorted_payloads(
            (strict_trend_to_chart_dict(trend) for trend in current_trends),
            "trend_id",
        )
        completed_payloads = _sorted_payloads(
            (
                strict_trend_to_chart_dict(trend)
                for trend in completed_trends
            ),
            "trend_id",
        )
        confirmed_payloads = _sorted_payloads(
            (
                strict_point_to_chart_dict(point)
                for point in confirmed_by_level.get(level.structural_level, ())
            ),
            "point_id",
        )
        approaching_payloads = _sorted_payloads(
            (
                strict_point_to_chart_dict(point)
                for point in approaching_by_level.get(level.structural_level, ())
            ),
            "point_id",
        )
        divergence_payloads = _sorted_payloads(
            (
                strict_divergence_to_chart_dict(divergence)
                for divergence in divergences_by_level.get(
                    level.structural_level,
                    (),
                )
            ),
            "divergence_id",
        )
        levels.append(
            {
                "structural_level": level.structural_level,
                "label": labels[level.structural_level],
                "origin": "current_chart_recursive",
                "centers": center_payloads,
                "center_projections": projections,
                "center_previews": preview_payloads,
                "current_trends": current_payloads,
                "completed_trend_snapshots": completed_payloads,
                "confirmed_points": confirmed_payloads,
                "approaching_points": approaching_payloads,
                "divergences": divergence_payloads,
            }
        )

    observations = _sorted_payloads(
        (
            center_observation_to_chart_dict(center)
            for center in _visible(
                evidence.stroke_center_observations.centers,
                evidence.source_closed_at,
            )
        ),
        "center_id",
    )
    display_observations = (
        list(observations)
        if display_center_observation_payloads is None
        else _sorted_payloads(
            (dict(item) for item in display_center_observation_payloads),
            "center_id",
        )
    )
    for observation in display_observations:
        if (
            observation.get("render_kind") != "center_observation"
            or observation.get("source_kind")
            not in {
                SourceKind.SEGMENT.value,
                SourceKind.STROKE_OBSERVATION.value,
            }
            or observation.get("tradable") is not False
            or observation.get("structural_level") != 0
        ):
            raise ValueError("invalid display center observation payload")
        if not isinstance(observation.get("center_id"), str) or not isinstance(
            observation.get("render_id"), str
        ):
            raise ValueError("display center observation identity is required")
        points = observation.get("points")
        if not isinstance(points, list) or len(points) != 2:
            raise ValueError("display center observation requires two points")
        available_at = observation.get("available_at")
        if type(available_at) is not int or available_at > source_closed_epoch:
            raise ValueError("display center observation is not causally visible")
    snapshot_revision = _revision(
        "chanlun-chart-snapshot/v5",
        evidence.structure_revision,
        source_closed_epoch,
    )
    render_extras = {
        "stroke_center_observations": observations,
        "display_center_observations": display_observations,
        "level_extras": [
            {
                "structural_level": level["structural_level"],
                "center_projections": level["center_projections"],
                "center_previews": level["center_previews"],
                "approaching_points": level["approaching_points"],
            }
            for level in levels
        ],
    }
    render_revision = _revision(
        "chanlun-chart-render/v7",
        snapshot_revision,
        render_extras,
    )
    snapshot = {
        "schema": CHART_STRUCTURE_SCHEMA,
        "symbol": evidence.symbol,
        "source_frequency": evidence.source_frequency,
        "display_frequency": interval,
        "price_basis_revision": evidence.price_basis_revision,
        "structure_price_quantum": quantum_text,
        "strict_config_revision": evidence.strict_config_revision,
        "source_closed_at": source_closed_epoch,
        "structure_revision": evidence.structure_revision,
        "snapshot_revision": snapshot_revision,
        "render_revision": render_revision,
        "stroke_center_observations": observations,
        "display_center_observations": display_observations,
        "levels": levels,
    }
    return _with_prices(snapshot, quantum)


__all__ = [
    "CHART_CENTER_SCHEMA",
    "CHART_STRUCTURE_SCHEMA",
    "active_center_projection_to_chart_dict",
    "aware_datetime_to_epoch_seconds",
    "build_strict_structure_snapshot",
    "center_observation_to_chart_dict",
    "display_segment_center_observations_to_chart_dicts",
    "strict_center_preview_to_chart_dict",
    "strict_center_to_chart_dict",
    "strict_divergence_to_chart_dict",
    "strict_point_to_chart_dict",
    "strict_trend_to_chart_dict",
]
