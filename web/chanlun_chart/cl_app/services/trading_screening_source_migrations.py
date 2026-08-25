"""Fail-closed cache migrations for non-decision screening source changes."""

from __future__ import annotations

from typing import Mapping

from chanlun.decision_support.trading_system.decision_source_provenance import (
    decision_source_snapshot_id,
)


_ORCHESTRATION_ONLY_SOURCE_MIGRATIONS = frozenset(
    {
        (
            "sha256:363824d1d15ab9b95a5f1918d53f2d5f9f98c160a3c6b7e51f4e4390bb1264ac",
            "sha256:7827bd74e2d369d9f84744c9a088a6cf2162a2f323b5a356fdcb9bd9d80a5209",
        ),
    }
)
_ORCHESTRATION_ONLY_SOURCE_PATHS = frozenset(
    {
        "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py",
    }
)


def orchestration_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize reviewed legacy or manifest-proven operational changes."""

    if not isinstance(cached_decision_source_snapshot_id, str) or not isinstance(
        current_decision_source_snapshot_id,
        str,
    ):
        return False
    if (
        cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id,
    ) in _ORCHESTRATION_ONLY_SOURCE_MIGRATIONS:
        return True
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
    return bool(changed_paths and changed_paths <= _ORCHESTRATION_ONLY_SOURCE_PATHS)


__all__ = ("orchestration_source_migration_allowed",)
