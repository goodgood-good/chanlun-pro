"""A native rule upgrade changes signals, not the approved symbol universe."""

from copy import deepcopy
import json

import pytest

from chanlun.screening.evidence import load_evidence
from chanlun.screening.runner import _save_evidence
from chanlun.core.strict_structure.native_center_signals import NATIVE_SIGNAL_RULE_BASIS
from cl_app.services.screening import read_state_json
from tests.web.test_screening_native_center_signals import (
    frozen_native_run as frozen_native_run,
)
from tests.web.test_signal_monitor import setup as setup, _seed, signal


def old_rule_record(frozen_native_run, version=3):
    manager, directory, record = frozen_native_run
    row = record["rows"][0]
    frame, snapshot, _ = load_evidence(
        directory, row["code"], row["frequency"], row["evidence"]
    )
    for level in snapshot["levels"]:
        for point in level["points"]:
            point["rule_basis"] = f"user_declared_native_center_macd_v{version}"
    point = snapshot["levels"][0]["points"][0]
    row["selected"][0]["point"] = deepcopy(point)
    row["evidence"] = _save_evidence(
        directory, row["code"], row["frequency"], frame, snapshot
    )
    (directory / "results.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    return manager, directory, record


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_verified_old_native_rule_recovers_only_symbol_membership_and_cutoff(
    frozen_native_run, version,
):
    manager, directory, record = old_rule_record(frozen_native_run, version)
    result = manager.results()
    assert result["selected"] == result["observations"] == []
    (period,) = result["historical_recheck_periods"]
    assert period == {
        "market": "a",
        "code": "SH.600183",
        "name": "SH.600183",
        "frequency": "30m",
        "cutoff": record["rows"][0]["source_closed_at"],
        "revision": "0" * 64,
    }
    assert "point" not in period and "point_id" not in period
    # Monitor consumption must still reject this run as current evidence.
    assert not manager.committed_results()["processed_symbols"]


@pytest.mark.parametrize("version", [3, 4, 5])
@pytest.mark.parametrize(
    "fault", ["forged_point", "missing_frame", "mismatched_cutoff"]
)
def test_corrupt_old_proof_cannot_add_a_symbol_or_notification_baseline(
    frozen_native_run, fault, version
):
    manager, directory, record = old_rule_record(frozen_native_run, version)
    row = record["rows"][0]
    if fault == "forged_point":
        row["selected"][0]["point"]["point_id"] = "forged"
    elif fault == "missing_frame":
        next((directory / "evidence").glob("*.parquet")).unlink()
    else:
        row["source_closed_at"] += 60
    (directory / "results.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    assert not manager.results()["historical_recheck_periods"]


@pytest.mark.parametrize("version", [0, int(NATIVE_SIGNAL_RULE_BASIS.rsplit("_v", 1)[1]) + 1, 999,
                                   "04", "+4", "-4", "4_suffix"])
def test_unknown_rule_versions_cannot_restore_pool_membership(frozen_native_run, version):
    manager, _, _ = old_rule_record(frozen_native_run, version)
    result = manager.results()
    assert result["historical_recheck_periods"] == []
    assert result["selected"] == result["observations"] == []
    assert not manager.committed_results()["processed_symbols"]


def test_next_native_version_recovers_previous_members_without_an_allowlist_edit(
    frozen_native_run, monkeypatch,
):
    from cl_app.services import historical_native_pool, screening

    current = int(NATIVE_SIGNAL_RULE_BASIS.rsplit("_v", 1)[1])
    next_rule = f"user_declared_native_center_macd_v{current + 1}"
    manager, _, record = old_rule_record(frozen_native_run, current)
    monkeypatch.setattr(historical_native_pool, "NATIVE_SIGNAL_RULE_BASIS", next_rule)
    monkeypatch.setattr(screening, "NATIVE_SIGNAL_RULE_BASIS", next_rule)
    result = manager.results()
    assert result["selected"] == result["observations"] == []
    assert result["historical_recheck_periods"][0]["code"] == record["rows"][0]["code"]
    assert not manager.committed_results()["processed_symbols"]


def test_different_rule_family_does_not_expand_historical_native_compatibility(monkeypatch):
    from cl_app.services import historical_native_pool

    monkeypatch.setattr(historical_native_pool, "NATIVE_SIGNAL_RULE_BASIS", "other_native_policy_v7")
    assert historical_native_pool._historical_rule_versions() == frozenset()


@pytest.mark.parametrize("historical", [True, False])
def test_same_run_historical_read_cannot_empty_an_existing_pool(setup, historical):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    manual.rows = []
    manual.state.update(
        source_current=not historical,
        frozen_native_review=historical,
        result_reader_revision="new-reader",
    )
    service.tick()
    expected = ["a:SH.600088"] if historical else []
    assert service._load()["seed"]["codes"] == expected


def test_historical_periods_restore_a_pruned_pool_without_restoring_old_signals(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    state = service._load()
    state["seed"].update(screening_symbols=[], symbols=[], codes=[])
    state["signals"] = {}
    state["selection_checks"] = {}
    service._save()
    manual.rows = []
    manual.state.update(
        source_current=False,
        frozen_native_review=True,
        result_reader_revision="new-reader",
        error_count=1,
        total=1,
        historical_recheck_periods=[
            {
                "market": "a",
                "code": "SH.600088",
                "name": "fixture",
                "frequency": "5m",
                "revision": "old",
                "cutoff": cutoff,
            }
        ],
    )
    service.tick()
    restored = read_state_json(service.root / "state.json")
    assert restored["seed"]["codes"] == ["a:SH.600088"]
    assert restored["signals"] == {}
    check = restored["selection_checks"]["a:SH.600088:5m"]
    assert check["error"] and check["notification_baseline"] == {
        "revision": "old",
        "cutoff": cutoff,
    }
    assert not restored.get("pending_notifications")
