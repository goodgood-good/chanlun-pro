"""Native exit references use the same verified sources as formal signals."""
from copy import deepcopy
from decimal import Decimal

import pytest

from chanlun.cl_utils.point_exits import native_point_exit_plans


def signal(identity, kind, anchor, known, price):
    return {"point_id": identity, "point_type": kind, "side": "buy" if kind.endswith("buy") else "sell",
            "anchor_at": anchor, "confirmed_at": known, "available_at": known,
            "anchor_tick": int(price * 100), "anchor_price": price, "status": "confirmed",
            "evidence_scope": "native_center_signal", "formal_point_issued": True,
            "missing_conditions": [], "structural_level": 0, "source_kind": "segment", "price_basis_revision": "basis"}


def plans(*points, valid=None):
    catalog = {p["point_id"]: p for p in points}
    base = {p["point_id"]: {"point_id": p["point_id"], "point_status": "confirmed", "side": p["side"],
                            "source_frequency": "5m", "stop_loss": {"price": 9 if p["side"] == "buy" else 13},
                            "take_profit": {"price": 99, "basis": "terminal_dd"}, "profit_observation": {"price": 99}}
            for p in points}
    before = deepcopy(base)
    result = native_point_exit_plans(catalog, valid or {key: True for key in catalog}, base,
                                     source_closed_at=100, quantum=Decimal("0.01"))
    assert base == before
    return result


@pytest.mark.parametrize("kind", ["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"])
def test_all_six_classes_keep_structural_stop_and_wait_without_a_reverse_signal(kind):
    result = plans(signal("origin", kind, 10, 20, 10))["origin"]
    assert result["stop_loss"]["price"] == (9 if kind.endswith("buy") else 13)
    assert result["take_profit"]["price"] is None
    assert result["take_profit"]["status"] == "waiting"
    assert result["profit_observation"] is None


@pytest.mark.parametrize("buy", [True, False])
def test_first_class_reverse_point_is_usable_and_first_recognized_exit_is_not_cherry_picked(buy):
    origin = signal("origin", "3buy" if buy else "3sell", 10, 20, 10)
    late = signal("late", "1sell" if buy else "1buy", 21, 80, 14 if buy else 6)
    earlier_known = signal("exit", "1sell" if buy else "1buy", 25, 40, 9 if buy else 11)
    after = signal("after", "3sell" if buy else "3buy", 35, 50, 15 if buy else 5)
    target = plans(origin, late, earlier_known, after)["origin"]["take_profit"]
    assert target["point_id"] == "exit"
    assert target["available_at"] == 40
    assert target["price"] == (9 if buy else 11)  # A structural exit need not be profitable.


@pytest.mark.parametrize("fault", ["not_validated", "pre_entry_pivot", "forming", "future", "other_basis", "other_level"])
def test_missing_future_or_different_scope_reverse_evidence_cannot_supply_an_exit(fault):
    origin = signal("origin", "1buy", 10, 20, 10)
    reverse = signal("reverse", "1sell", 30, 40, 12)
    validity = {"origin": True, "reverse": fault != "not_validated"}
    if fault == "pre_entry_pivot":
        reverse["anchor_at"] = 19
    elif fault == "forming":
        reverse["status"] = "approaching"
    elif fault == "future":
        reverse.update(confirmed_at=101, available_at=101)
    elif fault == "other_basis":
        reverse["price_basis_revision"] = "different"
    elif fault == "other_level":
        reverse["structural_level"] = 1
    target = plans(origin, reverse, valid=validity)["origin"]["take_profit"]
    assert target["price"] is None and target["status"] == "waiting"


def test_reverse_reference_uses_authoritative_ticks_instead_of_a_stale_display_price():
    origin = signal("origin", "3buy", 10, 20, 10)
    reverse = signal("reverse", "1sell", 30, 40, 12)
    reverse["anchor_price"] = 999
    assert plans(origin, reverse)["origin"]["take_profit"]["price"] == 12
