"""Fail-closed cache migrations for non-decision screening source changes."""

from __future__ import annotations

from typing import Mapping

from chanlun.decision_support.trading_system.decision_source_provenance import (
    decision_source_snapshot_id,
)


_ORCHESTRATION_ONLY_SOURCE_PATHS = frozenset(
    {
        "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py",
    }
)
_REVIEWED_ORCHESTRATION_SOURCE_TRANSITIONS = frozenset(
    {
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:f4d1bf3f5030621a03c590946d28b318bbb715d4c9ad187b9b463324d7f81d25",
                "sha256:743704a5116f4dfac1530ae38dd1c9f491f5d56c8e21296322f717ff4a81141b",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:34bad75e736608383eae305e0979f50e71cd884330d3d947fed2945967d678ed",
                "sha256:5e8b6809f29cd7aae51142a84f6d34af2db4894f44dde8b1252bda6de9c5f356",
            ),
        ),
    }
)


def orchestration_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize an authenticated, byte-exact orchestration transition."""

    if not isinstance(cached_decision_source_snapshot_id, str) or not isinstance(
        current_decision_source_snapshot_id,
        str,
    ):
        return False
    if not isinstance(cached_decision_source_snapshot, Mapping) or not isinstance(
        current_decision_source_snapshot,
        Mapping,
    ):
        return False
    try:
        if (
            decision_source_snapshot_id(cached_decision_source_snapshot)
            != cached_decision_source_snapshot_id
            or decision_source_snapshot_id(current_decision_source_snapshot)
            != current_decision_source_snapshot_id
        ):
            return False
    except (TypeError, ValueError):
        return False
    cached_files = {
        str(row["path"]): str(row["sha256"])
        for row in cached_decision_source_snapshot["files"]
        if isinstance(row, Mapping)
    }
    current_files = {
        str(row["path"]): str(row["sha256"])
        for row in current_decision_source_snapshot["files"]
        if isinstance(row, Mapping)
    }
    changed_paths = {
        path
        for path in set(cached_files).union(current_files)
        if cached_files.get(path) != current_files.get(path)
    }
    changed_rows = tuple(
        sorted(
            (
                path,
                cached_files.get(path),
                current_files.get(path),
            )
            for path in changed_paths
        )
    )
    return bool(
        changed_rows
        and (
            changed_paths <= _ORCHESTRATION_ONLY_SOURCE_PATHS
            or changed_rows in _REVIEWED_ORCHESTRATION_SOURCE_TRANSITIONS
        )
    )


__all__ = ("orchestration_source_migration_allowed",)
