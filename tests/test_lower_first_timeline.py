"""U295 lower-first evidence never upgrades a lone 1m point to interval-suite entry."""

from copy import deepcopy

import pytest

from chanlun.screening.lower_first_timeline import (
    observe_independent_lower_structural_witnesses,
    observe_lower_first_structural_timing,
)


def _case():
    main_point = {
        "point_id": "main", "point_type": "3buy", "side": "buy",
        "status": "confirmed", "source_kind": "segment",
        "structural_level": 0, "price_basis_revision": "basis",
        "anchor_unit_id": "main-unit", "anchor_at": 900,
        "anchor_tick": 120, "confirmed_at": 1500,
        "available_at": 1500, "center_id": "main-center",
        "evidence_codes": ["formal_center"], "missing_conditions": [],
    }
    lower_point = {
        **main_point, "point_id": "lower", "anchor_unit_id": "lower-unit",
        "anchor_at": 840, "anchor_tick": 118,
        "confirmed_at": 1000, "available_at": 1000,
        "center_id": "lower-center",
    }
    main = {
        "symbol": "SH.600000", "source_frequency": "5m",
        "source_closed_at": 2000, "price_basis_revision": "basis",
        "structure_price_quantum": "0.01",
        "levels": [{"points": [main_point]}],
    }
    lower = {
        **main, "source_frequency": "1m",
        "levels": [{"points": [lower_point]}],
    }
    lower_prefix = {
        **lower, "source_closed_at": 1000,
        "levels": [{"points": [deepcopy(lower_point)]}],
    }
    main_prefix = {
        **main, "source_closed_at": 900,
        "levels": [{"points": []}],
    }
    interval = {"start_after": 800, "end_at": 900}
    return main, lower, lower_prefix, main_prefix, interval


def test_replayed_lower_point_before_missing_main_is_only_a_structural_prerequisite():
    main, lower, lower_prefix, main_prefix, interval = _case()
    state = observe_lower_first_structural_timing(
        main, lower, main_point_id="main", lower_point_id="lower",
        lower_prefix_snapshot=lower_prefix, main_prefix_snapshot=main_prefix,
        association_interval=interval,
    )
    assert state["status"] == "lower_structural_point_before_parent"
    assert state["lower_point_known_at"] == 1000
    assert state["main_point_known_at"] == 1500
    assert state["association_basis"] == "declared_parent_interval"
    assert state["lower_prefix_replayed"] is True
    assert state["main_absent_at_lower_clock"] is True
    assert state["selection_strategy"] == "5m_with_1m_confirmation"
    assert state["independent_lower_first_selection_proven"] is False
    assert state["full_interval_nesting_certified"] is False
    assert state["author_interval_entry_certified"] is False
    assert state["parent_critical_price_followup_certified"] is False
    assert state["tradable"] is False

    pending = observe_lower_first_structural_timing(
        main, lower, main_point_id="main", lower_point_id="lower",
        association_interval=interval,
    )
    assert pending["status"] == "pending_causal_prefix_replay"
    assert pending["lower_prefix_replayed"] is False


def test_replay_rejects_future_or_borrowed_parent_and_lower_point_proofs():
    main, lower, lower_prefix, main_prefix, interval = _case()
    already = deepcopy(main_prefix)
    already["levels"][0]["points"] = [main["levels"][0]["points"][0]]
    state = observe_lower_first_structural_timing(
        main, lower, main_point_id="main", lower_point_id="lower",
        lower_prefix_snapshot=lower_prefix, main_prefix_snapshot=already,
        association_interval=interval,
    )
    assert state["status"] == "parent_point_already_present_at_lower_clock"

    borrowed = deepcopy(lower_prefix)
    borrowed["levels"][0]["points"][0]["anchor_tick"] += 1
    state = observe_lower_first_structural_timing(
        main, lower, main_point_id="main", lower_point_id="lower",
        lower_prefix_snapshot=borrowed, main_prefix_snapshot=main_prefix,
        association_interval=interval,
    )
    assert state["status"] == "lower_prefix_proof_mismatch"

    future = deepcopy(lower_prefix)
    future["source_closed_at"] = 1100
    with pytest.raises(ValueError, match="prefix"):
        observe_lower_first_structural_timing(
            main, lower, main_point_id="main", lower_point_id="lower",
            lower_prefix_snapshot=future, main_prefix_snapshot=main_prefix,
            association_interval=interval,
        )


def test_unrelated_price_basis_or_interval_cannot_form_a_lower_first_pair():
    main, lower, lower_prefix, main_prefix, interval = _case()
    unrelated = deepcopy(lower)
    unrelated["price_basis_revision"] = "other"
    with pytest.raises(ValueError, match="identity"):
        observe_lower_first_structural_timing(
            main, unrelated, main_point_id="main", lower_point_id="lower",
            association_interval=interval,
        )
    with pytest.raises(ValueError, match="association"):
        observe_lower_first_structural_timing(
            main, lower, main_point_id="main", lower_point_id="lower",
            association_interval={"start_after": 850, "end_at": 900},
        )
    seller_main, seller_lower = deepcopy(main), deepcopy(lower)
    for point in (seller_main["levels"][0]["points"][0],
                  seller_lower["levels"][0]["points"][0]):
        point["side"] = "sell"
        point["point_type"] = "3sell"
    with pytest.raises(ValueError, match="buy"):
        observe_lower_first_structural_timing(
            seller_main, seller_lower,
            main_point_id="main", lower_point_id="lower",
            association_interval=interval,
        )


def test_independent_one_minute_ledger_keeps_historical_point_without_parent_claim():
    _main, lower, _lower_prefix, _main_prefix, _interval = _case()
    witness, = observe_independent_lower_structural_witnesses(
        lower, known_from=900,
    )
    assert witness["status"] == "lower_formal_buy_point_only"
    assert witness["point_id"] == "lower"
    assert witness["known_at"] == 1000
    assert witness["historical_structural_witness"] is True
    assert witness["independent_lower_point_source"] is True
    assert witness["independent_lower_first_selection_proven"] is False
    assert witness["full_interval_nesting_certified"] is False
    assert witness["author_interval_entry_certified"] is False
    assert witness["parent_critical_price_followup_certified"] is False
    assert witness["current_selection_eligible"] is None
    assert witness["tradable"] is False
    assert observe_independent_lower_structural_witnesses(
        lower, known_from=1100,
    ) == ()
