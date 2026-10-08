"""L025 zero-axis chronology is an auxiliary observation, not a point gate."""

from chanlun.core.strict_structure.center_frame import initial_center_known_at
from chanlun.core.strict_structure.rule_state import center_macd_phase_rule_states
from tests.core.strict_structure.test_divergence_collector import completed_consolidation_fixture


def test_completed_center_without_raw_macd_phase_keeps_context_pending():
    # L025:3424-3430 rejects the bounded just-above-zero case. A provider
    # without the raw phase series must not pretend that criterion was checked.
    structure, _entry, _departure = completed_consolidation_fixture()
    level = structure.levels[0]
    center, = level.center_result.centers
    state, = center_macd_phase_rule_states(level, center.available_at, None)
    assert state["status"] == "pending_native_macd_phase_source"
    assert state["compare_leg_unit_ids"] == ("u-0",)
    assert state["signal_leg_unit_ids"] == ("u-6",)
    assert state["phase"] is None
    assert state["author_near_zero_threshold_status"] == "not_specified"
    assert state["formal_divergence_status"] == "independent_structural_ledger"
    assert state["tradable"] is False


def test_center_macd_pending_leg_does_not_publish_future_source_ids():
    structure, _entry, _departure = completed_consolidation_fixture()
    level = structure.levels[0]
    center, = level.center_result.centers
    cutoff = initial_center_known_at(center)
    pending, = center_macd_phase_rule_states(level, cutoff, None)
    assert pending["status"] == "pending_second_leg_completion"
    assert pending["compare_leg_unit_ids"] == ("u-0",)
    assert pending["signal_leg_unit_ids"] == ()
    assert pending["center_initial_known_at"] == cutoff
    assert pending["center_context_known_at"] is None
