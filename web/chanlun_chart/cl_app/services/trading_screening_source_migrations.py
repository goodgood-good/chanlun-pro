"""Fail-closed cache migrations for non-decision screening source changes."""

from __future__ import annotations

import re
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
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:bb5077ac0b737d14494a3357f8057c20de3171049e4f722321d4c57d6d84b568",
                "sha256:43e1a04db1d82ef7a81de2002752d93e8a2ee22e0c6d23b2a5a0a5b7512469fa",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:745fbf8abdc2864c06b2467f08d9fcda49f101385aaa7adc8e4cdc635e62e0c7",
                "sha256:ec204210c310ca0ca1f87057e1b41b13648062be48910b9b116a2c607a524434",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:743704a5116f4dfac1530ae38dd1c9f491f5d56c8e21296322f717ff4a81141b",
                "sha256:745fbf8abdc2864c06b2467f08d9fcda49f101385aaa7adc8e4cdc635e62e0c7",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:b554ac6c931cea904e0660dff27fe57537adddd9cd25f2cf4cc3285464966f03",
                "sha256:4e4ace9302d304a00373e01e659bb097677f8f3c9db5dfeb6bc57836215e8b84",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:5e8b6809f29cd7aae51142a84f6d34af2db4894f44dde8b1252bda6de9c5f356",
                "sha256:bb5077ac0b737d14494a3357f8057c20de3171049e4f722321d4c57d6d84b568",
            ),
        ),
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
_REVIEWED_SECTOR_SNAPSHOT_SOURCE_TRANSITIONS = frozenset(
    {
        (
            "sha256:c6c3e04ad2fcce74127fed58ee68ff39ffa1d3206218f70f4497c3950ea0a7d4",
            "sha256:2a5e1822092334582e3480e6908e909f3bf5b9625ab273fd59b137d017f818b1",
        ),
        (
            "sha256:544bc1e62b74d754771c8764114d8c754f5fd4c91b9dededaa83e036538c1ac8",
            "sha256:c6c3e04ad2fcce74127fed58ee68ff39ffa1d3206218f70f4497c3950ea0a7d4",
        ),
    }
)
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def sector_snapshot_source_migration_allowed(
    *,
    cached_source_revision: object,
    current_source_revision: object,
) -> bool:
    """Authorize one reviewed non-sector change by exact producer identities."""

    return bool(
        isinstance(cached_source_revision, str)
        and isinstance(current_source_revision, str)
        and _SHA256_ID.fullmatch(cached_source_revision) is not None
        and _SHA256_ID.fullmatch(current_source_revision) is not None
        and (cached_source_revision, current_source_revision)
        in _REVIEWED_SECTOR_SNAPSHOT_SOURCE_TRANSITIONS
    )


__all__ = (
    "orchestration_source_migration_allowed",
    "sector_snapshot_source_migration_allowed",
)
