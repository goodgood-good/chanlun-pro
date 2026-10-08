"""L093-L095: physical pen membership precedes any class/formal center claim."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart import build_center_snapshot
from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.physical_stroke_lineage import (
    observe_class_formal_component_candidates,
    observe_physical_segment_stroke_lineage,
)


@pytest.fixture(scope="module")
def recorded_source():
    path = Path(__file__).parents[2] / "fixtures/segment_origin/SH.688377_5m.parquet"
    frame = pd.read_parquet(path)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.688377", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    return runtime.cd


def test_recorded_pen_membership_is_exposed_without_center_promotion(recorded_source):
    cd = recorded_source
    raw_segments, segments = cd.get_xds(), cd.get_segment_units()
    raw_strokes, strokes = cd.get_contiguous_bis(), cd.get_stroke_observation_units()
    records = observe_physical_segment_stroke_lineage(
        raw_segments, segments, raw_strokes, strokes,
    )
    assert len(records) == len(segments) == 87
    assert all(record["status"] == "exact_raw_stroke_membership" for record in records)
    assert all(not record["tradable"] and not record["formal_center_identity_claim"]
               for record in records)
    by_index = {line.index: unit.unit_id for line, unit in zip(raw_strokes, strokes)}
    for line, unit, record in zip(raw_segments, segments, records):
        assert record["segment_unit_id"] == unit.unit_id
        assert record["stroke_unit_ids"] == tuple(
            by_index[index]
            for index in range(line.start_line.index, line.end_line.index + 1)
        )
    assert all(not unit.child_ids for unit in segments)

    snapshot = build_center_snapshot(
        cd, interval="5m",
        display_bar_closed_at=(int(cd._strict_as_of().timestamp()),),
    )
    assert snapshot["physical_segment_stroke_lineage_status"] == "exact_raw_source_membership"
    assert [row["segment_unit_id"] for row in snapshot["physical_segment_stroke_lineage"]] == [
        unit.unit_id for unit in segments
    ]
    assert all(not row["tradable"] and not row["formal_center_identity_claim"]
               for row in snapshot["physical_segment_stroke_lineage"])
    assert snapshot["stroke_center_observations"]
    assert snapshot["levels"][0]["centers"]

    evidence = cd.get_strict_evidence()
    observed = evidence.stroke_center_observations.centers
    formal = evidence.structure.levels[0].center_result.centers
    compared = observe_class_formal_component_candidates(
        observed, formal, records, as_of=cd._strict_as_of(),
    )
    assert compared["status"] == "component_candidates_only"
    # Equal-boundary returns stay in their original center; the recorded
    # source consequently has one fewer separate center pairing.
    assert compared["component_pair_count"] == 22
    assert compared["price_overlap_pair_count"] == 13
    assert max(pair["price_overlap_candidate_count_for_formal"]
               for pair in compared["pairs"]) > 1
    assert all(not pair["same_center_identity_proven"] and not pair["tradable"]
               for pair in compared["pairs"])
    assert len(snapshot["u194_class_formal_component_candidates"]) == 22
    assert snapshot["u194_class_formal_component_review"]["price_overlap_pair_count"] == 13
    assert not snapshot["u194_class_formal_component_review"]["same_center_identity_proven"]


def test_missing_raw_pen_span_stays_pending_and_an_internal_gap_is_rejected(recorded_source):
    cd = recorded_source
    raw_segments, segments = cd.get_xds(), cd.get_segment_units()
    raw_strokes, strokes = cd.get_contiguous_bis(), cd.get_stroke_observation_units()
    first = raw_segments[0]
    missing_indices = SimpleNamespace(
        start=first.start, end=first.end, type=first.type,
        start_line=None, end_line=None,
    )
    pending, = observe_physical_segment_stroke_lineage(
        (missing_indices,), segments[:1], raw_strokes, strokes,
    )
    assert pending["status"] == "pending_raw_stroke_indices"
    assert pending["stroke_unit_ids"] == ()
    assert not pending["formal_center_identity_claim"]

    internal = first.start_line.index + 1
    filtered = [(line, unit) for line, unit in zip(raw_strokes, strokes)
                if line.index != internal]
    with pytest.raises(ValueError, match="source gap"):
        observe_physical_segment_stroke_lineage(
            raw_segments[:1], segments[:1],
            (item[0] for item in filtered),
            (item[1] for item in filtered),
        )
    with pytest.raises(ValueError, match="price bases"):
        observe_physical_segment_stroke_lineage(
            raw_segments[:1], segments[:1], raw_strokes,
            (replace(strokes[0], price_basis_revision="other"), *strokes[1:]),
        )

    evidence = cd.get_strict_evidence()
    observed = evidence.stroke_center_observations.centers
    formal = evidence.structure.levels[0].center_result.centers
    memberships = observe_physical_segment_stroke_lineage(
        raw_segments, segments, raw_strokes, strokes,
    )
    needed = formal[0].body_units[0].unit_id
    incomplete = tuple(row for row in memberships if row["segment_unit_id"] != needed)
    review = observe_class_formal_component_candidates(
        observed, formal, incomplete, as_of=cd._strict_as_of(),
    )
    assert review["status"] == "pending_segment_membership"
    assert formal[0].center_id in review["formal_center_ids_missing_segment_membership"]
    assert not review["same_center_identity_proven"]

    before_first_formal = min(center.available_at for center in formal) - timedelta(microseconds=1)
    prior = observe_class_formal_component_candidates(
        observed, formal, memberships, as_of=before_first_formal,
    )
    assert prior["formal_center_count"] == 0
    assert prior["component_pair_count"] == 0
