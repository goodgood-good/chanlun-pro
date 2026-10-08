"""Historical comparisons remain true while their notification window expires."""

from copy import deepcopy
import json

import pandas as pd
import pytest

from chanlun.screening.confirmation import consolidation_lifetime_reasons
from chanlun.screening.rules import audit_native_consolidation_alerts
from chanlun.screening.runner import _save_evidence
from cl_app.services.screening import ScreeningManager
from tests.core.strict_structure.helpers import ongoing_center, ongoing_down_center
from tests.core.strict_structure.test_native_consolidation import fixture


@pytest.mark.parametrize("down", [False, True])
def test_late_center_confirmation_does_not_renew_an_already_finished_opposite_comparison(
    down,
):
    _, _, snapshot, frame, context = fixture(
        ongoing_down_center() if down else ongoing_center()
    )
    original = deepcopy(snapshot["native_consolidation_alerts"])
    assert {a["direction"] for a in original} == {"up", "down"}
    selected = audit_native_consolidation_alerts(snapshot, frame, context)
    assert {item["alert"]["direction"] for item in selected} == {
        "down" if down else "up"
    }
    assert snapshot["native_consolidation_alerts"] == original


def test_forming_following_segment_keeps_confirmation_and_its_lock_expires_it():
    _, _, snapshot, _, _ = fixture()
    alert = next(
        a
        for a in snapshot["native_consolidation_alerts"]
        if a["comparison_form"] == "entry_departure"
    )
    unit = {
        **snapshot["screening_segments"][-1],
        "unit_id": "following",
        "direction": "down",
        "locked": False,
    }
    snapshot["screening_segments"].append(unit)
    assert consolidation_lifetime_reasons(snapshot, alert) == []
    unit["locked"] = True
    assert consolidation_lifetime_reasons(snapshot, alert) == [
        "CONSOLIDATION_FOLLOWING_SEGMENT_COMPLETED"
    ]


def test_future_lock_cannot_expire_a_current_confirmation_and_warning_is_independent():
    _, _, snapshot, _, _ = fixture()
    alert = next(
        a
        for a in snapshot["native_consolidation_alerts"]
        if a["comparison_form"] == "entry_departure"
    )
    snapshot["screening_segments"].append(
        {
            "unit_id": "future",
            "locked": True,
            "available_at": snapshot["source_closed_at"] + 300,
        }
    )
    assert consolidation_lifetime_reasons(snapshot, alert) == []
    assert (
        consolidation_lifetime_reasons(snapshot, {**alert, "status": "warning"}) == []
    )


def test_saved_reader_filters_old_confirmed_comparisons_without_marking_their_proof_corrupt(
    tmp_path,
):
    _, _, snapshot, frame, _ = fixture()
    snapshot["symbol"] = "SH.600088"
    final = frame.iloc[-1].to_dict()
    final["date"] = pd.Timestamp(snapshot["source_closed_at"], unit="s", tz="UTC")
    frame = pd.concat([frame, pd.DataFrame([final])], ignore_index=True)
    directory = tmp_path / ("a" * 32)
    evidence = _save_evidence(directory, "SH.600088", "5m", frame, snapshot)
    row = {
        "market": "a",
        "code": "SH.600088",
        "frequency": "5m",
        "source_closed_at": snapshot["source_closed_at"],
        "evidence": evidence,
        "selected": [],
        "observations": [],
        "recent_rejections": [],
        "reason_counts": {},
        "consolidation_checked": True,
        "consolidation_alerts": [
            {
                "alert": a,
                "reasons": [],
                "audit": {
                    "cold_rebuild": True,
                    "confirmation_replay": True,
                    "confirmation_boundary": True,
                },
            }
            for a in snapshot["native_consolidation_alerts"]
        ],
    }
    (directory / "results.jsonl").write_text(
        json.dumps({"market": "a", "code": row["code"], "rows": [row]}) + "\n",
        encoding="utf-8",
    )
    result = ScreeningManager(tmp_path)._read_results(
        directory,
        {
            "source_current": True,
            "completed": 1,
            "settings": {"strategy": "direct_structural"},
        },
    )
    assert not result["errors"]
    assert result["verified_periods"][0]["consolidation_checked"]
    assert {c["alert"]["direction"] for c in result["consolidation_alerts"]} == {"up"}
    assert {c["alert"]["comparison_form"] for c in result["consolidation_alerts"]} == {"entry_departure", "center_oscillation"}
