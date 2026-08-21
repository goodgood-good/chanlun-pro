from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.live_review_materialization import (
    LIVE_REVIEW_MATERIALIZATION_RECEIPT_SCHEMA,
    LIVE_REVIEW_WEB_BUNDLE_RECEIPT_SCHEMA,
    LIVE_REVIEW_WEB_INDEX_SCHEMA,
    resolve_live_review_materialization_receipt,
    resolve_live_review_web_bundle_receipt,
)
import tools.validate_trading_screening_review as subject


def test_prune_stale_web_bundle_artifacts_preserves_current_and_unrelated(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / ".web"
    artifact_root.mkdir()
    current_index = artifact_root / f"{'a' * 64}.index.json"
    current_detail = artifact_root / f"{'a' * 64}.details.jsonl"
    stale_index = artifact_root / f"{'b' * 64}.index.json"
    stale_detail = artifact_root / f"{'b' * 64}.details.jsonl"
    unrelated = artifact_root / "manual-notes.json"
    for path, content in (
        (current_index, b"current index"),
        (current_detail, b"current detail"),
        (stale_index, b"stale index"),
        (stale_detail, b"stale detail"),
        (unrelated, b"keep unrelated"),
    ):
        path.write_bytes(content)

    removed_count, removed_bytes = subject._prune_stale_web_bundle_artifacts(
        artifact_root=artifact_root,
        keep_paths=(current_index, current_detail),
    )

    assert removed_count == 2
    assert removed_bytes == len(b"stale index") + len(b"stale detail")
    assert current_index.is_file()
    assert current_detail.is_file()
    assert unrelated.is_file()
    assert not stale_index.exists()
    assert not stale_detail.exists()


def test_validator_reuses_one_sealed_snapshot_validation_for_materialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot_sha256 = "sha256:" + "a" * 64
    source = tmp_path / "screening.json"
    source.write_text(
        json.dumps({"snapshot_content_sha256": snapshot_sha256}),
        encoding="utf-8",
    )
    validation = object()
    validation_calls: list[object] = []
    materialization_calls: list[object] = []

    def validate(payload):
        validation_calls.append(payload)
        return validation

    def materialize(**kwargs):
        materialization_calls.append(kwargs["validated_snapshot"])
        return {}

    monkeypatch.setattr(subject, "_validated_live_review_snapshot", validate)
    monkeypatch.setattr(subject, "_materialize_human_review_report", materialize)

    result = subject.validate_document(
        path=source,
        expected_sha256=snapshot_sha256,
        archive_root=tmp_path / "archive",
        repository_root=tmp_path,
    )

    assert result["ready"] is True
    assert len(validation_calls) == 1
    assert materialization_calls == [validation]


def test_materializer_receipt_binds_nested_decision_source_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "screening.json"
    source.write_text("{}", encoding="utf-8")
    source_hash = "sha256:" + "1" * 64
    decision_source_id = "sha256:" + "2" * 64
    stable: dict[str, object] = {
        "schema": "chanlun-human-review-screen",
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "input_hashes": {
            "decision_source_snapshot_id": decision_source_id,
        },
        "review_queue": [],
    }
    report = {**stable, "content_sha256": sha256_json(stable)}
    monkeypatch.setattr(
        subject,
        "current_decision_source_snapshot",
        lambda _root: {"ignored": True},
    )
    monkeypatch.setattr(
        subject,
        "live_human_review_document",
        lambda **_kwargs: report,
    )
    monkeypatch.setattr(
        subject,
        "validate_human_review_screen_document",
        lambda payload: () if payload == report else (_ for _ in ()).throw(
            AssertionError("unexpected report")
        ),
    )
    archive = tmp_path / "archive"

    result = subject._materialize_human_review_report(
        payload={"as_of": datetime.fromisoformat("2026-08-03T15:00:00+08:00")},
        source_path=source,
        source_stat=source.stat(),
        expected_sha256=source_hash,
        archive_root=archive,
        repository_root=tmp_path,
    )

    receipt = json.loads(
        Path(result["materialization_receipt"]).read_text(encoding="utf-8")
    )
    assert receipt["schema"] == LIVE_REVIEW_MATERIALIZATION_RECEIPT_SCHEMA
    assert receipt["decision_source_snapshot_id"] == decision_source_id
    assert receipt["source_file_sha256"].startswith("sha256:")
    assert receipt["report_file_sha256"].startswith("sha256:")
    assert resolve_live_review_materialization_receipt(
        source_path=source,
        archive_root=archive,
        expected_source_snapshot_content_sha256=source_hash,
        expected_decision_source_snapshot_id=decision_source_id,
    ) == Path(result["human_review_report_path"])
    web_receipt = json.loads(
        Path(result["web_bundle_receipt"]).read_text(encoding="utf-8")
    )
    assert web_receipt["schema"] == LIVE_REVIEW_WEB_BUNDLE_RECEIPT_SCHEMA
    bundle = resolve_live_review_web_bundle_receipt(
        source_path=source,
        archive_root=archive,
        expected_source_snapshot_content_sha256=source_hash,
        expected_decision_source_snapshot_id=decision_source_id,
    )
    assert bundle is not None
    assert bundle.report_path == Path(result["human_review_report_path"])
    assert bundle.detail_path == Path(result["human_review_detail_store_path"])
    index = json.loads(bundle.index_path.read_text(encoding="utf-8"))
    assert index["schema"] == LIVE_REVIEW_WEB_INDEX_SCHEMA
    assert index["review_queue"] == []
    assert index["review_queue_count"] == 0

    source.write_text("{\"new_epoch\":true}", encoding="utf-8")
    assert resolve_live_review_web_bundle_receipt(
        source_path=source,
        archive_root=archive,
        expected_decision_source_snapshot_id=decision_source_id,
    ) is None
    stale_bundle = resolve_live_review_web_bundle_receipt(
        source_path=source,
        archive_root=archive,
        expected_decision_source_snapshot_id=decision_source_id,
        require_current_source=False,
    )
    assert stale_bundle is not None
    assert stale_bundle.source_current is False

    bundle.detail_path.write_bytes(b"tampered\n")
    assert resolve_live_review_web_bundle_receipt(
        source_path=source,
        archive_root=archive,
        expected_source_snapshot_content_sha256=source_hash,
        expected_decision_source_snapshot_id=decision_source_id,
    ) is None
