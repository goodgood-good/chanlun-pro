import copy
import pickle

import pytest

from chanlun.core.cl import CL


def test_market_is_mandatory_and_normalized() -> None:
    with pytest.raises(ValueError, match="CL market is required"):
        CL("SH.600519", "30m", {})

    assert CL("SH.600519", "30m", {}, market=" A ").market == "a"
    assert CL("QQQ.US", "30m", {}, market="US").market == "us"


def test_current_pickle_schema_round_trips() -> None:
    current = CL("QQQ.US", "30m", {}, market="us")
    restored = pickle.loads(pickle.dumps(current))
    copied = copy.deepcopy(current)

    assert restored.market == "us"
    assert copied.market == "us"


def test_pickle_state_without_current_evidence_lock_is_rejected() -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    incomplete = dict(current.__dict__)
    incomplete.pop("_strict_evidence_lock")
    restored = object.__new__(CL)

    with pytest.raises(ValueError, match="strict CL pickle schema is invalid"):
        restored.__setstate__(incomplete)


def test_old_segment_engine_cache_is_rejected_before_reuse() -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    state = current.__getstate__()
    state["_pickle_schema"] = "chanlun-analysis-cl-v14"
    with pytest.raises(ValueError, match="strict CL pickle schema is invalid"):
        object.__new__(CL).__setstate__(state)


@pytest.mark.parametrize("old_rule", [
    "first-break-contained-stem-with-causal-invalidation-v3",
    "first-break-contained-stem-with-strict-local-extremum-v4",
    "first-break-contained-stem-and-source-backed-pivot-shift-v5",
])
def test_cache_without_current_local_segment_reference_rule_is_rejected(old_rule) -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    state = current.__getstate__()
    state["config"] = dict(
        state["config"],
        segment_reverse_break_rule=old_rule,
    )
    with pytest.raises(ValueError, match="segment_reverse_break_rule"):
        object.__new__(CL).__setstate__(state)


def test_cache_without_second_fractal_successor_evidence_is_rejected() -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    state = current.__getstate__()
    state["config"] = dict(
        state["config"], segment_gap_rule="second-feature-sequence-fractal"
    )
    with pytest.raises(ValueError, match="segment_gap_rule"):
        object.__new__(CL).__setstate__(state)


def test_cache_with_reference_filter_before_inclusion_is_rejected() -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    state = current.__getstate__()
    state["config"] = dict(
        state["config"],
        segment_feature_inclusion_rule="sequential-context-direction-with-standard-reference-provenance-v3",
    )
    with pytest.raises(ValueError, match="segment_feature_inclusion_rule"):
        object.__new__(CL).__setstate__(state)


def test_cache_with_unconditional_raw_first_gap_classification_is_rejected() -> None:
    current = CL("QQQ.US", "1m", {}, market="us")
    state = current.__getstate__()
    state["config"] = dict(
        state["config"],
        segment_gap_classification_rule="protected-reference-and-original-first-pen-v3",
    )
    with pytest.raises(ValueError, match="segment_gap_classification_rule"):
        object.__new__(CL).__setstate__(state)


def test_cache_with_unrecoverable_observation_origin_is_rejected() -> None:
    current = CL("SZ.300867", "5m", {}, market="a")
    state = current.__getstate__()
    state["config"] = dict(state["config"], segment_start_rule="earliest-three-overlap-directional-observation-origin-v1")
    with pytest.raises(ValueError, match="segment_start_rule"):
        object.__new__(CL).__setstate__(state)


def test_cache_conflating_stem_price_source_with_segment_boundary_is_rejected() -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    state = current.__getstate__()
    state["config"] = dict(
        state["config"],
        segment_equal_extreme_rule="earliest-bound-supplying-pen-as-boundary-convention-v1",
    )
    with pytest.raises(ValueError, match="segment_equal_extreme_rule"):
        object.__new__(CL).__setstate__(state)


@pytest.mark.parametrize("old_rule", [
    "unbounded-evidence-search-with-incremental-reference-v3",
    "unbounded-scan-with-incremental-reference-and-invalidation-jump-v4",
])
def test_cache_without_current_candidate_scan_rule_is_rejected(old_rule) -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    state = current.__getstate__()
    state["config"] = dict(
        state["config"],
        segment_scan_rule=old_rule,
    )
    with pytest.raises(ValueError, match="segment_scan_rule"):
        object.__new__(CL).__setstate__(state)


def test_cache_with_endpoint_only_exported_segment_ranges_is_rejected() -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    state = current.__getstate__()
    state["config"] = dict(
        state["config"],
        segment_price_range_rule="constituent-stroke-extremes-with-actual-market-time-v1",
    )
    with pytest.raises(ValueError, match="segment_price_range_rule"):
        object.__new__(CL).__setstate__(state)
