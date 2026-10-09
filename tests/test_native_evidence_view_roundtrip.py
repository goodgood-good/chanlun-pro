"""Persisted native evidence must keep the selected center's source proof."""
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from chanlun.screening.evidence import (
    evidence_consolidation_catalog,
    evidence_health,
    evidence_native_signal_catalog,
)
from chanlun.screening.rules import _native_center_signal_source_valid, native_consolidation_source_valid
from chanlun.screening.runner import _save_evidence, snapshot_for


@pytest.fixture(scope="module")
def native_evidence():
    frame = pd.read_parquet(Path(__file__).parent / "fixtures/SH.513100_5m_center_20261009.parquet")
    snapshot = snapshot_for(frame, "SH.513100", "5m", include_consolidation=True)
    centers = {c["center_id"]: c for level in snapshot["levels"] for c in level["centers"]}
    points = [p for level in snapshot["levels"] for p in level["points"] if p.get("view_id")]
    alerts = [a for a in snapshot["native_consolidation_alerts"] if a.get("view_id")]
    assert points and alerts
    assert all(_native_center_signal_source_valid(snapshot, p, centers, snapshot["screening_segments"])
               for p in points)
    assert all(native_consolidation_source_valid(snapshot, a) for a in alerts)
    return frame, snapshot, points, alerts


def catalogs(directory, frame, snapshot):
    manifest = _save_evidence(directory, "SH.513100", "5m", frame, snapshot)
    assert evidence_health(directory, "SH.513100", "5m", manifest) == {"complete": True, "verified": True}
    points, centers, point_valid = evidence_native_signal_catalog(
        directory, "SH.513100", "5m", manifest, source_closed_at=snapshot["source_closed_at"],
    )
    alerts, alert_valid = evidence_consolidation_catalog(
        directory, "SH.513100", "5m", manifest, source_closed_at=snapshot["source_closed_at"],
    )
    return points, centers, point_valid, alerts, alert_valid


def test_selected_native_points_and_consolidation_remain_valid_after_save(native_evidence, tmp_path):
    frame, snapshot, selected_points, selected_alerts = native_evidence
    points, centers, point_valid, alerts, alert_valid = catalogs(tmp_path, frame, snapshot)
    # Exercise the real save/read projection, not only the in-memory validator.
    for point in selected_points:
        assert points[point["point_id"]] == point
        assert point_valid[point["point_id"]] is True
        assert centers[point["center_id"]]["view_id"] == point["view_id"]
    for alert in selected_alerts:
        assert alerts[alert["alert_id"]] == alert
        assert alert_valid[alert["alert_id"]] is True


@pytest.mark.parametrize("fault", ["missing", "forged_return", "missing_connection", "early_selection"])
def test_valid_file_hash_does_not_bypass_selected_view_proof(native_evidence, tmp_path, fault):
    frame, original, selected_points, selected_alerts = native_evidence
    snapshot = deepcopy(original)
    if fault == "missing":
        snapshot.pop("center_view_selections")
    else:
        for record in snapshot["center_view_selections"]:
            if fault == "forged_return":
                record["return_unit_id"] = "nonexistent-return"
            elif fault == "missing_connection":
                record["connection_unit_ids"] = []
            else:
                record["available_at"] -= 300
    # Re-save with valid hashes: the original role/clock checks must still
    # reject broken source proof, rather than relying on file corruption.
    _, _, point_valid, _, alert_valid = catalogs(tmp_path, frame, snapshot)
    assert all(point_valid[p["point_id"]] is False for p in selected_points)
    assert all(alert_valid[a["alert_id"]] is False for a in selected_alerts)
