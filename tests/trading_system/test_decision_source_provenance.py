from __future__ import annotations

import copy
import json

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    DECISION_SOURCE_SNAPSHOT_SCHEMA,
    REPLAY_DECISION_SOURCE_SNAPSHOT_SCHEMA,
    current_decision_source_snapshot,
    current_replay_decision_source_snapshot,
    decision_source_snapshot_id,
    decision_source_snapshot_matches_current,
    replay_decision_source_snapshot_id,
    replay_decision_source_snapshot_matches_current,
)


def _recompute(value: dict[str, object]) -> None:
    stable = {
        "schema": value["schema"],
        "files": value["files"],
    }
    value["aggregate_sha256"] = sha256_json(stable)


def test_current_decision_source_snapshot_survives_json_round_trip() -> None:
    snapshot = current_decision_source_snapshot()
    restored = json.loads(json.dumps(snapshot))

    assert snapshot["schema"] == DECISION_SOURCE_SNAPSHOT_SCHEMA
    assert decision_source_snapshot_id(restored) == snapshot["aggregate_sha256"]
    assert decision_source_snapshot_matches_current(restored) is True


def test_valid_but_different_source_snapshot_is_a_stale_cohort() -> None:
    snapshot = current_decision_source_snapshot()
    changed = copy.deepcopy(snapshot)
    changed["files"][0]["sha256"] = "sha256:" + "0" * 64
    _recompute(changed)

    assert decision_source_snapshot_id(changed) == changed["aggregate_sha256"]
    assert decision_source_snapshot_matches_current(changed) is False


def test_source_snapshot_rejects_forged_or_noncanonical_manifests() -> None:
    snapshot = current_decision_source_snapshot()
    forged = {**snapshot, "aggregate_sha256": "sha256:" + "0" * 64}
    with pytest.raises(ValueError, match="aggregate changed"):
        decision_source_snapshot_id(forged)

    reversed_files = copy.deepcopy(snapshot)
    reversed_files["files"] = list(reversed(reversed_files["files"]))
    _recompute(reversed_files)
    with pytest.raises(ValueError, match="not canonical"):
        decision_source_snapshot_id(reversed_files)

    extra = {**snapshot, "unbound_note": "not part of aggregate"}
    with pytest.raises(ValueError, match="shape changed"):
        decision_source_snapshot_id(extra)


def test_replay_snapshot_is_dependency_scoped_not_forward_adapter_scoped() -> None:
    replay = current_replay_decision_source_snapshot()
    restored = json.loads(json.dumps(replay))
    paths = {row["path"] for row in replay["files"]}

    assert replay["schema"] == REPLAY_DECISION_SOURCE_SNAPSHOT_SCHEMA
    assert "tools/backtest_v3_sector_first_full_market.py" in paths
    assert "src/chanlun/core/cl.py" in paths
    assert (
        "src/chanlun/decision_support/trading_system/v3_human_review_screening.py"
        in paths
    )
    assert (
        "src/chanlun/decision_support/trading_system/human_paper_ledger.py"
        not in paths
    )
    assert (
        "src/chanlun/decision_support/trading_system/v3_forward_paper.py"
        not in paths
    )
    assert (
        "src/chanlun/decision_support/trading_system/"
        "forward_warmup_structure_lineage.py"
        not in paths
    )
    assert "tools/run_v3_forward_paper.py" not in paths
    assert (
        "web/chanlun_chart/cl_app/services/human_review_screening.py" not in paths
    )
    assert replay_decision_source_snapshot_id(restored) == replay["aggregate_sha256"]
    assert replay_decision_source_snapshot_matches_current(restored) is True


def test_full_integration_snapshot_still_binds_forward_adapters() -> None:
    paths = {
        row["path"] for row in current_decision_source_snapshot()["files"]
    }

    assert "tools/run_v3_forward_paper.py" in paths
    assert (
        "src/chanlun/decision_support/trading_system/human_paper_ledger.py"
        in paths
    )
    assert (
        "src/chanlun/decision_support/trading_system/"
        "forward_warmup_structure_lineage.py"
        in paths
    )
    assert (
        "web/chanlun_chart/cl_app/services/human_review_screening.py" in paths
    )


def test_replay_snapshot_rejects_a_valid_but_different_replay_cohort() -> None:
    snapshot = current_replay_decision_source_snapshot()
    changed = copy.deepcopy(snapshot)
    changed["files"][0]["sha256"] = "sha256:" + "0" * 64
    _recompute(changed)

    assert replay_decision_source_snapshot_id(changed) == changed["aggregate_sha256"]
    assert replay_decision_source_snapshot_matches_current(changed) is False
