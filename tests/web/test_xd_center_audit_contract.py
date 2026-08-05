from tools.audit_xd_center_consistency import _payload_issues


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


def _forming_payload(*, leave=None):
    core = [
        _segment("down", 1, 2, 13, 9),
        _segment("up", 2, 3, 9, 14),
        _segment("down", 3, 4, 14, 10),
    ]
    direction = "zd" if leave is None else leave["direction"]
    return {
        "center_id": "center-1",
        "center_state": "forming",
        "points": [{"time": 1, "price": 12}, {"time": 4, "price": 10}],
        "zd": 10,
        "zg": 12,
        "type": direction,
        "core_line_count": 3,
        "core_directions": ["down", "up", "down"],
        "first_three_component_ids": ["u1", "u2", "u3"],
        "first_three_components": core,
        "entering_segment": None,
        "leaving_segment": leave,
        "completion_phase": (
            "AWAITING_SAME_LEVEL_DEPARTURE"
            if leave is None
            else "AWAITING_SAME_LEVEL_RETURN"
        ),
        "completion_point_type": None,
        "expected_completion_point_type": None if leave is None else "3buy",
        "completion_return_segment": None,
        "linestyle": "1",
        "done": False,
        "provisional": True,
        "tradable": False,
    }


def test_audit_accepts_first_three_center_before_any_departure():
    assert _payload_issues([_forming_payload()]) == []


def test_audit_accepts_pending_departure_without_resurrecting_a_return():
    leave = _segment("up", 4, 5, 10, 14)
    assert _payload_issues([_forming_payload(leave=leave)]) == []


def test_audit_accepts_inclusive_third_buy_return_and_zero_width_core():
    payload = _forming_payload(leave=_segment("up", 4, 5, 10, 14))
    payload.update(
        center_state="completed",
        zd=12,
        zg=12,
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


def test_audit_rejects_duplicate_live_owners_and_synthetic_entry():
    first = _forming_payload()
    second = dict(_forming_payload())
    second["center_id"] = "center-2"
    second["entering_segment"] = second["first_three_components"][0]

    issues = _payload_issues([first, second])

    assert "MULTIPLE_UNRESOLVED_CENTERS" in issues
    assert "CENTER_1_SYNTHETIC_ENTRY_PRESENT" in issues
