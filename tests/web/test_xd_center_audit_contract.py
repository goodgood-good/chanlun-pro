from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from chanlun.core.strict_structure.models import CenterState
from tools.audit_xd_center_consistency import (
    _display_completed_center_recall_issues,
    _payload_issues,
    _terminal_projection_rewrite_counts,
)


def _segment(
    direction: str,
    start: int,
    end: int,
    start_price: float,
    end_price: float,
):
    return {
        "direction": direction,
        "start_time": start,
        "end_time": end,
        "start_price": start_price,
        "end_price": end_price,
    }


def _forming_payload():
    entry = _segment("up", 0, 1, 8, 13)
    core = [
        _segment("down", 1, 2, 13, 9),
        _segment("up", 2, 3, 9, 12),
        _segment("down", 3, 4, 12, 10),
    ]
    leave = _segment("up", 4, 5, 10, 14)
    establishment = [entry, *core, leave]
    return {
        "center_id": "center-1",
        "center_state": "forming",
        "points": [
            {"time": 1, "price": 12},
            {"time": 4, "price": 10},
        ],
        "display_range": {
            "start_role": "middle_three_first_start",
            "end_role": "middle_three_last_end",
            "includes_entry": False,
            "includes_leave": False,
            "price_core_source": "middle_three_intersection",
        },
        "zd": 10,
        "zg": 12,
        "type": "up",
        "entry_role": "external_entry",
        "core_component_count": 3,
        "minimum_lifecycle_role_count": 5,
        "lifecycle_role_count": 5,
        "overlap_component_count": 5,
        "establishment_component_count": 5,
        "core_line_count": 3,
        "core_directions": ["down", "up", "down"],
        "first_three_component_ids": ["u1", "u2", "u3"],
        "establishment_segment_ids": ["u0", "u1", "u2", "u3", "u4"],
        "first_three_components": core,
        "body_components": core,
        "establishment_segments": establishment,
        "overlap_components": establishment,
        "entering_segment": entry,
        "leaving_segment": leave,
        "completion_phase": "AWAITING_SAME_LEVEL_RETURN",
        "completion_point_type": None,
        "expected_completion_point_type": "3buy",
        "completion_return_segment": None,
        "linestyle": "1",
        "done": False,
        "provisional": True,
        "tradable": False,
    }


def test_audit_accepts_exact_five_segment_center_with_pending_leave():
    assert _payload_issues([_forming_payload()]) == []


def test_audit_accepts_four_component_immature_preview():
    payload = _forming_payload()
    payload.update(
        type="zd",
        lifecycle_role_count=4,
        overlap_component_count=4,
        establishment_component_count=4,
        establishment_segment_ids=payload["establishment_segment_ids"][:4],
        establishment_segments=payload["establishment_segments"][:4],
        overlap_components=payload["overlap_components"][:4],
        leaving_segment=None,
        completion_phase="AWAITING_MATURITY_SEGMENT",
        expected_completion_point_type=None,
        establishment_unit_id=None,
    )

    assert _payload_issues([payload]) == []


def test_audit_accepts_fifth_segment_as_first_extension():
    payload = _forming_payload()
    maturity = _segment("up", 4, 5, 10, 11)
    payload.update(
        points=[
            {"time": 1, "price": 12},
            {"time": 5, "price": 10},
        ],
        display_range={
            "start_role": "middle_three_first_start",
            "end_role": "body_tail_end",
            "includes_entry": False,
            "includes_leave": False,
            "price_core_source": "middle_three_intersection",
        },
        type="zd",
        body_components=[*payload["first_three_components"], maturity],
        establishment_segments=[
            payload["entering_segment"],
            *payload["first_three_components"],
            maturity,
        ],
        overlap_components=[
            payload["entering_segment"],
            *payload["first_three_components"],
            maturity,
        ],
        leaving_segment=None,
        completion_phase="AWAITING_SAME_LEVEL_DEPARTURE",
        expected_completion_point_type=None,
    )

    assert _payload_issues([payload]) == []


def test_audit_accepts_return_crossing_as_opposite_new_leave():
    payload = _forming_payload()
    initial_leave = payload["establishment_segments"][-1]
    opposite_leave = _segment("down", 5, 6, 14, 8)
    payload.update(
        points=[
            {"time": 1, "price": 12},
            {"time": 5, "price": 10},
        ],
        display_range={
            "start_role": "middle_three_first_start",
            "end_role": "body_tail_end",
            "includes_entry": False,
            "includes_leave": False,
            "price_core_source": "middle_three_intersection",
        },
        type="down",
        lifecycle_role_count=6,
        overlap_component_count=6,
        body_components=[*payload["first_three_components"], initial_leave],
        overlap_components=[*payload["establishment_segments"], opposite_leave],
        leaving_segment=opposite_leave,
        expected_completion_point_type="3sell",
    )

    assert _payload_issues([payload]) == []


def test_audit_accepts_inclusive_third_buy_return():
    payload = _forming_payload()
    payload.update(
        center_state="completed",
        completion_phase="FORMAL_THIRD_CLASS_POINT",
        completion_point_type="3buy",
        expected_completion_point_type="3buy",
        completion_point_status="confirmed",
        completion_return_segment=_segment("down", 5, 6, 14, 12),
        linestyle="0",
        done=True,
        provisional=False,
        tradable=True,
    )

    assert _payload_issues([payload]) == []


def test_audit_rejects_zero_width_line_center():
    payload = _forming_payload()
    payload.update(zd=12, zg=12)

    assert "CENTER_0_GEOMETRY_INVALID" in _payload_issues([payload])


def test_audit_rejects_duplicate_live_owners_and_invalid_external_entry():
    first = _forming_payload()
    second = dict(_forming_payload())
    second["center_id"] = "center-2"
    second["entering_segment"] = second["first_three_components"][0]

    issues = _payload_issues([first, second])

    assert "MULTIPLE_UNRESOLVED_CENTERS" in issues
    assert "CENTER_1_EXTERNAL_ENTRY_INVALID" in issues


def test_audit_rejects_completed_center_removed_by_display_reducer():
    base = datetime(2026, 8, 6, tzinfo=timezone(timedelta(hours=8)))

    def center(center_id, state, offset):
        return SimpleNamespace(
            center_id=center_id,
            state=state,
            entry_unit=SimpleNamespace(
                direction="up",
                market_start=base + timedelta(minutes=offset),
                market_end=base + timedelta(minutes=offset + 1),
            ),
        )

    result = SimpleNamespace(
        centers=(
            center("kept", CenterState.COMPLETED, 0),
            center("missing", CenterState.COMPLETED, 5),
            center("live", CenterState.ONGOING, 10),
        )
    )

    assert _display_completed_center_recall_issues(
        result,
        [
            {
                "center_id": "display-basis-specific-id",
                "entering_segment": {
                    "direction": "up",
                    "start_time": int(base.timestamp()),
                    "end_time": int((base + timedelta(minutes=1)).timestamp()),
                },
            }
        ],
    ) == ["DISPLAY_COMPLETED_CENTER_MISSING_missing"]


def test_audit_classifies_terminal_rewrites_by_structural_kind():
    assert _terminal_projection_rewrite_counts(
        (
            "BAR_PREFIX_10_COMPLETED_CENTER_REWRITTEN_L0_c1",
            "BAR_PREFIX_10_COMPLETED_CENTER_REWRITTEN_L1_c2",
            "BAR_PREFIX_10_COMPLETED_TREND_REWRITTEN_L0_t1",
            "BAR_PREFIX_10_CONFIRMED_POINT_REWRITTEN_p1",
            "BAR_PREFIX_10_DIVERGENCE_REWRITTEN_d1",
            "BAR_PREFIX_10_NINE_SEGMENT_UPGRADE_REWRITTEN_u1",
            "BAR_PREFIX_10_LEVEL_2_MISSING",
            "BAR_PREFIX_10_UNKNOWN_REWRITE",
        )
    ) == {
        "completed_center": 2,
        "completed_trend": 1,
        "confirmed_point": 1,
        "divergence": 1,
        "nine_segment_upgrade": 1,
        "other": 1,
        "recursive_level": 1,
    }
