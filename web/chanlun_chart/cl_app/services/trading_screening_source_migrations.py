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


def orchestration_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize an authenticated runtime-policy-only source transition."""

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
    return bool(changed_paths and changed_paths <= _ORCHESTRATION_ONLY_SOURCE_PATHS)


__all__ = ("orchestration_source_migration_allowed",)
