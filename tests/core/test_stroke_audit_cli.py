"""Local-file auditing produces reproducible evidence without altering input."""

import json

import pytest

from script.audit_old_strokes import audit_file, main
from tests.core.test_bi_source_updates import _frame
from tests.core.test_stroke_range_audit import SECONDARY


@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_local_ohlc_audit_exports_conflict_evidence(tmp_path, suffix):
    source = tmp_path / ("local prices" + suffix)
    output = tmp_path / "report.json"
    frame = _frame(SECONDARY, False)
    if suffix == ".csv":
        frame.to_csv(source, index=False)
    else:
        frame.to_parquet(source, index=False)
    original = source.read_bytes()
    main([str(source), "--output", str(output), "--compare-range-gate"])
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["stroke_profile"] == "old-user-path-b-completed-prefix-v14"
    assert report["profile_revision"].startswith("sha256:")
    result = report["files"][0]
    assert result["raw_bars"] == 11
    assert result["strokes"] == result["pending_strokes"] == 1
    assert result["confirmed_strokes"] == 0
    assert result["completion_status"] == "tail_pending"
    assert result["completion_evidence"] == []
    assert len(result["qualification_evidence"]) == 1
    evidence = result["qualification_evidence"][0]
    assert evidence["fractal_visible_at"] <= evidence["selected_at"]
    assert result["last_endpoint_cl_index"] == 9
    assert result["continuation_blocked_at"] is None
    assert result["old_spacing_violations"] == 0
    assert result["range_violations"] == result["confirmed_range_violations"] == 0
    assert result["issues"] == []
    assert result["adjacency_violations"] == 0
    assert result["adjacency_issues"] == []
    assert result["range_gate_only_experiment"]["range_violations"] == 0
    # The incomplete greedy experiment cannot recover origin 3; production can.
    assert result["range_gate_only_experiment"]["strokes"] == 0
    assert source.read_bytes() == original


def test_audit_rejects_ambiguous_duplicate_dates(tmp_path):
    source = tmp_path / "duplicate.csv"
    frame = _frame(SECONDARY, False)
    frame.loc[1, "date"] = frame.loc[0, "date"]
    frame.to_csv(source, index=False)
    with pytest.raises(ValueError, match="dates must be present, unique and ordered"):
        audit_file(source)


def test_isolated_range_gate_experiment_still_accepts_a_qualified_interval(tmp_path):
    source = tmp_path / "qualified.csv"
    _frame([(12,8),(10,6),(11,7),(12,8),(13,9),(14,10),(13,9)],False).to_csv(source,index=False)
    result = audit_file(source, compare_range_gate=True)
    assert result["range_gate_only_experiment"]["strokes"] == 1
    assert result["range_gate_only_experiment"]["range_violations"] == 0
