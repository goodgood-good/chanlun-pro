"""U255 completed lower evidence needs exact parent signal-member lineage."""

from hashlib import sha256
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart import build_center_snapshot
from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.divergence import collect_formal_divergence_ledger
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.nested_divergence_lineage import (
    observe_completed_nested_divergence_lineage,
    observe_nested_candidate_history_lineage,
)
from tests.core.strict_structure.test_divergence_collector import completed_trend_fixture


def _case():
    structure, _, _ = completed_trend_fixture(level=1)
    parent, = collect_formal_divergence_ledger(structure)
    higher = structure.levels[1]
    source = {unit.unit_id: unit for unit in higher.units}
    children = []
    inner = None
    for index, unit_id in enumerate(parent.signal_leg_unit_ids):
        unit = source[unit_id]
        member_ids = (f"child-{index}-compare", f"child-{index}-signal")
        child = SimpleNamespace(
            trend_id=unit_id, market_start=unit.market_start,
            market_end=unit.market_end, start_tick=unit.start_tick,
            end_tick=unit.end_tick, direction=unit.direction,
            price_basis_revision=parent.price_basis_revision,
            available_at=unit.available_at, complete=True,
            constituent_units=tuple(SimpleNamespace(
                unit_id=name, source_kind=SourceKind.SEGMENT)
                for name in member_ids),
            centers=(SimpleNamespace(center_id=f"child-center-{index}"),),
            terminal_divergence=None,
        )
        children.append(child)
        if unit.direction == parent.direction:
            inner = SimpleNamespace(
                divergence_id="inner-completed", structural_level=0,
                kind="consolidation", direction=parent.direction,
                source_kind=SourceKind.SEGMENT,
                price_basis_revision=parent.price_basis_revision,
                compare_leg_unit_ids=(member_ids[0],),
                signal_leg_unit_ids=(member_ids[1],),
                anchor_at=unit.market_end, available_at=unit.available_at,
                is_divergent=True,
            )
    assert inner is not None
    lower = SimpleNamespace(structural_level=0, trend_types=tuple(children),
                            completed_trends=())
    return lower, higher, parent, inner


def _candidate_state(
    child, parent, known_at, status, *, center_id=None, tail_id=None,
    tail_market_end=None,
):
    center_id = center_id or child.centers[0].center_id
    compare_id = child.constituent_units[0].unit_id
    tail_id = tail_id or child.constituent_units[-1].unit_id
    signal_start = int(child.market_start.timestamp())
    basis = parent.price_basis_revision
    direction = parent.direction
    return {
        "rule_id": "U255",
        "observation_kind": "one_center_open_departure",
        "candidate_id": stable_structure_id(
            "u255-open-departure-comparison-v1", center_id, tail_id,
            (compare_id,), (tail_id,), basis,
        ),
        "hypothesis_id": stable_structure_id(
            "u255-live-comparison-hypothesis-v1",
            "one_center_open_departure", center_id, (compare_id,),
            signal_start, direction, 0, "segment", basis,
        ),
        "status": status,
        "center_id": center_id,
        "structural_level": 0,
        "source_kind": "segment",
        "price_basis_revision": basis,
        "direction": direction,
        "compare_leg_unit_ids": [compare_id],
        "signal_leg_unit_ids": [tail_id],
        "signal_leg_market_start": signal_start,
        "open_tail_unit_id": tail_id,
        "open_tail_market_end": int((tail_market_end or child.market_end).timestamp()),
        "as_of": int(known_at.timestamp()),
        "known_at": int(known_at.timestamp()),
        "tail_locked_in_source": False,
        "projected_lock_only_for_comparison": True,
        "price_extreme_confirmed": True,
        "temporary_divergence_metrics": {
            "histogram_area_decayed": status == "temporary_weakening_candidate",
            "histogram_peak_decayed": False,
            "dif_extreme_decayed": False,
        },
        "macd_strength_relation": {
            "histogram_area": "stronger",
            "histogram_peak": "equal",
            "dif_extreme": "equal",
        } if status == "macd_strength_exceeds_reference" else None,
        "formal_divergence_certified": False,
        "author_strength_refutation_certified": False,
        "full_recursive_interval_nesting_certified": False,
        "formal_point_from_observation": False,
        "tradable": False,
    }


def test_exact_completed_inner_divergence_is_observed_without_trade_promotion():
    lower, higher, parent, inner = _case()
    state, = observe_completed_nested_divergence_lineage(
        (lower, higher), (inner, parent), as_of=parent.available_at,
    )
    assert state["status"] == "exact_completed_child_divergence_facts"
    assert state["missing_child_type_ids"] == []
    assert [item["child_divergence_id"] for item in state["exact_child_divergences"]] == [
        inner.divergence_id,
    ]
    assert state["known_at"] == int(parent.available_at.timestamp())
    assert not state["full_recursive_interval_nesting_certified"]
    assert not state["live_candidate_refutation_certified"]
    assert not state["formal_point_from_this_observation"]
    assert not state["tradable"]


def test_live_child_history_links_only_inside_the_exact_parent_signal_member():
    lower, higher, parent, _inner = _case()
    child = next(
        item for item in lower.trend_types
        if item.trend_id == parent.signal_leg_unit_ids[-1]
    )
    first_at = parent.anchor_at - timedelta(minutes=3)
    later_at = parent.anchor_at - timedelta(minutes=1)
    tail_end = parent.anchor_at - timedelta(minutes=4)
    first = _candidate_state(
        child, parent, first_at, "temporary_weakening_candidate",
        tail_market_end=tail_end,
    )
    later = _candidate_state(
        child, parent, later_at, "macd_strength_exceeds_reference",
        tail_market_end=tail_end,
    )
    rows = ((first["as_of"], (first,)), (later["as_of"], (later,)))
    observed, = observe_nested_candidate_history_lineage(
        (lower, higher), (parent,), rows, as_of=parent.available_at,
    )
    assert observed["status"] == "exact_candidate_history_linked_to_parent_signal_member"
    assert observed["sampled_cutoff_count"] == 2
    assert observed["exact_candidate_history_count"] == 1
    assert observed["temporal_only_candidate_history_count"] == 0
    assert observed["turn_time_weakening_history_count"] == 1
    linked, = observed["linked_candidate_histories"]
    assert linked["child_trend_id"] == child.trend_id
    assert linked["source_lineage_scope"] == "exact_final_child_source_unit_ids"
    assert linked["turn_anchor_knowledge"] == "known_at_or_before_parent_turn"
    history = linked["candidate_history"]
    assert history["first_weakening_known_at"] == first["known_at"]
    assert history["first_macd_proxy_exceeds_known_at"] == later["known_at"]
    assert history["time_ordered_macd_proxy_increase_observed"] is True
    assert not observed["full_child_interval_replay_certified"]
    assert not observed["author_strength_refutation_certified"]
    assert not observed["full_recursive_interval_nesting_certified"]
    assert not observed["tradable"]

    borrowed = _candidate_state(
        child, parent, first_at, "temporary_weakening_candidate",
        center_id="borrowed-center", tail_market_end=tail_end,
    )
    unrelated, = observe_nested_candidate_history_lineage(
        (lower, higher), (parent,), ((borrowed["as_of"], (borrowed,)),),
        as_of=parent.available_at,
    )
    assert unrelated["status"] == "no_linked_candidate_in_supplied_snapshots"
    assert unrelated["linked_candidate_histories"] == []

    versioned = _candidate_state(
        child, parent, first_at, "temporary_weakening_candidate",
        tail_id="earlier-open-tail-version", tail_market_end=tail_end,
    )
    temporal, = observe_nested_candidate_history_lineage(
        (lower, higher), (parent,), ((versioned["as_of"], (versioned,)),),
        as_of=parent.available_at,
    )
    assert temporal["status"] == "candidate_history_temporally_associated_only"
    assert temporal["exact_candidate_history_count"] == 0
    assert temporal["temporal_only_candidate_history_count"] == 1
    assert temporal["linked_candidate_histories"][0]["source_lineage_scope"] == (
        "same_center_and_contained_tail_clock_only"
    )

    retrospective = _candidate_state(
        child, parent, parent.available_at - timedelta(minutes=4),
        "temporary_weakening_candidate",
    )
    after_turn, = observe_nested_candidate_history_lineage(
        (lower, higher), (parent,),
        ((retrospective["as_of"], (retrospective,)),),
        as_of=parent.available_at,
    )
    assert after_turn["status"] == "candidate_history_known_after_parent_turn_only"
    assert after_turn["turn_time_weakening_history_count"] == 0
    assert after_turn["post_turn_known_weakening_history_count"] == 1
    assert after_turn["linked_candidate_histories"][0]["turn_anchor_knowledge"] == (
        "known_after_parent_turn"
    )

    parent_fields = (
        "divergence_id", "structural_level", "source_kind",
        "signal_leg_unit_ids", "price_basis_revision", "direction",
        "kind", "available_at", "is_divergent", "anchor_at",
    )
    earlier_parent = SimpleNamespace(**{
        name: getattr(parent, name) for name in parent_fields
    })
    earlier_parent.anchor_at = child.market_end - timedelta(minutes=1)
    late, = observe_nested_candidate_history_lineage(
        (lower, higher), (earlier_parent,),
        ((retrospective["as_of"], (retrospective,)),),
        as_of=parent.available_at,
    )
    assert late["status"] == "candidate_after_parent_anchor_only"
    assert late["late_candidate_version_ids"] == [retrospective["candidate_id"]]
    assert late["linked_candidate_histories"] == []

    with pytest.raises(ValueError, match="future snapshots"):
        observe_nested_candidate_history_lineage(
            (lower, higher), (parent,),
            ((int(parent.available_at.timestamp()) + 60, ()),),
            as_of=parent.available_at,
        )


def test_borrowed_or_future_lower_divergence_cannot_complete_the_link():
    lower, higher, parent, inner = _case()
    borrowed = SimpleNamespace(**{**vars(inner),
                                  "divergence_id": "borrowed",
                                  "signal_leg_unit_ids": ("outside-child",)})
    future = SimpleNamespace(**{**vars(inner),
                                "divergence_id": "future",
                                "available_at": parent.available_at + timedelta(minutes=1)})
    state, = observe_completed_nested_divergence_lineage(
        (lower, higher), (parent, borrowed, future), as_of=parent.available_at,
    )
    assert state["status"] == "no_completed_child_divergence_in_parent_signal_members"
    assert state["exact_child_divergences"] == []


def test_child_divergence_after_parent_turn_cannot_explain_the_earlier_anchor():
    # L061:151-166 locates the nested lower turn at the parent's turning
    # point.  A parent's later confirmation clock cannot admit a child event
    # whose market anchor itself occurs after that turn.
    lower, higher, parent, inner = _case()
    parent_fields = (
        "divergence_id", "structural_level", "source_kind",
        "signal_leg_unit_ids", "price_basis_revision", "direction",
        "kind", "available_at", "is_divergent", "anchor_at",
    )
    earlier_parent = SimpleNamespace(**{
        name: getattr(parent, name) for name in parent_fields
    })
    earlier_parent.anchor_at = inner.anchor_at - timedelta(minutes=1)
    assert inner.anchor_at > earlier_parent.anchor_at
    state, = observe_completed_nested_divergence_lineage(
        (lower, higher), (inner, earlier_parent), as_of=parent.available_at,
    )
    assert state["status"] == "child_divergence_after_parent_anchor_only"
    assert state["exact_child_divergences"] == []
    assert [item["child_divergence_id"] for item in state["late_child_divergences"]] == [
        inner.divergence_id,
    ]
    assert not state["full_recursive_interval_nesting_certified"]


def test_missing_exact_child_type_is_pending_not_a_completed_nest():
    lower, higher, parent, inner = _case()
    lower.trend_types = lower.trend_types[:1]
    state, = observe_completed_nested_divergence_lineage(
        (lower, higher), (parent, inner), as_of=parent.available_at,
    )
    assert state["status"] == "pending_exact_child_type_lineage"
    assert state["missing_child_type_ids"]
    assert not state["full_recursive_interval_nesting_certified"]


def test_recorded_prefix_does_not_invent_parent_divergence_after_center_rollback():
    path = (Path(__file__).parents[2] / "fixtures/segment_nested"
            / "SZ.300069_1m_parent_divergence_20251230.parquet")
    manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert sha256(path.read_bytes()).hexdigest() == manifest["parquet_sha256"]
    frame = pd.read_parquet(path)
    assert len(frame) == manifest["rows"] == 33531
    frame.attrs.update(
        structure_price_quantum=manifest["structure_price_quantum"],
        price_basis_revision=manifest["price_basis_revision"],
    )
    for prefix in (frame.iloc[:-1], frame):
        source = prefix.copy()
        source.attrs.update(frame.attrs)
        runtime = build_strict_chart_cd(
            market="a", code="SZ.300069", frequency="1m",
            frame=source, last_bar_closed=True,
        )
        assert runtime.cd is not None
        cd = runtime.cd
        evidence = cd.get_strict_evidence()
        assert evidence.divergences == ()
        observations = observe_completed_nested_divergence_lineage(
            evidence.structure.levels, evidence.divergences,
            as_of=cd._strict_as_of(),
        )
        snapshot = build_center_snapshot(
            cd, interval="1m",
            display_bar_closed_at=(int(cd._strict_as_of().timestamp()),),
        )
        assert observations == ()
        assert snapshot["completed_nested_divergence_observations"] == []
        assert snapshot["nested_candidate_history_lineage_observations"] == []
