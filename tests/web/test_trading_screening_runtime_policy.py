from __future__ import annotations

import copy

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    current_decision_source_snapshot,
)
from cl_app.services.trading_screening_runtime_policy import (
    candidate_monitor_deadline_perf,
)
from cl_app.services.trading_screening_source_migrations import (
    orchestration_source_migration_allowed,
    sector_snapshot_source_migration_allowed,
)


def test_live_candidate_lane_keeps_shared_priority_deadline() -> None:
    assert candidate_monitor_deadline_perf(
        priority_deadline_perf=55.0,
        candidate_budget_deadline_perf=90.0,
        minute_codes_present=True,
        force_startup_bootstrap=False,
        compute_window_open=True,
    ) == pytest.approx(55.0)


def test_closed_startup_candidate_lane_uses_independent_budget() -> None:
    assert candidate_monitor_deadline_perf(
        priority_deadline_perf=55.0,
        candidate_budget_deadline_perf=90.0,
        minute_codes_present=True,
        force_startup_bootstrap=True,
        compute_window_open=False,
    ) == pytest.approx(90.0)


def test_operational_source_migration_requires_authenticated_manifests() -> None:
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id="sha256:" + "9" * 64,
        current_decision_source_snapshot_id="sha256:" + "8" * 64,
    )


def test_manifest_migration_allows_only_runtime_policy_change() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    runtime_path = (
        "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py"
    )
    runtime_row = next(row for row in cached["files"] if row["path"] == runtime_path)
    runtime_row["sha256"] = "sha256:" + "1" * 64
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )

    decision_row = next(
        row
        for row in cached["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    decision_row["sha256"] = "sha256:" + "2" * 64
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_only_exact_reviewed_shard_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    transitions = {
        "web/chanlun_chart/cl_app/services/trading_screening.py": (
            "sha256:f4d1bf3f5030621a03c590946d28b318bbb715d4c9ad187b9b463324d7f81d25",
            "sha256:743704a5116f4dfac1530ae38dd1c9f491f5d56c8e21296322f717ff4a81141b",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_process.py": (
            "sha256:34bad75e736608383eae305e0979f50e71cd884330d3d947fed2945967d678ed",
            "sha256:5e8b6809f29cd7aae51142a84f6d34af2db4894f44dde8b1252bda6de9c5f356",
        ),
    }
    for snapshot, offset in ((cached, 0), (current, 1)):
        rows = {row["path"]: row for row in snapshot["files"]}
        for path, digests in transitions.items():
            rows[path]["sha256"] = digests[offset]
        snapshot["aggregate_sha256"] = sha256_json(
            {"schema": snapshot["schema"], "files": snapshot["files"]}
        )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )

    current_row = next(
        row
        for row in current["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    )
    current_row["sha256"] = "sha256:" + "9" * 64
    current["aggregate_sha256"] = sha256_json(
        {"schema": current["schema"], "files": current["files"]}
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_exact_review_auditor_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    transitions = {
        "src/chanlun/decision_support/trading_system/live_human_review.py": (
            "sha256:b554ac6c931cea904e0660dff27fe57537adddd9cd25f2cf4cc3285464966f03",
            "sha256:4e4ace9302d304a00373e01e659bb097677f8f3c9db5dfeb6bc57836215e8b84",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_process.py": (
            "sha256:5e8b6809f29cd7aae51142a84f6d34af2db4894f44dde8b1252bda6de9c5f356",
            "sha256:bb5077ac0b737d14494a3357f8057c20de3171049e4f722321d4c57d6d84b568",
        ),
    }
    for snapshot, offset in ((cached, 0), (current, 1)):
        rows = {row["path"]: row for row in snapshot["files"]}
        for path, digests in transitions.items():
            rows[path]["sha256"] = digests[offset]
        snapshot["aggregate_sha256"] = sha256_json(
            {"schema": snapshot["schema"], "files": snapshot["files"]}
        )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_sector_snapshot_migration_allows_only_exact_reviewed_revision_pair() -> None:
    cached = (
        "sha256:544bc1e62b74d754771c8764114d8c754f5fd4c91b9dededaa83e036538c1ac8"
    )
    current = (
        "sha256:c6c3e04ad2fcce74127fed58ee68ff39ffa1d3206218f70f4497c3950ea0a7d4"
    )

    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=cached,
        current_source_revision=current,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=current,
        current_source_revision=cached,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=cached,
        current_source_revision="sha256:" + "0" * 64,
    )
