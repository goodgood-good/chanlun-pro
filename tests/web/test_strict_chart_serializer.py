from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.cl_utils.strict_chart import (
    active_center_projection_to_chart_dict,
    aware_datetime_to_epoch_seconds,
    build_strict_structure_snapshot,
    center_observation_to_chart_dict,
    strict_center_to_chart_dict,
    strict_point_to_chart_dict,
    strict_trend_to_chart_dict,
)
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.identity import (
    build_strict_evidence_revision,
    stable_structure_id,
)
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    ConstituentUnit,
    DivergenceEvidence,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictPointStatus,
    StrictStructureResult,
    TrendCenter,
    TrendKind,
    TrendState,
    TrendType,
)
from tests.core.strict_structure.helpers import completed_up_center
from tests.trading_system.strict_helpers import strict_point


CN = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 7, 20, 9, 30, tzinfo=CN)
PRICE_BASIS = "test-raw-v1"
QUANTUM = Decimal("0.01")


def _unit(
    index: int,
    direction: str,
    start_tick: int,
    end_tick: int,
    *,
    source_kind: SourceKind = SourceKind.SEGMENT,
) -> ConstituentUnit:
    market_start = BASE + timedelta(minutes=index * 5)
    market_end = market_start + timedelta(minutes=5)
    return ConstituentUnit(
        unit_id=f"{source_kind.value}-u-{index}",
        structural_level=0,
        source_kind=source_kind,
        price_basis_revision=PRICE_BASIS,
        direction=direction,
        start_tick=start_tick,
        end_tick=end_tick,
        low_tick=min(start_tick, end_tick),
        high_tick=max(start_tick, end_tick),
        market_start=market_start,
        market_end=market_end,
        confirmed_at=market_end + timedelta(minutes=5),
        available_at=market_end + timedelta(minutes=5),
        locked=True,
        child_ids=(),
    )


def _center(
    *,
    source_kind: SourceKind = SourceKind.SEGMENT,
    extension: bool = False,
) -> TrendCenter:
    initial = (
        _unit(0, "up", 90, 120, source_kind=source_kind),
        _unit(1, "down", 120, 100, source_kind=source_kind),
        _unit(2, "up", 100, 115, source_kind=source_kind),
        _unit(3, "down", 115, 105, source_kind=source_kind),
        _unit(4, "up", 105, 130, source_kind=source_kind),
    )
    value = establish_center(initial, 0, source_kind)
    assert value is not None
    if extension:
        value, _event = advance_center(
            value,
            _unit(5, "down", 130, 110, source_kind=source_kind),
        )
    return value


def _trend(center: TrendCenter) -> TrendType:
    units = center.body_units
    direction = (
        "up" if units[-1].end_tick > units[0].start_tick else "down"
    )
    return TrendType(
        trend_id="trend-0",
        structural_level=0,
        price_basis_revision=PRICE_BASIS,
        kind=TrendKind.CONSOLIDATION,
        direction=direction,
        state=TrendState.FORMING,
        centers=(center,),
        constituent_units=units,
        start_tick=units[0].start_tick,
        end_tick=units[-1].end_tick,
        low_tick=min(item.low_tick for item in units),
        high_tick=max(item.high_tick for item in units),
        market_start=units[0].market_start,
        market_end=units[-1].market_end,
        confirmed_at=None,
        available_at=max(
            center.available_at,
            *(item.available_at for item in units),
        ),
    )


def _evidence(
    *,
    observation: TrendCenter | None = None,
    confirmed_points=(),
    approaching_points=(),
    divergences=(),
    level_count: int = 1,
    source_frequency: str = "5m",
) -> StrictEvidenceResult:
    formal_center = _center(extension=True)
    trend = _trend(formal_center)
    center_result = CenterLevelResult(
        structural_level=0,
        price_basis_revision=PRICE_BASIS,
        centers=(formal_center,),
        previews=(),
        events=(),
        locked_unit_count=len(formal_center.body_units),
        replay_from=0,
    )
    first_level = StrictLevelResult(
        structural_level=0,
        units=formal_center.body_units,
        center_result=center_result,
        trend_types=(trend,),
        completed_trends=(),
    )
    levels = [first_level]
    for structural_level in range(1, level_count):
        empty_centers = CenterLevelResult(
            structural_level=structural_level,
            price_basis_revision=PRICE_BASIS,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=0,
            replay_from=0,
        )
        levels.append(
            StrictLevelResult(
                structural_level=structural_level,
                units=(),
                center_result=empty_centers,
                trend_types=(),
                completed_trends=(),
            )
        )
    structure = StrictStructureResult(
        schema_version="chanlun-structure/v3",
        price_basis_revision=PRICE_BASIS,
        levels=tuple(levels),
    )
    observation_result = CenterLevelResult(
        structural_level=0,
        price_basis_revision=PRICE_BASIS,
        centers=() if observation is None else (observation,),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )
    revision = build_strict_evidence_revision(
        symbol="SH.600519",
        source_frequency=source_frequency,
        price_basis_revision=PRICE_BASIS,
        strict_config_revision="strict-config-v1",
        structure=structure,
        confirmed_points=confirmed_points,
        divergences=divergences,
    )
    return StrictEvidenceResult(
        symbol="SH.600519",
        source_frequency=source_frequency,
        source_closed_at=BASE + timedelta(hours=6),
        price_basis_revision=PRICE_BASIS,
        structure_price_quantum=QUANTUM,
        strict_config_revision="strict-config-v1",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=observation_result,
        confirmed_points=tuple(confirmed_points),
        approaching_points=tuple(approaching_points),
        divergences=tuple(divergences),
    )


def test_formal_center_rectangle_uses_core_not_envelope() -> None:
    payload = strict_center_to_chart_dict(_center(extension=True))

    assert [point["price_tick"] for point in payload["points"]] == [115, 105]
    assert payload["envelope"] == {"dd_tick": 90, "gg_tick": 130}


def test_v4_center_payload_exposes_five_roles_and_completion_state() -> None:
    center = completed_up_center()
    payload = strict_center_to_chart_dict(center)

    assert payload["schema"] == "chanlun-chart-center/v4"
    assert payload["state"] == "completed"
    assert payload["entry_unit_id"] == center.entry_unit.unit_id
    assert payload["core_unit_ids"] == [
        item.unit_id for item in center.core_units
    ]
    assert payload["initial_exit_unit_id"] == center.initial_exit_unit.unit_id
    assert (
        payload["completion_leave_unit_id"]
        == center.completion_leave_unit.unit_id
    )
    assert (
        payload["completion_return_unit_id"]
        == center.completion_return_unit.unit_id
    )


def test_center_render_id_changes_on_body_revision_or_state_only() -> None:
    established = strict_center_to_chart_dict(_center())
    extended = strict_center_to_chart_dict(_center(extension=True))

    assert established["center_id"] == extended["center_id"]
    assert established["render_id"] != extended["render_id"]


def test_stroke_observation_is_explicitly_non_tradable() -> None:
    payload = center_observation_to_chart_dict(
        _center(source_kind=SourceKind.STROKE_OBSERVATION)
    )

    assert payload["source_kind"] == "stroke_observation"
    assert payload["tradable"] is False
    assert payload["render_kind"] == "center_observation"


def test_formal_serializer_rejects_stroke_observation() -> None:
    with pytest.raises(ValueError, match="formal serializer rejects"):
        strict_center_to_chart_dict(
            _center(source_kind=SourceKind.STROKE_OBSERVATION)
        )


def test_chart_times_are_utc_epoch_seconds_and_reject_naive_datetime() -> None:
    center = _center()
    payload = strict_center_to_chart_dict(center)

    assert payload["points"][0]["time"] == int(
        center.body_start_market_time.timestamp()
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        aware_datetime_to_epoch_seconds(
            center.body_start_market_time.replace(tzinfo=None)
        )


def test_active_projection_is_separate_non_tradable_overlay() -> None:
    center = _center(extension=True)
    source_closed_at = BASE + timedelta(hours=6)

    body = strict_center_to_chart_dict(center)
    projection = active_center_projection_to_chart_dict(
        center,
        source_closed_at,
    )

    assert body["points"][1]["time"] == int(
        center.last_touch_market_time.timestamp()
    )
    assert projection["render_kind"] == "center_projection"
    assert projection["tradable"] is False
    assert projection["points"][1]["time"] == int(source_closed_at.timestamp())
    assert projection["core"] == body["core"]


def test_trend_and_point_serializers_preserve_strict_identity() -> None:
    center = _center(extension=True)
    trend = strict_trend_to_chart_dict(_trend(center))
    source_point = strict_point("3buy")
    point = strict_point_to_chart_dict(source_point)

    assert trend["trend_id"] == "trend-0"
    assert trend["render_id"].startswith("trend-0@forming@")
    assert point["point_id"] == source_point.point_id
    assert point["point_type"] == "3buy"
    assert point["status"] == "confirmed"
    assert point["center_ordinal"] == 1


def test_snapshot_revision_is_deterministic_and_window_independent() -> None:
    evidence = _evidence()

    first = build_strict_structure_snapshot(evidence, interval="5m")
    second = build_strict_structure_snapshot(evidence, interval="5m")

    assert first == second
    assert first["structure_revision"] == evidence.structure_revision
    assert first["source_frequency"] == first["display_frequency"] == "5m"
    assert first["schema"] == "chanlun-chart-structure/v4"


def test_v4_snapshot_groups_independent_divergences_by_level() -> None:
    item = DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence/v3",
            PRICE_BASIS,
            1,
            SourceKind.TREND_TYPE.value,
            "trend",
            "up",
            "L1-earlier",
            "L1-later",
        ),
        structural_level=1,
        source_kind=SourceKind.TREND_TYPE,
        price_basis_revision=PRICE_BASIS,
        kind="trend",
        direction="up",
        compare_unit_id="L1-earlier",
        signal_unit_id="L1-later",
        anchor_at=BASE + timedelta(minutes=30),
        anchor_tick=160,
        confirmed_at=BASE + timedelta(minutes=35),
        available_at=BASE + timedelta(minutes=35),
        price_extreme_confirmed=True,
        histogram_area_decayed=True,
        histogram_peak_decayed=True,
        dif_extreme_decayed=True,
        strength_source="macd_native",
    )
    snapshot = build_strict_structure_snapshot(
        _evidence(
            divergences=(item,),
            level_count=2,
            source_frequency="1m",
        ),
        interval="1m",
    )

    assert snapshot["schema"] == "chanlun-chart-structure/v4"
    assert [level["label"] for level in snapshot["levels"]] == ["1m", "5m"]
    assert {level["origin"] for level in snapshot["levels"]} == {
        "current_chart_recursive"
    }
    assert snapshot["levels"][1]["divergences"][0]["kind"] == "trend"


def test_observation_or_approaching_change_updates_render_not_decision_revision() -> None:
    observation = _center(source_kind=SourceKind.STROKE_OBSERVATION)
    approaching = strict_point(
        "3buy",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5),
    )
    before = build_strict_structure_snapshot(_evidence(), interval="5m")
    observation_after = build_strict_structure_snapshot(
        _evidence(observation=observation),
        interval="5m",
    )
    approaching_after = build_strict_structure_snapshot(
        _evidence(approaching_points=(approaching,)),
        interval="5m",
    )

    for after in (observation_after, approaching_after):
        assert before["structure_revision"] == after["structure_revision"]
        assert before["snapshot_revision"] == after["snapshot_revision"]
        assert before["render_revision"] != after["render_revision"]
