"""Lesson-54 associative hypotheses stay separate in the public chart snapshot."""

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from chanlun.cl_utils.strict_chart import _official_adjacent_a_share_closes, build_center_snapshot
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import SourceKind
from tests.core.strict_structure import helpers as strict_helpers
from tests.core.strict_structure.helpers import completed_up_center
from tests.core.strict_structure.test_general_decomposition import _l054_segments


def _snapshot(prefix):
    as_of = prefix[-1].available_at
    level = SimpleNamespace(
        structural_level=0, units=prefix,
        center_result=SimpleNamespace(centers=(), previews=()),
        trend_types=(), completed_trends=(), decomposition_boundaries=(),
        decomposition_mode="same_level",
    )
    evidence = SimpleNamespace(
        structure=SimpleNamespace(levels=(level,)),
        confirmed_points=(), approaching_points=(), divergences=(),
        stroke_center_observations=SimpleNamespace(centers=()),
    )
    cd = SimpleNamespace(
        get_frequency=lambda: "1m", get_code=lambda: "L054",
        get_src_klines=lambda: (SimpleNamespace(date=as_of),),
        _strict_as_of=lambda: as_of,
        _strict_price_quantum=lambda: Decimal("0.01"),
        _strict_price_basis_revision=lambda: prefix[0].price_basis_revision,
        _strict_config_revision=lambda: "l054-probe",
        get_strict_evidence=lambda: evidence,
    )
    return build_center_snapshot(
        cd, interval="1m", display_bar_closed_at=(int(as_of.timestamp()),)
    )


def test_chart_only_publishes_direct_gap_from_matching_adjacent_source_bars(monkeypatch):
    monkeypatch.setattr(strict_helpers, "BASE", strict_helpers.BASE - timedelta(hours=8))
    previous = completed_up_center()
    current = completed_up_center(7, zd_tick=145, zg_tick=155)
    distant = completed_up_center(10, zd_tick=145, zg_tick=155)
    left = previous.completion_return_unit
    right = current.entry_unit or current.body_units[0]
    cutoff = max(previous.available_at, distant.available_at) + timedelta(minutes=5)
    before = SimpleNamespace(date=left.market_end, l=1.15, h=1.25)
    after = SimpleNamespace(date=right.market_start, l=1.30, h=1.40)
    tail = SimpleNamespace(date=cutoff, l=1.35, h=1.45)
    level = SimpleNamespace(
        structural_level=0, units=(),
        center_result=SimpleNamespace(centers=(previous, current), previews=()),
        trend_types=(), completed_trends=(), decomposition_boundaries=(),
        decomposition_mode="general_declared",
    )
    evidence = SimpleNamespace(
        structure=SimpleNamespace(levels=(level,)),
        confirmed_points=(), approaching_points=(), divergences=(),
        stroke_center_observations=SimpleNamespace(centers=()),
    )

    def chart(bars, display=None, market="a"):
        cd = SimpleNamespace(
            market=market, get_frequency=lambda: "5m", get_code=lambda: "gap-probe",
            get_src_klines=lambda: bars, _strict_as_of=lambda: cutoff,
            _strict_price_quantum=lambda: Decimal("0.01"),
            _strict_price_basis_revision=lambda: previous.price_basis_revision,
            _strict_config_revision=lambda: "gap-probe", get_strict_evidence=lambda: evidence,
        )
        labels = tuple(int(bar.date.timestamp()) for bar in bars) if display is None else display
        return build_center_snapshot(cd, interval="5m", display_bar_closed_at=labels)

    matched = chart((before, after, tail))["levels"][0]
    observed = matched["general_center_connector_observations"]
    gap, = observed
    assert matched["general_center_connector_review"] == {
        "status": "direct_gap_observed", "candidate_boundary_pairs": 1,
        "calendar_verified_pairs": 1, "calendar_unverified_pairs": 0,
        "invalid_bar_geometry_pairs": 0, "direct_gap_observations": 1,
        "tradable": False,
    }
    assert gap["connector_kind"] == "direct_price_gap"
    assert gap["source_adjacency_scope"] == "adjacent_in_matching_chart_source_bars"
    assert gap["calendar_adjacency_scope"] == "official_a_share_next_closed_bar"
    assert gap["calendar_evidence_ids"]
    assert gap["source_bar_count"] == 3
    gap_state = gap["u297_gap_strength_state"]
    assert gap_state["rule_id"] == "U297"
    assert gap_state["status"] == "pending_transaction_coverage"
    assert gap_state["physical_frequency"] == "5m"
    assert gap_state["gap_lower_tick"] == 125
    assert gap_state["gap_upper_tick"] == 130
    assert gap_state["full_transaction_fill_certified"] is False
    assert gap_state["tradable"] is False
    assert not gap["tradable"] and not gap["formal_parent_type_certified"]
    assert not gap["higher_center_certified"] and not gap["third_point_certified"]

    overlap = SimpleNamespace(date=after.date, l=1.25, h=1.40)
    no_gap = chart((before, overlap, tail))["levels"][0]
    assert no_gap["general_center_connector_observations"] == []
    assert no_gap["general_center_connector_review"]["status"] == "verified_no_direct_gap"
    hidden = SimpleNamespace(date=before.date + timedelta(minutes=1), l=1.26, h=1.28)
    hidden_result = chart((before, hidden, after, tail))["levels"][0]
    assert hidden_result["general_center_connector_observations"] == []
    assert hidden_result["general_center_connector_review"]["status"] == "no_direct_boundary_pair"
    incomplete_display = (int(before.date.timestamp()), int(tail.date.timestamp()))
    mismatched = chart((before, after, tail), incomplete_display)["levels"][0]
    assert mismatched["general_center_connector_observations"] == []
    assert mismatched["general_center_connector_review"]["status"] == "source_window_mismatch"
    level.center_result.centers = (previous, distant)
    distant_after = SimpleNamespace(date=distant.body_units[0].market_start, l=1.30, h=1.40)
    unverified = chart((before, distant_after, tail))["levels"][0]
    assert unverified["general_center_connector_observations"] == []
    assert unverified["general_center_connector_review"]["status"] == "no_direct_boundary_pair"
    level.center_result.centers = (previous, current)
    unsupported = chart((before, after, tail), market="us")["levels"][0]
    assert unsupported["general_center_connector_observations"] == []
    assert unsupported["general_center_connector_review"]["status"] == "calendar_adjacency_not_proven"
    monkeypatch.setattr("chanlun.cl_utils.strict_chart.official_trading_session_evidence", lambda **_kwargs: None)
    unavailable = chart((before, after, tail))["levels"][0]
    assert unavailable["general_center_connector_observations"] == []
    assert unavailable["general_center_connector_review"]["status"] == "calendar_adjacency_not_proven"


def test_direct_gap_calendar_gate_distinguishes_lunch_overnight_and_missing_bars():
    cn = ZoneInfo("Asia/Shanghai")
    observed = datetime(2026, 1, 6, 16, 0, tzinfo=cn)

    def adjacent(left, right, *, market="a"):
        return _official_adjacent_a_share_closes(
            datetime(*left, tzinfo=cn), datetime(*right, tzinfo=cn),
            market=market, interval="5m", as_of=observed,
        )

    assert adjacent((2026, 1, 5, 11, 30), (2026, 1, 5, 13, 5))
    assert adjacent((2026, 1, 5, 15, 0), (2026, 1, 6, 9, 35))
    assert adjacent((2025, 12, 31, 15, 0), (2026, 1, 5, 9, 35))
    assert adjacent((2026, 1, 5, 10, 0), (2026, 1, 5, 10, 20)) is None
    assert adjacent((2026, 1, 5, 15, 0), (2026, 1, 7, 9, 35)) is None
    assert adjacent((2026, 1, 5, 10, 0), (2026, 1, 5, 10, 5), market="us") is None


def test_chart_snapshot_keeps_l054_two_views_nontradable_and_causal():
    values = _l054_segments()
    at_g5 = _snapshot(values[:10])
    at_g6 = _snapshot(values[:12])

    first, second = at_g5["general_associative_hypotheses"]
    assert first["schema"] == "chanlun-entry-anchored-associative-hypothesis-v1"
    assert first["evidence_scope"] == "entry_anchored_general_geometry_hypothesis"
    assert first["anchor_unit_id"] == second["anchor_unit_id"] == values[0].unit_id
    assert first["anchor_basis"] == second["anchor_basis"] == "first_locked_segment"
    assert first["parent_centers"][0]["core"] == {"zd_tick": 60, "zg_tick": 65}
    assert second["parent_centers"] == []
    assert second["view_id"] in {
        view["view_id"] for view in at_g6["general_associative_hypotheses"]
    }
    second_later = next(
        view for view in at_g6["general_associative_hypotheses"]
        if view["view_id"] == second["view_id"]
    )
    assert second_later["parent_centers"][0]["core"] == {
        "zd_tick": 50, "zg_tick": 65,
    }
    assert second_later["parent_centers"][0]["observed_at"] > (
        first["parent_centers"][0]["observed_at"]
    )
    assert all(not view["tradable"] for view in at_g6["general_associative_hypotheses"])
    assert all(
        parent["formal_status"] == "requires_completed_child_type_certificates"
        for view in at_g6["general_associative_hypotheses"]
        for parent in view["parent_centers"]
    )
    assert not at_g5["levels"][0]["centers"]
    assert not at_g5["levels"][0]["points"]
    assert at_g5["structure_revision"] != at_g6["structure_revision"]


def test_l054_short_overlap_stays_out_of_restored_formal_center_snapshot():
    values = _l054_segments()
    at_d2 = _snapshot(values[:3])
    at_g2 = _snapshot(values[:4])
    assert calculate_centers(values[:3], 0, SourceKind.SEGMENT).centers == ()
    assert calculate_centers(values[:4], 0, SourceKind.SEGMENT).centers == ()
    assert at_d2["levels"][0]["centers"] == []
    assert at_g2["levels"][0]["centers"] == []
    for selected in (
        "three_segment_core_observations", "selected_three_core_base_view",
        "selected_user_center_policy", "selected_five_segment_centers",
        "selected_center_type_partition", "user_same_level_type_view",
    ):
        assert selected not in at_d2
        assert selected not in at_g2
    five = calculate_centers(values[:5], 0, SourceKind.SEGMENT).centers
    assert len(five) == 1
    assert five[0].entry_unit == values[0]
    assert five[0].establishment_leave_unit == values[4]
