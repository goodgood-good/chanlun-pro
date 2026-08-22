"""正式固定年度因果回测报告的只读展示模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from chanlun.decision_support.trading_system.backtest.report import (
    SCHEMA,
    STRATEGY_ID,
    verify_report_hash,
)


_MAX_JSON_BYTES = 64 * 1024 * 1024
_ARTIFACT_DIRECTORY = Path("audit/chanlun_trading_system_backtest")
_ARTIFACT_PATH = _ARTIFACT_DIRECTORY / "certified_report.json"
_CAUSALITY_GATE_PATH = _ARTIFACT_DIRECTORY / "causality_gate.json"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_GRADES = {"certified", "research_only", "invalid"}
_CAUSAL_CONTROLS = {
    "survivorship_free_effective_dated_security_master",
    "decision_time_sw1_membership",
    "ex_date_only_causal_price_basis",
    "cash_and_share_corporate_action_accounting",
    "closed_bar_strict_structure_witnesses",
    "next_complete_minute_execution",
    "observed_range_and_volume_fill_guard",
    "delisted_security_zero_recovery",
    "content_addressed_algorithm_data_and_checkpoints",
}
_HISTORICAL_DATASET_PATH = (
    _ARTIFACT_DIRECTORY / "fixed_year_2025_2026"
)
_PIT_METADATA_NAME = "pit_metadata.json"
_EXTRACT_MANIFEST_NAME = "extract_manifest.json"
_REPLAY_HEARTBEAT_MAX_AGE_SECONDS = 10 * 60
_AUDIT_CATALOG_DIRECTORIES = (
    Path("audit/chanlun_live_integration"),
)
_AUDIT_CATALOG_LIMIT = 64


class ResearchAuditUnavailable(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResearchAuditUnavailable("artifact_invalid_json")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ResearchAuditUnavailable("artifact_invalid_json")


def _root(root: str | Path) -> Path:
    try:
        path = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ResearchAuditUnavailable("artifact_root_unavailable") from exc
    if not path.is_dir():
        raise ResearchAuditUnavailable("artifact_root_unavailable")
    return path


def _artifact(root: Path) -> Path:
    try:
        path = (root / _ARTIFACT_PATH).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise OSError("research audit artifact is not a file")
        size = path.stat().st_size
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResearchAuditUnavailable("artifact_unavailable") from exc
    if size <= 0 or size > _MAX_JSON_BYTES:
        raise ResearchAuditUnavailable("artifact_unavailable")
    return path


def _causality_gate(root: Path) -> Path | None:
    candidate = root / _CAUSALITY_GATE_PATH
    try:
        if not candidate.exists():
            return None
        path = candidate.resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise OSError("causality gate is not a file")
        size = path.stat().st_size
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResearchAuditUnavailable("causality_gate_invalid") from exc
    if size <= 0 or size > _MAX_JSON_BYTES:
        raise ResearchAuditUnavailable("causality_gate_invalid")
    return path


def _load(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ResearchAuditUnavailable:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchAuditUnavailable("artifact_invalid_json") from exc
    if not isinstance(value, dict):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value, "sha256:" + hashlib.sha256(raw).hexdigest()


def _load_catalog_document(path: Path) -> tuple[dict[str, Any], str, str]:
    """Read legacy audit records without weakening the formal-report parser.

    Several archived diagnostic exports were written by PowerShell with an
    explicit UTF-8 or UTF-16 byte-order mark.  They are valid historical JSON,
    but the certified causality gate/report intentionally remains strict UTF-8
    through :func:`_load`.
    """

    try:
        raw = path.read_bytes()
        if not raw or len(raw) > _MAX_JSON_BYTES:
            raise ResearchAuditUnavailable("artifact_invalid_json")
        if raw.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        else:
            encoding = "utf-8"
        value = json.loads(
            raw.decode(encoding),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ResearchAuditUnavailable:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchAuditUnavailable("artifact_invalid_json") from exc
    if not isinstance(value, dict):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value, "sha256:" + hashlib.sha256(raw).hexdigest(), encoding


def _modified_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(
        timespec="seconds"
    )


def _safe_file(root: Path, candidate: Path) -> Path | None:
    """Return a regular file contained by *root*, rejecting links and escapes."""
    try:
        if candidate.is_symlink():
            return None
        path = candidate.resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def _audit_catalog(root: Path) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    for relative_directory in _AUDIT_CATALOG_DIRECTORIES:
        directory = root / relative_directory
        try:
            if directory.is_dir() and not directory.is_symlink():
                candidates.extend(directory.glob("*.json"))
        except OSError:
            continue
    audit_directory = root / "audit"
    try:
        if audit_directory.is_dir() and not audit_directory.is_symlink():
            candidates.extend(audit_directory.glob("*.json"))
    except OSError:
        pass

    rows: list[dict[str, Any]] = []
    unique_paths: set[Path] = set()
    for candidate in candidates:
        path = _safe_file(root, candidate)
        if path is None or path in unique_paths:
            continue
        unique_paths.add(path)
        try:
            size = path.stat().st_size
            payload, file_sha256, encoding = _load_catalog_document(path)
            schema = payload.get("schema")
            status = payload.get("status")
            rows.append(
                {
                    "name": path.stem,
                    "relative_path": path.relative_to(root).as_posix(),
                    "category": (
                        "实时集成审计"
                        if "chanlun_live_integration" in path.parts
                        else "结构一致性审计"
                    ),
                    "schema": (
                        str(schema)[:120]
                        if isinstance(schema, str) and schema.strip()
                        else "未声明"
                    ),
                    "status": (
                        str(status)[:80]
                        if isinstance(status, str) and status.strip()
                        else "已归档"
                    ),
                    "valid_json": True,
                    "encoding": encoding,
                    "size_bytes": size,
                    "modified_at": _modified_at(path),
                    "file_sha256": file_sha256,
                }
            )
        except (OSError, ResearchAuditUnavailable):
            rows.append(
                {
                    "name": path.stem,
                    "relative_path": path.relative_to(root).as_posix(),
                    "category": "审计记录",
                    "schema": "无法解析",
                    "status": "格式无效",
                    "valid_json": False,
                    "encoding": None,
                    "size_bytes": path.stat().st_size if path.exists() else 0,
                    "modified_at": _modified_at(path) if path.exists() else None,
                    "file_sha256": None,
                }
            )
    rows.sort(key=lambda row: str(row.get("modified_at") or ""), reverse=True)
    return rows[:_AUDIT_CATALOG_LIMIT]


def _historical_dataset_status(root: Path) -> dict[str, Any] | None:
    directory = root / _HISTORICAL_DATASET_PATH
    metadata_path = _safe_file(root, directory / _PIT_METADATA_NAME)
    if metadata_path is None:
        return None
    try:
        payload, file_sha256 = _load(metadata_path)
        securities = _sequence(payload.get("securities"))
        memberships = _sequence(payload.get("memberships"))
        factors = _sequence(payload.get("factors"))
        sectors = _sequence(payload.get("qmt_sw1_sector_names"))
        source_start = _text(payload.get("source_start"), max_length=10)
        source_end = _text(payload.get("source_end"), max_length=10)
        captured_at = _text(payload.get("captured_at"), max_length=64)
        schema = _text(payload.get("schema"), max_length=120)
        content_sha256 = _text(payload.get("content_sha256"), max_length=80)
        if _HASH_RE.fullmatch(content_sha256) is None:
            raise ResearchAuditUnavailable("artifact_invalid_schema")
    except ResearchAuditUnavailable:
        return {
            "available": False,
            "relative_path": metadata_path.relative_to(root).as_posix(),
            "error_code": "historical_metadata_invalid",
        }

    checkpoint_count = 0
    checkpoint_bytes = 0
    latest_checkpoint_modified_at: datetime | None = None
    symbol_directory = directory / "symbols"
    try:
        if symbol_directory.is_dir() and not symbol_directory.is_symlink():
            for candidate in symbol_directory.glob("*.pkl"):
                path = _safe_file(root, candidate)
                if path is None:
                    continue
                stat = path.stat()
                checkpoint_count += 1
                checkpoint_bytes += stat.st_size
                candidate_modified_at = datetime.fromtimestamp(
                    stat.st_mtime
                ).astimezone()
                if (
                    latest_checkpoint_modified_at is None
                    or candidate_modified_at > latest_checkpoint_modified_at
                ):
                    latest_checkpoint_modified_at = candidate_modified_at
    except OSError:
        pass
    security_count = len(securities)
    return {
        "available": True,
        "schema": schema,
        "captured_at": captured_at,
        "source_start": source_start,
        "source_end": source_end,
        "security_count": security_count,
        "sector_count": len(sectors),
        "membership_count": len(memberships),
        "corporate_action_count": len(factors),
        "symbol_checkpoint_count": checkpoint_count,
        "symbol_checkpoint_bytes": checkpoint_bytes,
        "checkpoint_coverage": (
            checkpoint_count / security_count if security_count else 0.0
        ),
        "latest_checkpoint_modified_at": (
            latest_checkpoint_modified_at.isoformat(timespec="seconds")
            if latest_checkpoint_modified_at is not None
            else None
        ),
        "relative_path": metadata_path.relative_to(root).as_posix(),
        "content_sha256": content_sha256,
        "file_sha256": file_sha256,
        "modified_at": _modified_at(metadata_path),
    }


def _historical_replay_status(
    root: Path,
    *,
    latest_checkpoint_modified_at: object = None,
) -> dict[str, Any]:
    directory = root / _HISTORICAL_DATASET_PATH
    manifest_path = _safe_file(root, directory / _EXTRACT_MANIFEST_NAME)
    if manifest_path is None:
        return {
            "available": False,
            "status": "not_started",
            "reason_code": "extract_manifest_unavailable",
            "relative_path": (
                _HISTORICAL_DATASET_PATH / _EXTRACT_MANIFEST_NAME
            ).as_posix(),
        }
    try:
        payload, file_sha256 = _load(manifest_path)
        schema = _text(payload.get("schema"), max_length=80)
        summary = _mapping(payload.get("summary"))
        algorithm = _mapping(payload.get("algorithm"))
        complete = _boolean(payload.get("complete"))
        selected = _integer(summary.get("selected_symbol_count"))
        completed = _integer(summary.get("completed_symbol_count"))
        failed = _integer(summary.get("failed_symbol_count"))
        evaluations = _integer(summary.get("evaluation_count"))
        revision = _text(algorithm.get("revision"), max_length=80)
        generated_at = _text(payload.get("generated_at"), max_length=64)
        started_at = _text(payload.get("started_at"), max_length=64)
        if (
            schema != "chanlun-fixed-year-qmt-run"
            or _HASH_RE.fullmatch(revision) is None
            or selected <= 0
            or completed > selected
            or (complete and (completed != selected or failed != 0))
        ):
            raise ResearchAuditUnavailable("artifact_invalid_schema")
    except ResearchAuditUnavailable:
        return {
            "available": False,
            "status": "invalid",
            "reason_code": "extract_manifest_invalid",
            "relative_path": manifest_path.relative_to(root).as_posix(),
        }
    modified_at = datetime.fromtimestamp(manifest_path.stat().st_mtime).astimezone()
    checkpoint_modified_at: datetime | None = None
    try:
        if latest_checkpoint_modified_at not in (None, ""):
            checkpoint_modified_at = datetime.fromisoformat(
                str(latest_checkpoint_modified_at)
            )
            if (
                checkpoint_modified_at.tzinfo is None
                or checkpoint_modified_at.utcoffset() is None
            ):
                checkpoint_modified_at = None
    except (TypeError, ValueError):
        checkpoint_modified_at = None
    heartbeat_at = max(
        value
        for value in (modified_at, checkpoint_modified_at)
        if value is not None
    )
    heartbeat_source = (
        "symbol_checkpoint"
        if checkpoint_modified_at is not None
        and checkpoint_modified_at > modified_at
        else "extract_manifest"
    )
    heartbeat_age = max(
        0.0,
        (datetime.now().astimezone() - heartbeat_at).total_seconds(),
    )
    running = (
        not complete and heartbeat_age <= _REPLAY_HEARTBEAT_MAX_AGE_SECONDS
    )
    status = (
        "complete"
        if complete
        else "running"
        if running
        else "failed"
        if failed
        else "paused"
    )
    return {
        "available": True,
        "status": status,
        "complete": complete,
        "running": running,
        "selected_symbol_count": selected,
        "completed_symbol_count": completed,
        "failed_symbol_count": failed,
        "remaining_symbol_count": max(0, selected - completed),
        "evaluation_count": evaluations,
        "completion_ratio": completed / selected,
        "algorithm_revision": revision,
        "started_at": started_at,
        "generated_at": generated_at,
        "heartbeat_age_seconds": round(heartbeat_age, 1),
        "heartbeat_source": heartbeat_source,
        "modified_at": heartbeat_at.isoformat(timespec="seconds"),
        "manifest_modified_at": modified_at.isoformat(timespec="seconds"),
        "latest_checkpoint_modified_at": (
            checkpoint_modified_at.isoformat(timespec="seconds")
            if checkpoint_modified_at is not None
            else None
        ),
        "relative_path": manifest_path.relative_to(root).as_posix(),
        "file_sha256": file_sha256,
    }


def build_research_audit_status_snapshot(
    root: str | Path,
    *,
    formal_error_code: str,
    formal_error_details: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Expose existing historical evidence without presenting uncertified PnL.

    The certified report remains fail-closed.  This fallback only inventories
    immutable source material and audit records, so a missing report no longer
    turns the entire read-only page into an empty error card.
    """
    root_path = _root(root)
    gate_path = _safe_file(root_path, root_path / _CAUSALITY_GATE_PATH)
    report_path = _safe_file(root_path, root_path / _ARTIFACT_PATH)
    catalog = _audit_catalog(root_path)
    dataset = _historical_dataset_status(root_path)
    replay = _historical_replay_status(
        root_path,
        latest_checkpoint_modified_at=(
            dataset.get("latest_checkpoint_modified_at")
            if isinstance(dataset, dict)
            else None
        ),
    )
    return {
        "schema": "research-audit-status-page",
        "source_kind": "historical_evidence_inventory",
        "strategy_id": STRATEGY_ID,
        "active_strategy_count": 1,
        "read_only": True,
        "historical": True,
        "no_order_execution": True,
        "formal_report": {
            "available": False,
            "error_code": formal_error_code,
            "error_details": formal_error_details,
            "causality_gate_present": gate_path is not None,
            "certified_report_present": report_path is not None,
        },
        "historical_dataset": dataset,
        "historical_replay": replay,
        "audit_artifacts": catalog,
        "audit_artifact_count": len(catalog),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _sequence(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _text(value: object, *, max_length: int = 1000) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _decimal(value: object, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    try:
        converted = Decimal(str(value))
    except InvalidOperation as exc:
        raise ResearchAuditUnavailable("artifact_invalid_schema") from exc
    if not converted.is_finite():
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return converted


def _date_range(value: object) -> tuple[dict[str, Any], date, date]:
    document = _mapping(value)
    start_text = _text(document.get("start"), max_length=10)
    end_text = _text(document.get("end"), max_length=10)
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except ValueError as exc:
        raise ResearchAuditUnavailable("artifact_invalid_schema") from exc
    if start > end:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return document, start, end


def _validate_algorithm_hashes(
    value: object,
) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    output: list[tuple[str, str]] = []
    for row in _sequence(value):
        document = _mapping(row)
        source = _text(document.get("source"), max_length=300)
        digest = _text(document.get("sha256"), max_length=80)
        if source in seen or _HASH_RE.fullmatch(digest) is None:
            raise ResearchAuditUnavailable("artifact_invalid_schema")
        seen.add(source)
        output.append((source, digest))
    if not output:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return tuple(output)


def _algorithm_revision(hashes: tuple[tuple[str, str], ...]) -> str:
    encoded = json.dumps(
        hashes,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _gate_details(
    payload: dict[str, Any],
    path: Path,
    root: Path,
) -> dict[str, Any]:
    try:
        schema = _text(payload.get("schema"), max_length=80)
        checked_at = _text(payload.get("checked_at"), max_length=64)
        status = _text(payload.get("status"), max_length=32)
        pnl_generated = _boolean(payload.get("pnl_generated"))
        algorithm_revision = _text(
            payload.get("algorithm_revision"), max_length=80
        )
        validated_symbol_fact_count = _integer(
            payload.get("validated_symbol_fact_count")
        )
        validated_decision_count = _integer(
            payload.get("validated_decision_count")
        )
        snapshot_hash = _text(
            payload.get("pit_snapshot_sha256"), max_length=80
        )
        controls = [
            _text(value, max_length=100)
            for value in _sequence(payload.get("proven_controls"))
        ]
        failures: list[str] = []
        seen_codes: set[str] = set()
        for row in _sequence(payload.get("failures")):
            code = _text(row, max_length=100)
            if code in seen_codes:
                raise ResearchAuditUnavailable("causality_gate_invalid")
            seen_codes.add(code)
            failures.append(code)
        report_value = payload.get("report")
        report = (
            None
            if report_value is None
            else _text(report_value, max_length=1000)
        )
    except ResearchAuditUnavailable as exc:
        if exc.code == "causality_gate_invalid":
            raise
        raise ResearchAuditUnavailable("causality_gate_invalid") from exc
    if (
        schema != "chanlun-backtest-causality-gate"
        or status not in {"blocked", "passed"}
        or _HASH_RE.fullmatch(algorithm_revision) is None
        or _HASH_RE.fullmatch(snapshot_hash) is None
        or set(controls) != _CAUSAL_CONTROLS
        or len(controls) != len(_CAUSAL_CONTROLS)
        or (
            status == "blocked"
            and (pnl_generated is not False or not failures or report is not None)
        )
        or (
            status == "passed"
            and (pnl_generated is not True or failures or report is None)
        )
    ):
        raise ResearchAuditUnavailable("causality_gate_invalid")
    return {
        "checked_at": checked_at,
        "status": status,
        "pnl_generated": pnl_generated,
        "algorithm_revision": algorithm_revision,
        "pit_snapshot_sha256": snapshot_hash,
        "validated_symbol_fact_count": validated_symbol_fact_count,
        "validated_decision_count": validated_decision_count,
        "proven_controls": controls,
        "failures": [
            {
                "code": code,
                "evidence": "无未来函数门禁检测到不满足项。",
                "required": "修复该项后重新执行完整因果回放。",
            }
            for code in failures
        ],
        "report": report,
        "relative_path": path.relative_to(root).as_posix(),
    }


def _validated_causality_gate(root: Path) -> dict[str, Any]:
    path = _causality_gate(root)
    if path is None:
        raise ResearchAuditUnavailable("causality_gate_unavailable")
    try:
        payload, _file_sha256 = _load(path)
        details = _gate_details(payload, path, root)
    except ResearchAuditUnavailable as exc:
        if exc.code == "causality_gate_invalid":
            raise
        raise ResearchAuditUnavailable("causality_gate_invalid") from exc
    if details["status"] == "blocked":
        raise ResearchAuditUnavailable(
            "causality_gate_blocked",
            details=details,
        )
    return details


def _validate_report(
    payload: dict[str, Any],
    *,
    gate: dict[str, Any],
) -> None:
    if (
        payload.get("schema") != SCHEMA
        or payload.get("strategy_id") != STRATEGY_ID
        or payload.get("active_strategy_count") != 1
        or payload.get("read_only") is not True
        or payload.get("historical") is not True
        or payload.get("no_order_execution") is not True
    ):
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    if not verify_report_hash(payload):
        raise ResearchAuditUnavailable("artifact_hash_mismatch")
    algorithm_hashes = _validate_algorithm_hashes(
        payload.get("algorithm_hashes")
    )
    if _algorithm_revision(algorithm_hashes) != gate["algorithm_revision"]:
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    _text(payload.get("generated_at"), max_length=64)
    if payload.get("evaluation_mode") != "fixed_policy_one_year":
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    _requested, requested_start, requested_end = _date_range(
        payload.get("requested_range")
    )
    _effective, effective_start, effective_end = _date_range(
        payload.get("effective_range")
    )
    if not (
        requested_start <= effective_start <= effective_end == requested_end
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    universe = _mapping(payload.get("universe"))
    if (
        universe.get("catalog_source")
        != "qmt_sw1_with_cninfo_effective_dates"
    ):
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    eligible_sectors = _integer(universe.get("eligible_sector_count"))
    selected_symbols = _integer(universe.get("selected_symbol_count"))
    archived_symbols = _integer(
        universe.get("archived_intersecting_symbol_count")
    )
    unclassified_symbols = _integer(
        universe.get("unclassified_excluded_symbol_count")
    )
    corporate_actions = _integer(universe.get("corporate_action_count"))
    evaluation_count = _integer(universe.get("causal_evaluation_count"))
    if (
        eligible_sectors <= 0
        or universe.get("sector_composite_member_limit") is not None
        or selected_symbols <= 0
        or archived_symbols != selected_symbols + unclassified_symbols
        or corporate_actions < 0
        or gate["validated_symbol_fact_count"] != selected_symbols
        or gate["validated_decision_count"] != evaluation_count
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    source_hashes = _mapping(payload.get("data_source_hashes"))
    required_source_hashes = {
        "pit_metadata_snapshot",
        "qmt_extract_manifest",
        "prefix_invariance_audit",
        "symbol_fact_checkpoint_tree",
        "sector_fact_checkpoint_tree",
        "certified_portfolio_run",
    }
    if set(source_hashes) != required_source_hashes:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    for digest in source_hashes.values():
        if _HASH_RE.fullmatch(_text(digest, max_length=80)) is None:
            raise ResearchAuditUnavailable("artifact_invalid_schema")
    if source_hashes["pit_metadata_snapshot"] != gate["pit_snapshot_sha256"]:
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    evidence = _mapping(payload.get("data_evidence"))
    grade = _text(evidence.get("grade"), max_length=32)
    if grade not in _EVIDENCE_GRADES:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    _sequence(evidence.get("failures"))
    _sequence(evidence.get("warnings"))
    coverage = _mapping(evidence.get("coverage"))
    for value in coverage.values():
        _decimal(value)

    contract = _mapping(payload.get("execution_contract"))
    current_expected_contract = {
        "context_frequency": "30m",
        "setup_frequency": "5m",
        "trade_frequency": "5m",
        "segment_difference_frequency": "1m",
        "segment_difference_required_for_trade_signal": False,
        "segment_difference_required_for_precise_execution": True,
        "execution_observation_frequency": "1m",
        "point_classes_analyzed_independently": True,
        "buy_point_classes_share_execution_logic": True,
        "sector_price_source": "qmt-sw1-pit-composite",
        "sector_price_change_gate": False,
        "formal_selection_required": False,
        "next_tradable_minute_fill": True,
        "entry_risk_ttl_seconds": 300,
        "entry_liquidity_resize": "one_shot_to_10pct_minute_volume",
        "exit_liquidity_execution": (
            "partial_up_to_10pct_minute_volume_until_complete"
        ),
        "t_plus_one": True,
        "intraday_structural_stop": True,
    }
    # 旧认证报告仍可只读展示，但新报告必须明确区分“5分钟正式信号”、
    # “1分钟精确执行区间套”和“1分钟成交观察”，不能再把三者压成 trigger_frequency。
    previous_current_expected_contract = {
        key: value
        for key, value in current_expected_contract.items()
        if key != "segment_difference_required_for_precise_execution"
    }
    legacy_expected_contract = {
        key: value
        for key, value in current_expected_contract.items()
        if key
        not in {
            "trade_frequency",
            "segment_difference_frequency",
            "segment_difference_required_for_trade_signal",
            "segment_difference_required_for_precise_execution",
            "execution_observation_frequency",
        }
    }
    legacy_expected_contract["trigger_frequency"] = "1m"
    if not any(
        all(contract.get(key) == value for key, value in expected.items())
        for expected in (
            current_expected_contract,
            previous_current_expected_contract,
            legacy_expected_contract,
        )
    ):
        raise ResearchAuditUnavailable("strategy_contract_invalid")

    metrics = _mapping(payload.get("aggregate_out_of_sample"))
    net_return = _decimal(metrics.get("net_return"))
    max_drawdown = _decimal(metrics.get("max_drawdown"))
    calmar = _decimal(metrics.get("calmar"), optional=True)
    _decimal(metrics.get("annualized_return"), optional=True)
    _decimal(metrics.get("sharpe"), optional=True)
    _decimal(metrics.get("sortino"), optional=True)
    _integer(metrics.get("max_drawdown_duration_bars"))
    _sequence(metrics.get("warnings"))

    adequacy = _mapping(payload.get("sample_adequacy"))
    sample_passed = _boolean(adequacy.get("passed"))
    _integer(adequacy.get("closed_trade_count"))
    _mapping(adequacy.get("point_counts"))
    _sequence(adequacy.get("failures"))

    concentration = _mapping(payload.get("concentration"))
    concentration_passed = _boolean(concentration.get("passed"))
    _decimal(concentration.get("max_symbol_trade_fraction"))
    _decimal(concentration.get("max_sector_trade_fraction"))
    _decimal(concentration.get("limit"))

    verdict = _mapping(payload.get("verdict"))
    live_ready = _boolean(verdict.get("live_ready"))
    status = _text(verdict.get("status"), max_length=64)
    failed_conditions = _sequence(verdict.get("failed_conditions"))
    if len(failed_conditions) != len(set(failed_conditions)):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    if live_ready is not (status == "live_ready" and not failed_conditions):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    if live_ready and not (
        grade == "certified"
        and sample_passed
        and net_return is not None
        and net_return > 0
        and max_drawdown is not None
        and max_drawdown <= Decimal("0.10")
        and calmar is not None
        and calmar >= 1
        and concentration_passed
    ):
        raise ResearchAuditUnavailable("verdict_contract_invalid")
    for key in (
        "walk_forward_windows",
        "ablations",
        "benchmarks",
        "limitations",
    ):
        _sequence(payload.get(key))
    _mapping(payload.get("point_type_metrics"))
    _mapping(payload.get("sector_year_liquidity_metrics"))
    _mapping(payload.get("parameter_robustness"))
    bootstrap = payload.get("bootstrap_intervals")
    if bootstrap is not None:
        _mapping(bootstrap)


def build_research_audit_snapshot(root: str | Path) -> dict[str, object]:
    """读取并严格校验唯一的正式固定年度回测报告。"""
    root_path = _root(root)
    gate = _validated_causality_gate(root_path)
    path = _artifact(root_path)
    try:
        reported_path = Path(str(gate["report"])).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ResearchAuditUnavailable("causality_gate_invalid") from exc
    if reported_path != path:
        raise ResearchAuditUnavailable("causality_gate_invalid")
    payload, file_sha256 = _load(path)
    _validate_report(payload, gate=gate)
    point_type_metrics = _mapping(payload.get("point_type_metrics"))
    closed_trade_net_pnl = sum(
        (
            _decimal(_mapping(row).get("net_pnl")) or Decimal("0")
            for row in point_type_metrics.values()
        ),
        Decimal("0"),
    )
    evidence = _mapping(payload.get("data_evidence"))
    evidence_warnings = _sequence(evidence.get("warnings"))
    return {
        "schema": "research-audit-page",
        "source_kind": "certified_report",
        "strategy_id": STRATEGY_ID,
        "strategy_label": _text(payload.get("strategy_label"), max_length=120),
        "active_strategy_count": 1,
        "read_only": True,
        "historical": True,
        "no_order_execution": True,
        "generated_at": _text(payload.get("generated_at"), max_length=64),
        "evaluation_mode": "fixed_policy_one_year",
        "requested_range": _mapping(payload.get("requested_range")),
        "effective_range": _mapping(payload.get("effective_range")),
        "universe": _mapping(payload.get("universe")),
        "data_evidence": evidence,
        "execution_contract": _mapping(payload.get("execution_contract")),
        "walk_forward_windows": _sequence(payload.get("walk_forward_windows")),
        "aggregate_out_of_sample": _mapping(
            payload.get("aggregate_out_of_sample")
        ),
        "point_type_metrics": point_type_metrics,
        "closed_trade_net_pnl": str(closed_trade_net_pnl),
        "terminal_positions_marked_to_market": (
            "terminal_open_positions_marked_to_market_not_same_bar_liquidated"
            in evidence_warnings
        ),
        "sector_year_liquidity_metrics": _mapping(
            payload.get("sector_year_liquidity_metrics")
        ),
        "bootstrap_intervals": payload.get("bootstrap_intervals"),
        "ablations": _sequence(payload.get("ablations")),
        "parameter_robustness": _mapping(
            payload.get("parameter_robustness")
        ),
        "benchmarks": _sequence(payload.get("benchmarks")),
        "concentration": _mapping(payload.get("concentration")),
        "sample_adequacy": _mapping(payload.get("sample_adequacy")),
        "verdict": _mapping(payload.get("verdict")),
        "limitations": _sequence(payload.get("limitations")),
        "algorithm_hashes": _sequence(payload.get("algorithm_hashes")),
        "content_sha256": _text(payload.get("content_sha256"), max_length=80),
        "artifact": {
            "relative_path": path.relative_to(root_path).as_posix(),
            "file_sha256": file_sha256,
            "integrity_verified": True,
        },
    }


__all__ = (
    "ResearchAuditUnavailable",
    "build_research_audit_status_snapshot",
    "build_research_audit_snapshot",
)
