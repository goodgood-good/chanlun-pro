from copy import deepcopy

import pytest

from chanlun.cl_utils.point_exits import build_point_exit_plans, with_point_exit_plans


def point(kind="1buy", **kwargs):
    buy = kind.endswith("buy")
    return {"point_id": kind, "point_type": kind, "side": "buy" if buy else "sell",
            "status": "confirmed", "variant": "strict" if kind[0] == "2" else "standard",
            "structural_level": 0, "source_kind": "segment", "price_basis_revision": "raw",
            "anchor_at": 100, "available_at": 120, "confirmed_at": 120,
            "anchor_tick": 900 if buy else 1300,
            "invalidation_tick": (850 if buy else 1350) if kind[0] == "2" else (900 if buy else 1300),
            "center_id": "center", "divergence": {"kind": "trend", "structural_level": 0}, **kwargs}


def center(**kwargs):
    return {"center_id": "center", "structural_level": 0, "source_kind": "segment",
            "price_basis_revision": "raw", "available_at": 80, "established_at": 60,
            "core": {"zd_tick": 1100, "zg_tick": 1150},
            "envelope": {"dd_tick": 1000, "gg_tick": 1200}, **kwargs}


def snapshot(*points, centers=None, cutoff=300, quantum="0.01"):
    return {"symbol": "SH.600088", "source_frequency": "5m", "source_closed_at": cutoff,
            "price_basis_revision": "raw", "structure_price_quantum": quantum,
            "levels": [{"structural_level": 0, "centers": centers if centers is not None else [center()],
                        "points": list(points)}]}


@pytest.mark.parametrize("kind,price,trigger", [
    ("1buy", 9, "lt"), ("1sell", 13, "gt"), ("2buy", 8.5, "lt"), ("2sell", 13.5, "gt"),
    ("3buy", 9, "lte"), ("3sell", 13, "gte"),
])
def test_six_directional_stops_and_boundary_contact(kind, price, trigger):
    plan = build_point_exit_plans(snapshot(point(kind)))[kind]
    assert plan["stop_loss"]["price"] == price
    assert plan["stop_loss"]["trigger"] == trigger
    assert plan["stop_loss"]["derivation"] == "system_structural_invalidation"


@pytest.mark.parametrize("kind,price", [("1buy", 10), ("1sell", 12)])
def test_first_class_uses_terminal_envelope_not_core(kind, price):
    plan = build_point_exit_plans(snapshot(point(kind)))[kind]
    assert plan["take_profit"]["price"] == price
    assert plan["take_profit"]["basis"] in {"terminal_dd", "terminal_gg"}
    assert plan["take_profit"]["status"] == "reference"


@pytest.mark.parametrize("kind", ["2buy", "2sell", "3buy", "3sell"])
def test_no_invented_fixed_profit_target_for_second_or_third(kind):
    tp = build_point_exit_plans(snapshot(point(kind)))[kind]["take_profit"]
    assert tp["price"] is None and tp["status"] == "waiting"


def test_consolidation_and_unknown_center_are_not_first_class_profit_proof():
    p = point(divergence={"kind": "consolidation", "structural_level": 0})
    assert build_point_exit_plans(snapshot(p))[p["point_id"]]["take_profit"]["price"] is None
    assert build_point_exit_plans(snapshot(point(), centers=[]))["1buy"]["take_profit"]["price"] is None


def test_subsequent_exit_requires_later_anchor_known_by_snapshot_and_same_level():
    origin = point("3buy")
    early_anchor = point("1sell", anchor_at=115, available_at=150, confirmed_at=150, point_id="early")
    lower = point("1sell", structural_level=1, anchor_at=130, available_at=140, confirmed_at=140, point_id="other-level")
    later_anchor_earlier_confirmation = point("2sell", anchor_at=170, available_at=180, confirmed_at=180)
    earlier_anchor_later_confirmation = point("1sell", anchor_at=160, available_at=200, confirmed_at=200)
    future = point("3sell", anchor_at=180, available_at=301, confirmed_at=301)
    data = snapshot(origin, early_anchor, lower, later_anchor_earlier_confirmation, earlier_anchor_later_confirmation, future)
    actual = build_point_exit_plans(data)["3buy"]["take_profit"]
    assert actual["point_id"] == "2sell" and actual["available_at"] == 180
    data["source_closed_at"] = 179
    assert build_point_exit_plans(data)["3buy"]["take_profit"]["price"] is None


def test_first_reverse_exit_is_retained_even_if_it_is_below_the_buy_price():
    early = point("1sell", anchor_at=150, available_at=160, confirmed_at=160, anchor_tick=850)
    later = point("2sell", anchor_at=170, available_at=180, confirmed_at=180, anchor_tick=1100)
    tp = build_point_exit_plans(snapshot(point("3buy"), early, later))["3buy"]["take_profit"]
    assert tp["price"] == 8.5 and tp["point_id"] == "1sell"
    assert tp["status"] == "exit_confirmed"


def test_extension_after_original_point_does_not_rewrite_first_observation():
    units = [{"unit_id": str(i), "low_tick": 1000, "high_tick": 1200,
              "end_time": 50 + i * 10, "available_at": 60 + i * 10, "locked": True} for i in range(3)]
    units.append({"unit_id": "future", "low_tick": 800, "high_tick": 1250,
                  "end_time": 180, "available_at": 190, "locked": True})
    evolved = center(available_at=190, envelope={"dd_tick": 800, "gg_tick": 1250},
                     body_unit_ids=[u["unit_id"] for u in units], core_unit_ids=["0", "1", "2"],
                     overlap_components=units)
    assert build_point_exit_plans(snapshot(point(), centers=[evolved]))["1buy"]["take_profit"]["price"] == 10
    # Legacy geometry has no unit availability: fail closed for an evolved center.
    for u in units:
        u.pop("available_at")
    assert build_point_exit_plans(snapshot(point(), centers=[evolved]))["1buy"]["take_profit"]["price"] is None


def test_waiting_point_keeps_provisional_prices_and_does_not_use_later_exit():
    origin = point(status="approaching", confirmed_at=None)
    reverse = point("1sell", anchor_at=150, available_at=180, confirmed_at=180)
    plan = build_point_exit_plans(snapshot(origin, reverse, quantum="0.001"))["1buy"]
    assert plan["stop_loss"]["price"] == 0.9 and plan["stop_loss"]["status"] == "provisional"
    assert plan["take_profit"]["price"] == 1 and plan["take_profit"]["status"] == "provisional"


def test_missing_quantum_or_wrong_side_boundary_does_not_become_zero_price():
    s = snapshot(point(invalidation_tick=910), quantum="NaN")
    plan = build_point_exit_plans(s)["1buy"]
    assert plan["stop_loss"]["price"] is None and plan["stop_loss"]["status"] == "unavailable"
    assert plan["take_profit"]["price"] is None


def test_enrichment_preserves_saved_point_and_center_evidence():
    s = snapshot(point())
    before = deepcopy(s)
    enriched = with_point_exit_plans(s)
    assert s == before
    assert enriched["levels"] == before["levels"]
    assert enriched["point_exit_plans"]["1buy"]["stop_loss"]["price"] == 9
