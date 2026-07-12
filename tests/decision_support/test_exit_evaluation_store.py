from __future__ import annotations

from dataclasses import replace
import importlib.util
import importlib
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from chanlun.decision_support.exit_evaluation_store import (
    ExitEvaluationConflictError,
    ExitEvaluationIntegrityError,
    ExitEvaluationSnapshot,
    SQLiteExitEvaluationStore,
)


def test_exit_evaluation_store_module_contract_is_available() -> None:
    assert (
        importlib.util.find_spec(
            "chanlun.decision_support.exit_evaluation_store"
        )
        is not None
    )


def test_exit_evaluation_store_api_is_available() -> None:
    module = importlib.import_module(
        "chanlun.decision_support.exit_evaluation_store"
    )
    assert hasattr(module, "ExitEvaluationSnapshot")
    assert hasattr(module, "SQLiteExitEvaluationStore")
    assert hasattr(module, "ExitEvaluationConflictError")
    assert hasattr(module, "ExitEvaluationIntegrityError")


def test_exit_evaluation_service_api_is_available() -> None:
    module = importlib.import_module(
        "chanlun.decision_support.exit_evaluation_store"
    )
    assert hasattr(module, "ExitEvaluationService")
    assert hasattr(module, "ExitEvaluationBatchResult")


def _snapshot(*, cycle: str = "c", marker: str = "base"):
    return ExitEvaluationSnapshot(
        entry_event_id="entry-event-1",
        evaluation_cycle_id="sha256:" + cycle * 64,
        entry_provenance_fingerprint="sha256:" + "1" * 64,
        exit_evidence_policy_fingerprint="sha256:" + "2" * 64,
        certified_corpus_manifest_fingerprint="sha256:" + "3" * 64,
        source_pdf_fingerprint="sha256:" + "4" * 64,
        bar_structure_payload_fingerprint="sha256:" + "5" * 64,
        risk_context_payload_fingerprint="sha256:" + "6" * 64,
        quote_payload_fingerprint="sha256:" + "7" * 64,
        algorithm_version="chanlun-exit-runtime-v2",
        evaluation_version=2,
        recommendation_payload={
            "schema_version": 2,
            "entry_event_id": "entry-event-1",
            "marker": marker,
        },
        evaluated_at=datetime(
            2026,
            7,
            14,
            10,
            35,
            tzinfo=timezone(timedelta(hours=8)),
        ),
    )


def test_sqlite_store_is_idempotent_cas_and_restart_safe(tmp_path) -> None:
    path = tmp_path / "exit-evaluations.sqlite3"
    store = SQLiteExitEvaluationStore(path)
    snapshot = _snapshot()

    saved = store.persist(snapshot, expected_revision=0)
    replayed = store.persist(snapshot, expected_revision=0)

    assert saved == snapshot
    assert replayed == snapshot
    assert store.revision == 1
    restarted = SQLiteExitEvaluationStore(path)
    assert restarted.revision == 1
    assert restarted.get(
        snapshot.entry_event_id,
        snapshot.evaluation_cycle_id,
    ) == snapshot

    with pytest.raises(ExitEvaluationConflictError, match="payload conflict"):
        restarted.persist(_snapshot(marker="forged"), expected_revision=1)
    with pytest.raises(ExitEvaluationConflictError, match="revision conflict"):
        restarted.persist(_snapshot(cycle="d"), expected_revision=0)


def test_sqlite_store_lists_verified_snapshots_newest_first(tmp_path) -> None:
    path = tmp_path / "exit-evaluations.sqlite3"
    store = SQLiteExitEvaluationStore(path)
    first = _snapshot(cycle="a")
    second = replace(
        first,
        evaluation_cycle_id="sha256:" + "b" * 64,
        evaluated_at=first.evaluated_at + timedelta(minutes=1),
    )
    store.persist(first, expected_revision=0)
    store.persist(second, expected_revision=1)

    assert SQLiteExitEvaluationStore(path).list_snapshots() == (
        second,
        first,
    )


def test_sqlite_store_detects_persisted_payload_tampering(tmp_path) -> None:
    path = tmp_path / "exit-evaluations.sqlite3"
    snapshot = _snapshot()
    SQLiteExitEvaluationStore(path).persist(snapshot, expected_revision=0)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE exit_evaluations SET snapshot_json = ?",
            ('{"schema_version":2,"tampered":true}',),
        )
        connection.commit()

    restarted = SQLiteExitEvaluationStore(path)
    with pytest.raises(ExitEvaluationIntegrityError, match="checksum"):
        restarted.get(
            snapshot.entry_event_id,
            snapshot.evaluation_cycle_id,
        )


def test_sqlite_store_detects_redundant_identity_column_tampering(
    tmp_path,
) -> None:
    path = tmp_path / "exit-evaluations.sqlite3"
    snapshot = _snapshot()
    SQLiteExitEvaluationStore(path).persist(snapshot, expected_revision=0)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE exit_evaluations SET snapshot_id = ?",
            ("exit-evaluation:" + "f" * 64,),
        )
        connection.commit()

    with pytest.raises(ExitEvaluationIntegrityError, match="row identity"):
        SQLiteExitEvaluationStore(path).get(
            snapshot.entry_event_id,
            snapshot.evaluation_cycle_id,
        )
