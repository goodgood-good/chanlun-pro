from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.v3_qmt_sector_ledger import (
    append_sector_catalog,
    audit_forward_sector_capture_readiness,
    load_sector_ledger,
)
from tools import snapshot_qmt_gics3_sector_ledger as subject


CN = ZoneInfo("Asia/Shanghai")
DAY_ONE = datetime(2026, 7, 27, 9, 10, tzinfo=CN)


def _catalog(
    captured_at: datetime,
    *,
    members: tuple[str, ...] = ("SH.600000", "SZ.000001"),
) -> dict[str, object]:
    sectors = [
        {
            "sector_id": "qmt-gics3:bank",
            "name": "商业银行",
            "source_key": "GICS3商业银行",
            "member_codes": list(members),
        }
    ]
    return {
        "source": "qmt_gics3_components",
        "captured_at": captured_at.isoformat(),
        "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
        "catalog_revision": sha256_json(
            {"schema": "chanlun-qmt-gics3-catalog/v1", "sectors": sectors}
        ),
        "sectors": sectors,
        "capture_transport": "QMT_LOCAL_SECTOR_FILES",
        "capture_evidence": {
            "latest_source_mtime": (captured_at - timedelta(days=1)).isoformat()
        },
    }


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_auto_capture_falls_back_to_read_only_qmt_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    expected = _catalog(DAY_ONE)

    def native_failure():
        raise RuntimeError("QMT RPC unavailable")

    monkeypatch.setattr(subject, "build_qmt_gics3_sector_catalog", native_failure)
    monkeypatch.setattr(
        subject,
        "build_qmt_gics3_sector_catalog_from_local_files",
        lambda *, qmt_data_dir: expected,
    )
    args = argparse.Namespace(
        source="auto",
        qmt_local_data_dir=tmp_path,
    )

    catalog, transport, native_error = subject._capture(args)

    assert catalog == expected
    assert transport == "QMT_LOCAL_SECTOR_FILES"
    assert native_error == "RuntimeError: QMT RPC unavailable"


def test_daily_snapshot_is_idempotent_and_reconstructs_prior_prefix(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "ledger.json"
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: (_catalog(DAY_ONE), "QMT_LOCAL_SECTOR_FILES", "rpc down"),
    )

    argv = ["--output", str(output), "--receipt-dir", str(receipts)]
    assert subject.main(argv) == 0
    first_hash = _file_sha256(output)
    assert subject.main(argv) == 0
    assert _file_sha256(output) == first_hash
    assert len(load_sector_ledger(output)["entries"]) == 1
    first_receipt = json.loads(
        (receipts / "2026-07-27.json").read_text(encoding="utf-8")
    )
    assert first_receipt["same_session_revision_reused"] is False
    assert first_receipt["ledger_prefix_reconstructable"] is True
    assert first_receipt["full_ledger_archive_required"] is False
    assert first_receipt["real_account_accessed"] is False
    assert first_receipt["live_status"] == "LIVE_DISABLED"

    day_two = DAY_ONE + timedelta(days=1)
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: (_catalog(day_two), "QMT_LOCAL_SECTOR_FILES", "rpc down"),
    )
    assert subject.main(argv) == 0

    ledger = load_sector_ledger(output)
    assert len(ledger["entries"]) == 2
    second_receipt = json.loads(
        (receipts / "2026-07-28.json").read_text(encoding="utf-8")
    )
    assert second_receipt["archived_previous_ledger"] is None
    assert second_receipt["ledger_prefix_reconstructable"] is True
    assert second_receipt["full_ledger_archive_required"] is False
    assert not (output.parent / "archive").exists()
    assert second_receipt["local_source_from_prior_calendar_date"] is True
    assert second_receipt["historical_backfill_allowed"] is False

    audit = subject.audit_daily_capture_receipts(
        output=output,
        receipt_dir=receipts,
    )
    assert audit["status"] == "COMPLETE"
    assert audit["valid_receipt_count"] == 2
    assert audit["current_ledger_receipt_count"] == 1
    assert audit["archived_prefix_receipt_count"] == 0
    assert audit["reconstructed_prefix_receipt_count"] == 1

    required = subject.audit_daily_capture_receipts(
        output=output,
        receipt_dir=receipts,
    )
    assert required["required_capture_session"] is None
    due_missing = subject.audit_sector_capture_receipts(
        output=output,
        receipt_dir=receipts,
        required_capture_session=(day_two + timedelta(days=1)).date(),
    )
    assert due_missing["status"] == "REQUIRED_CAPTURE_MISSING"
    assert due_missing["required_capture_present"] is False
    assert due_missing["required_capture_missing_sessions"] == ("2026-07-29",)

    # A same-day retry must preserve the exact immutable receipt.
    receipt_bytes = (receipts / "2026-07-28.json").read_bytes()
    assert subject.main(argv) == 0
    retry_receipt = json.loads(
        (receipts / "2026-07-28.json").read_text(encoding="utf-8")
    )
    assert retry_receipt["same_session_revision_reused"] is False
    assert retry_receipt["archived_previous_ledger"] is None
    assert (receipts / "2026-07-28.json").read_bytes() == receipt_bytes


def test_forward_archive_requires_same_session_preclose_capture_and_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "ledger.json"
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: (_catalog(DAY_ONE), "QMT_LOCAL_SECTOR_FILES", "rpc down"),
    )
    assert subject.main(
        ["--output", str(output), "--receipt-dir", str(receipts)]
    ) == 0

    ready = audit_forward_sector_capture_readiness(
        output=output,
        receipt_dir=receipts,
        session=DAY_ONE.date(),
        decision_time=DAY_ONE.replace(hour=15, minute=0),
    )
    assert ready["ready"] is True
    assert ready["reason_code"] == "READY"
    assert ready["receipt_proven"] is True
    assert ready["catalog_captured_at"] == DAY_ONE.isoformat()

    day_two = DAY_ONE + timedelta(days=1)
    missing = audit_forward_sector_capture_readiness(
        output=output,
        receipt_dir=receipts,
        session=day_two.date(),
        decision_time=day_two.replace(hour=15, minute=0),
    )
    assert missing["ready"] is False
    assert missing["reason_code"] == (
        "SAME_SESSION_SECTOR_CAPTURE_UNAVAILABLE_BEFORE_CLOSE"
    )

    # A process interruption after the hash-chain append but before receipt
    # materialisation is not a complete forward archive input.
    append_sector_catalog(output, _catalog(day_two))
    unproven = audit_forward_sector_capture_readiness(
        output=output,
        receipt_dir=receipts,
        session=day_two.date(),
        decision_time=day_two.replace(hour=15, minute=0),
    )
    assert unproven["ready"] is False
    assert unproven["reason_code"] == (
        "SAME_SESSION_SECTOR_CAPTURE_RECEIPT_UNPROVEN"
    )
    assert unproven["receipt_proven"] is False


def test_forward_archive_rejects_capture_created_after_decision_close(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "ledger.json"
    receipts = tmp_path / "receipts"
    late = DAY_ONE.replace(hour=15, minute=1)
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: (_catalog(late), "QMT_LOCAL_SECTOR_FILES", None),
    )
    assert subject.main(
        ["--output", str(output), "--receipt-dir", str(receipts)]
    ) == 0

    result = audit_forward_sector_capture_readiness(
        output=output,
        receipt_dir=receipts,
        session=late.date(),
        decision_time=late.replace(hour=15, minute=0),
    )

    assert result["ready"] is False
    assert result["reason_code"] == (
        "SAME_SESSION_SECTOR_CAPTURE_UNAVAILABLE_BEFORE_CLOSE"
    )
    assert result["receipt_proven"] is False


def test_same_session_revision_uses_a_new_immutable_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A later intraday catalog must not overwrite earlier receipt evidence."""

    output = tmp_path / "ledger.json"
    receipts = tmp_path / "receipts"
    first_catalog = _catalog(DAY_ONE)
    second_catalog = _catalog(
        DAY_ONE + timedelta(hours=1),
        members=("SH.600000", "SH.600036", "SZ.000001"),
    )
    current = first_catalog

    def capture(_args):
        return current, "QMT_RPC", None

    monkeypatch.setattr(subject, "_capture", capture)
    argv = ["--output", str(output), "--receipt-dir", str(receipts)]

    assert subject.main(argv) == 0
    first_receipt_path = receipts / "2026-07-27.json"
    first_receipt_bytes = first_receipt_path.read_bytes()
    first_receipt = json.loads(first_receipt_bytes)

    current = second_catalog
    assert subject.main(argv) == 0
    ledger = load_sector_ledger(output)
    assert len(ledger["entries"]) == 2

    # The path already referenced by the first CAPTURE event is immutable.
    assert first_receipt_path.read_bytes() == first_receipt_bytes
    second_receipt_candidates = tuple(
        path
        for path in receipts.rglob("*.json")
        if path != first_receipt_path
    )
    assert len(second_receipt_candidates) == 1
    second_receipt_path = second_receipt_candidates[0]
    second_receipt = json.loads(second_receipt_path.read_text(encoding="utf-8"))
    assert first_receipt["entry_sha256"] != second_receipt["entry_sha256"]
    assert second_receipt["entry_sha256"] == ledger["entries"][-1]["entry_sha256"]
    assert second_receipt["receipt_path"] == str(second_receipt_path)

    # Retrying the same revision reuses its exact receipt and appends nothing.
    second_receipt_bytes = second_receipt_path.read_bytes()
    assert subject.main(argv) == 0
    assert len(load_sector_ledger(output)["entries"]) == 2
    assert second_receipt_path.read_bytes() == second_receipt_bytes
    assert first_receipt_path.read_bytes() == first_receipt_bytes

    audit = subject.audit_daily_capture_receipts(
        output=output,
        receipt_dir=receipts,
    )
    assert audit["status"] == "COMPLETE"
    assert audit["valid_receipt_count"] == 2
    assert audit["current_ledger_receipt_count"] == 1
    assert audit["reconstructed_prefix_receipt_count"] == 1


def test_receipt_audit_reports_legacy_gap_without_synthesizing_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "ledger.json"
    receipts = tmp_path / "receipts"
    append_sector_catalog(output, _catalog(DAY_ONE))

    day_two = DAY_ONE + timedelta(days=1)
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: (_catalog(day_two), "QMT_LOCAL_SECTOR_FILES", "rpc down"),
    )
    assert subject.main(
        ["--output", str(output), "--receipt-dir", str(receipts)]
    ) == 0

    audit = subject.audit_daily_capture_receipts(
        output=output,
        receipt_dir=receipts,
    )

    assert audit["status"] == "LEGACY_RECEIPT_GAPS"
    assert audit["entry_count"] == 2
    assert audit["valid_receipt_count"] == 1
    assert audit["missing_entry_count"] == 1
    assert audit["missing_capture_sessions"] == ("2026-07-27",)
    assert audit["legacy_missing_capture_sessions"] == ("2026-07-27",)
    assert audit["required_missing_capture_sessions"] == ()
    assert audit["receipt_required_from_session"] == "2026-07-28"
    assert audit["invalid_receipt_count"] == 0
    assert audit["historical_receipts_synthesized"] is False
    assert len(tuple(receipts.rglob("*.json"))) == 1


def test_receipt_audit_distinguishes_post_activation_required_gap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "ledger.json"
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: (_catalog(DAY_ONE), "QMT_LOCAL_SECTOR_FILES", "rpc down"),
    )
    assert subject.main(
        ["--output", str(output), "--receipt-dir", str(receipts)]
    ) == 0

    # Simulate a process interruption after appending day two but before its
    # immutable daily receipt could be written.
    day_two = DAY_ONE + timedelta(days=1)
    append_sector_catalog(output, _catalog(day_two))

    audit = subject.audit_daily_capture_receipts(
        output=output,
        receipt_dir=receipts,
    )

    assert audit["status"] == "REQUIRED_RECEIPT_GAPS"
    assert audit["required_capture_session"] is None
    assert audit["valid_receipt_count"] == 1
    assert audit["missing_capture_sessions"] == ("2026-07-28",)
    assert audit["legacy_missing_capture_sessions"] == ()
    assert audit["required_missing_capture_sessions"] == ("2026-07-28",)
    assert audit["receipt_required_from_session"] == "2026-07-27"
    assert audit["historical_receipts_synthesized"] is False


def test_receipt_audit_rejects_safety_tampering(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "ledger.json"
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: (_catalog(DAY_ONE), "QMT_LOCAL_SECTOR_FILES", "rpc down"),
    )
    assert subject.main(
        ["--output", str(output), "--receipt-dir", str(receipts)]
    ) == 0
    receipt_path = receipts / "2026-07-27.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["real_order_transport_enabled"] = True
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False),
        encoding="utf-8",
    )

    audit = subject.audit_daily_capture_receipts(
        output=output,
        receipt_dir=receipts,
    )

    assert audit["status"] == "INVALID_RECEIPTS_PRESENT"
    assert audit["valid_receipt_count"] == 0
    assert audit["missing_entry_count"] == 1
    assert audit["invalid_receipt_count"] == 1
    assert "real-order transport" in audit["invalid_receipts"][0]["reason"]


def test_receipt_audit_rejects_unreconstructable_prefix_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "ledger.json"
    receipts = tmp_path / "receipts"
    current = _catalog(DAY_ONE)
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: (current, "QMT_LOCAL_SECTOR_FILES", "rpc down"),
    )
    argv = ["--output", str(output), "--receipt-dir", str(receipts)]
    assert subject.main(argv) == 0

    current = _catalog(DAY_ONE + timedelta(days=1))
    assert subject.main(argv) == 0
    first_receipt_path = receipts / "2026-07-27.json"
    first_receipt = json.loads(first_receipt_path.read_text(encoding="utf-8"))
    first_receipt["ledger_file_sha256"] = "sha256:" + "0" * 64
    first_receipt_path.write_text(
        json.dumps(first_receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    audit = subject.audit_daily_capture_receipts(
        output=output,
        receipt_dir=receipts,
    )
    assert audit["status"] == "INVALID_RECEIPTS_PRESENT"
    assert audit["valid_receipt_count"] == 1
    assert audit["invalid_receipt_count"] == 1
    assert "cannot be reconstructed" in audit["invalid_receipts"][0]["reason"]
