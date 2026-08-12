#!/usr/bin/env python3
"""Capture today's QMT GICS3 catalog into the immutable strict strategy PIT ledger.

Run this before a decision session (and again after a catalog revision when
needed).  The command only records facts; it does not scan candidates, connect
an account or create an order.  Past sessions cannot be reconstructed by
running it later.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chanlun.decision_support.trading_system.qmt_sector_ledger import (  # noqa: E402
    QMT_SECTOR_RECEIPT_SCHEMA,
    _append_sector_catalog_unlocked,
    load_sector_ledger,
)
from chanlun.decision_support.trading_system.file_lock import (  # noqa: E402
    interprocess_file_lock,
)
from chanlun.exchange.qmt_screening_sector_source import (  # noqa: E402
    build_qmt_gics3_sector_catalog,
    build_qmt_gics3_sector_catalog_from_local_files,
)


DEFAULT_OUTPUT = Path(
    ".cache/chanlun_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
)
QMT_LOCAL_DATA_ENV = "CHANLUN_QMT_LOCAL_DATA_DIR"
CN = ZoneInfo("Asia/Shanghai")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    value.add_argument(
        "--source",
        choices=("auto", "native", "local"),
        default="auto",
        help="auto tries the QMT RPC first and then its read-only local sector files",
    )
    value.add_argument("--qmt-local-data-dir", type=Path)
    value.add_argument("--receipt-dir", type=Path)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _local_data_dir(args: argparse.Namespace) -> Path | None:
    raw = args.qmt_local_data_dir or os.environ.get(QMT_LOCAL_DATA_ENV)
    return None if raw is None else Path(raw).resolve()


def _capture(args: argparse.Namespace) -> tuple[dict[str, object], str, str | None]:
    native_error: str | None = None
    if args.source in {"auto", "native"}:
        try:
            return build_qmt_gics3_sector_catalog(), "QMT_RPC", None
        except Exception as exc:
            native_error = f"{type(exc).__name__}: {exc}"
            if args.source == "native":
                raise
    data_dir = _local_data_dir(args)
    if data_dir is None:
        raise RuntimeError(
            "QMT RPC failed and no local data directory was supplied via "
            f"--qmt-local-data-dir or {QMT_LOCAL_DATA_ENV}: {native_error}"
        )
    return (
        build_qmt_gics3_sector_catalog_from_local_files(qmt_data_dir=data_dir),
        "QMT_LOCAL_SECTOR_FILES",
        native_error,
    )


def _receipt_candidates(receipt_dir: Path, capture_session: str) -> tuple[Path, ...]:
    revision_dir = receipt_dir / capture_session
    return (
        tuple(sorted(revision_dir.glob("*.json")))
        if revision_dir.is_dir()
        else ()
    )


def _matching_receipt(
    *,
    receipt_dir: Path,
    capture_session: str,
    entry_sha256: str,
    ledger_file_sha256: str,
) -> dict[str, object] | None:
    """Return the immutable receipt for the current ledger tail, if present."""

    for path in _receipt_candidates(receipt_dir, capture_session):
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("existing daily QMT receipt is unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema")
            != QMT_SECTOR_RECEIPT_SCHEMA
            or value.get("capture_session") != capture_session
            or value.get("receipt_path") != str(path)
        ):
            raise RuntimeError("existing daily QMT receipt is invalid")
        if value.get("entry_sha256") != entry_sha256:
            # 同一日内的不同修订本身也是有效证据；它必须逐字节保持不可变，
            # 不能在此处复用。
            continue
        if value.get("ledger_file_sha256") != ledger_file_sha256:
            raise RuntimeError("existing daily QMT receipt is not bound to the ledger")
        return value
    return None


def _new_receipt_path(
    *,
    receipt_dir: Path,
    capture_session: str,
    entry_sha256: str,
) -> Path:
    return (
        receipt_dir
        / capture_session
        / f"{entry_sha256.removeprefix('sha256:')}.json"
    )


def capture_daily(args: argparse.Namespace) -> dict[str, object]:
    """Capture one session and return its durable receipt."""

    catalog, transport, native_error = _capture(args)
    output = args.output.resolve()
    captured = datetime.fromisoformat(str(catalog["captured_at"]))
    receipt_dir = (
        args.receipt_dir.resolve()
        if args.receipt_dir is not None
        else output.parent / "receipts"
    )
    capture_session = captured.date().isoformat()
    lock_path = output.with_suffix(output.suffix + ".lock")
    with interprocess_file_lock(lock_path):
        existing = load_sector_ledger(output) if output.is_file() else None
        latest_existing = (
            None
            if existing is None or not existing["entries"]
            else existing["entries"][-1]
        )
        reused = bool(
            latest_existing is not None
            and datetime.fromisoformat(str(latest_existing["captured_at"])).date()
            == captured.date()
            and latest_existing["catalog_revision"] == catalog["catalog_revision"]
        )
        archived_previous_ledger = None
        if reused:
            ledger = existing
            assert ledger is not None
        else:
            ledger = _append_sector_catalog_unlocked(output, catalog)
        entries = ledger["entries"]
        latest = entries[-1]
        ledger_file_sha256 = _sha256_file(output)
        prior_receipt = _matching_receipt(
            receipt_dir=receipt_dir,
            capture_session=capture_session,
            entry_sha256=str(latest["entry_sha256"]),
            ledger_file_sha256=ledger_file_sha256,
        )
        if prior_receipt is not None:
        # 重试不得改变已经被模拟事件引用的回执，包括同一交易日内更晚的修订。
            return prior_receipt
        evidence = dict(catalog.get("capture_evidence") or {})
        source_date = (
            None
            if not evidence.get("latest_source_mtime")
            else datetime.fromisoformat(str(evidence["latest_source_mtime"])).date()
        )
        receipt_path = _new_receipt_path(
            receipt_dir=receipt_dir,
            capture_session=capture_session,
            entry_sha256=str(latest["entry_sha256"]),
        )
        if receipt_path.exists():
            raise RuntimeError("existing daily QMT receipt is not bound to the ledger")
        receipt: dict[str, object] = {
            "schema": QMT_SECTOR_RECEIPT_SCHEMA,
            "complete": True,
            "output": str(output),
            "capture_count": len(entries),
            "captured_at": latest["captured_at"],
            "capture_session": capture_session,
            "capture_transport": transport,
            "native_rpc_error": native_error,
            "same_session_revision_reused": reused,
            "sector_count": len(latest["sectors"]),
            "catalog_revision": latest["catalog_revision"],
            "entry_sha256": latest["entry_sha256"],
            "ledger_content_sha256": ledger["content_sha256"],
            "ledger_file_sha256": ledger_file_sha256,
            "archived_previous_ledger": archived_previous_ledger,
            "ledger_prefix_reconstructable": True,
            "full_ledger_archive_required": False,
            "point_in_time_scope": latest["point_in_time_scope"],
            "capture_evidence": evidence,
            "local_source_from_prior_calendar_date": (
                source_date is not None and source_date < captured.date()
            ),
            "historical_backfill_allowed": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "highest_status": "PAPER_OBSERVATION",
            "live_status": "LIVE_DISABLED",
        }
        receipt["receipt_path"] = str(receipt_path)
        _atomic_json(receipt_path, receipt)
        return receipt


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    receipt = capture_daily(args)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
