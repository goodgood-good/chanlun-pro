from copy import deepcopy
import pandas as pd

from cl_app.services.holding_risk import advance


def reference():
    return {
        "market": "a",
        "code": "SH.600000",
        "name": "fixture",
        "frequency": "5m",
        "binding_id": "b" * 32,
        "signal_id": "entry",
        "point_id": "point",
        "point_type": "3buy",
        "anchor_price": 10.5,
        "stop_price": 10.0,
        "stop_loss": {"price_tick": 1000},
        "structure_price_quantum": "0.01",
        "price_basis_revision": "raw",
        "available_at": 50,
        "bound_cutoff": 100,
        "checked_cutoff": 100,
        "active": True,
        "status": "active",
    }


def frame(lows=(10.1, 9.8, 10.2)):
    return pd.DataFrame(
        {
            "date": pd.to_datetime([100, 200, 300], unit="s", utc=True),
            "low": lows,
            "close": [10.5, 10.3, 10.6],
        }
    )


def snapshot(**extra):
    return {
        "source_closed_at": 300,
        "price_basis_revision": "raw",
        "structure_price_quantum": "0.01",
        **extra,
    }


def test_bound_stop_keeps_tracking_after_window_exit_and_detects_intrabar_breach_once():
    plan = reference()
    original = deepcopy(plan)
    updated, events = advance(plan, frame(), snapshot(), {}, {}, set(), 310)
    assert plan == original and updated["status"] == "stop_triggered"
    assert updated["window_exited_at"] == 300 and updated["checked_cutoff"] == 300
    assert (
        len(events) == 1
        and events[0]["available_at"] == 200
        and events[0]["latest_close"] > events[0]["stop_price"]
    )
    assert (
        events[0]["trigger_price"] == 9.8 and events[0]["signal_type"] == "holding_risk"
    )
    assert advance(updated, frame(), snapshot(), {}, {}, set(), 320)[1] == []


def test_touching_stop_does_not_cross_it_and_basis_change_never_creates_a_false_trigger():
    plan = reference()
    assert (
        advance(plan, frame((10.1, 10.0, 10.2)), snapshot(), {}, {}, set(), 310)[1]
        == []
    )
    changed, events = advance(
        plan, frame(), snapshot(price_basis_revision="adjusted"), {}, {}, set(), 310
    )
    assert (
        changed["status"] == "requires_review"
        and not events
        and changed["checked_cutoff"] == 100
    )


def test_reverse_notification_uses_later_confirmed_evidence_and_ignores_unverified_points():
    plan = reference()
    points = {
        "early": {
            "point_id": "early",
            "side": "sell",
            "status": "confirmed",
            "anchor_at": 40,
            "available_at": 150,
            "anchor_price": 12,
            "point_type": "1sell",
        },
        "new": {
            "point_id": "new",
            "side": "sell",
            "status": "confirmed",
            "anchor_at": 150,
            "available_at": 250,
            "anchor_price": 12,
            "point_type": "3sell",
        },
    }
    _, events = advance(
        plan,
        frame((10.1, 10.2, 10.3)),
        snapshot(),
        points,
        {"early": True, "new": False},
        {"entry"},
        310,
    )
    assert not events
    updated, events = advance(
        plan,
        frame((10.1, 10.2, 10.3)),
        snapshot(),
        points,
        {"early": True, "new": True},
        {"entry"},
        310,
    )
    assert (
        updated["status"] == "reverse_confirmed"
        and events[0]["point_type"] == "risk_reverse"
    )
    assert (
        events[0]["available_at"] == 250 and events[0]["reverse_point_type"] == "3sell"
    )


def test_a_finer_bound_stop_is_not_rounded_to_a_new_coarser_price_grid():
    plan = reference()
    plan.update(
        stop_price=10.005,
        structure_price_quantum="0.001",
        stop_loss={"price_tick": 10005},
    )
    _, events = advance(plan, frame((10.1, 10.0, 10.2)), snapshot(), {}, {}, set(), 310)
    assert len(events) == 1 and events[0]["stop_price"] == 10.005


def test_corrected_closed_bars_at_the_same_cutoff_are_rechecked():
    plan, _ = advance(
        reference(), frame((10.1, 10.2, 10.3)), snapshot(), {}, {}, {"entry"}, 310
    )
    corrected, events = advance(
        plan, frame((10.1, 9.8, 10.3)), snapshot(), {}, {}, {"entry"}, 320
    )
    assert corrected["stop_triggered_at"] == 200 and len(events) == 1


def test_recovered_reverse_proof_at_the_same_cutoff_is_not_a_cached_price_check():
    points = {"reverse": {"point_id": "reverse", "side": "sell", "status": "confirmed",
                          "anchor_at": 150, "available_at": 250, "anchor_price": 12,
                          "point_type": "3sell"}}
    candles = frame((10.1, 10.2, 10.3))
    first, events = advance(reference(), candles, snapshot(), points, {"reverse": False}, {"entry"}, 310)
    assert not events
    updated, events = advance(first, candles, snapshot(), points, {"reverse": True}, {"entry"}, 320)
    assert updated['reverse_point_id'] == 'reverse' and len(events) == 1
    assert advance(updated, candles, snapshot(), points, {"reverse": True}, {"entry"}, 330)[1] == []
