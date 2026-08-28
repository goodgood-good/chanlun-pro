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
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:98b8373179fcb2c2ab772bc58975f832fc79c86c46880e4f8f34becf899a646f",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:6a1d8dd8fbf3b80794fb7f8e16f721cc73faf4119430a8c07e968adf2af233fa",
                "sha256:3cd8d938d16a422000dd7f6ea307645bed15c4a30094bd2302c845392b23cc85",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:98b8373179fcb2c2ab772bc58975f832fc79c86c46880e4f8f34becf899a646f",
                "sha256:401efa0ccbda18ec6bc203fbcac93a92ce6131dba602c70373e218918182e6e5",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:ec204210c310ca0ca1f87057e1b41b13648062be48910b9b116a2c607a524434",
                "sha256:6a1d8dd8fbf3b80794fb7f8e16f721cc73faf4119430a8c07e968adf2af233fa",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening_process.py",
                "sha256:43e1a04db1d82ef7a81de2002752d93e8a2ee22e0c6d23b2a5a0a5b7512469fa",
                "sha256:cbfd8b3c23680a2b604bae14d0c2baf8a8dc14fb537824bcb38184b5572fb0a7",
            ),
        ),
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
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:4e4ace9302d304a00373e01e659bb097677f8f3c9db5dfeb6bc57836215e8b84",
                "sha256:4b5223d73c250f293940556ec858622b4e44fc8762fb2ff9e8893320dbb0bb56",
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
_REVIEWED_SUSPENSION_EVIDENCE_RECHECK_SOURCE_TRANSITIONS = frozenset(
    {
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:117e1e518f6c4417385e72f2ad9a911147192eb413543b7610550f1bbaebf8e3",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:117e1e518f6c4417385e72f2ad9a911147192eb413543b7610550f1bbaebf8e3",
                "sha256:98b8373179fcb2c2ab772bc58975f832fc79c86c46880e4f8f34becf899a646f",
            ),
        ),
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:117e1e518f6c4417385e72f2ad9a911147192eb413543b7610550f1bbaebf8e3",
                "sha256:401efa0ccbda18ec6bc203fbcac93a92ce6131dba602c70373e218918182e6e5",
            ),
        ),
    }
)
_REVIEWED_INCOMPLETE_RETRY_RECONCILIATION_SOURCE_TRANSITIONS = frozenset(
    {
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
            ),
        ),
        (
            (
                "src/chanlun/decision_support/trading_system/live_human_review.py",
                "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
                "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
            ),
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
                "sha256:e6846a56cd2770b68af525a9b94f2dfd0bc156c0eb1340de9a849f3266a8d1fe",
            ),
        ),
    }
)
_REVIEWED_COMPLETED_RETRY_RESIDUE_SOURCE_TRANSITIONS = frozenset(
    {
        (
            (
                "web/chanlun_chart/cl_app/services/trading_screening.py",
                "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
                "sha256:e6846a56cd2770b68af525a9b94f2dfd0bc156c0eb1340de9a849f3266a8d1fe",
            ),
        ),
    }
)
_REVIEWED_SECTOR_SNAPSHOT_SOURCE_TRANSITIONS = frozenset(
    {
        (
            "sha256:bb88417a5a59aafc1891512071d40f0f0432f4a26469b26aba709146b10216ab",
            "sha256:fcb531d1e2940880845580d169999c5be7bc7d45875147c54605b38fc613bd9a",
        ),
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


def _authenticated_source_changed_rows(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> tuple[tuple[str, str | None, str | None], ...] | None:
    if not isinstance(cached_decision_source_snapshot_id, str) or not isinstance(
        current_decision_source_snapshot_id,
        str,
    ):
        return None
    if not isinstance(cached_decision_source_snapshot, Mapping) or not isinstance(
        current_decision_source_snapshot,
        Mapping,
    ):
        return None
    try:
        if (
            decision_source_snapshot_id(cached_decision_source_snapshot)
            != cached_decision_source_snapshot_id
            or decision_source_snapshot_id(current_decision_source_snapshot)
            != current_decision_source_snapshot_id
        ):
            return None
        cached_rows = cached_decision_source_snapshot["files"]
        current_rows = current_decision_source_snapshot["files"]
        if not isinstance(cached_rows, (list, tuple)) or not isinstance(
            current_rows,
            (list, tuple),
        ):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    cached_files = {
        str(row["path"]): str(row["sha256"])
        for row in cached_rows
        if isinstance(row, Mapping)
        and isinstance(row.get("path"), str)
        and isinstance(row.get("sha256"), str)
    }
    current_files = {
        str(row["path"]): str(row["sha256"])
        for row in current_rows
        if isinstance(row, Mapping)
        and isinstance(row.get("path"), str)
        and isinstance(row.get("sha256"), str)
    }
    if len(cached_files) != len(cached_rows) or len(current_files) != len(current_rows):
        return None
    changed_paths = {
        path
        for path in set(cached_files).union(current_files)
        if cached_files.get(path) != current_files.get(path)
    }
    return tuple(
        sorted(
            (
                path,
                cached_files.get(path),
                current_files.get(path),
            )
            for path in changed_paths
        )
    )


def orchestration_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize an authenticated, byte-exact reviewed cache transition."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    changed_paths = set() if changed_rows is None else {row[0] for row in changed_rows}
    return bool(
        changed_rows
        and (
            changed_paths <= _ORCHESTRATION_ONLY_SOURCE_PATHS
            or changed_rows in _REVIEWED_ORCHESTRATION_SOURCE_TRANSITIONS
            or changed_rows in _REVIEWED_SUSPENSION_EVIDENCE_RECHECK_SOURCE_TRANSITIONS
            or changed_rows
            in _REVIEWED_INCOMPLETE_RETRY_RECONCILIATION_SOURCE_TRANSITIONS
            or changed_rows
            in _REVIEWED_COMPLETED_RETRY_RESIDUE_SOURCE_TRANSITIONS
        )
    )


def suspension_evidence_recheck_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize only the reviewed status-hint/5m-evidence transition."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    return bool(
        changed_rows
        and changed_rows in _REVIEWED_SUSPENSION_EVIDENCE_RECHECK_SOURCE_TRANSITIONS
    )


def incomplete_retry_reconciliation_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize one reviewed repair of an unfinished frozen retry queue."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    return bool(
        changed_rows
        and changed_rows
        in _REVIEWED_INCOMPLETE_RETRY_RECONCILIATION_SOURCE_TRANSITIONS
    )


def completed_retry_residue_source_migration_allowed(
    *,
    cached_decision_source_snapshot_id: object,
    current_decision_source_snapshot_id: object,
    cached_decision_source_snapshot: object = None,
    current_decision_source_snapshot: object = None,
) -> bool:
    """Authorize one exact cleanup of stale errors after completed coverage."""

    changed_rows = _authenticated_source_changed_rows(
        cached_decision_source_snapshot_id=cached_decision_source_snapshot_id,
        current_decision_source_snapshot_id=current_decision_source_snapshot_id,
        cached_decision_source_snapshot=cached_decision_source_snapshot,
        current_decision_source_snapshot=current_decision_source_snapshot,
    )
    return bool(
        changed_rows
        and changed_rows in _REVIEWED_COMPLETED_RETRY_RESIDUE_SOURCE_TRANSITIONS
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
    "completed_retry_residue_source_migration_allowed",
    "incomplete_retry_reconciliation_source_migration_allowed",
    "orchestration_source_migration_allowed",
    "sector_snapshot_source_migration_allowed",
    "suspension_evidence_recheck_source_migration_allowed",
)
