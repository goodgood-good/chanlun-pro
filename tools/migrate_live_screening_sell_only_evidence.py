"""Migrate the one known sell-only live-screening evidence policy revision.

This is deliberately not a general snapshot rewriter.  It accepts only a
content-authenticated, complete snapshot whose screening policy is exactly the
current policy minus ``sell_only_higher_timeframe_evidence_policy``.  Signal
decisions are verified before and after the presentation-only evidence fields
are completed, then the coverage epoch and snapshot identities are re-derived.
Nothing is written until the complete live-review boundary passes.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for source_root in (
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "web" / "chanlun_chart",
):
    source = str(source_root)
    if source not in sys.path:
        sys.path.insert(0, source)

from chanlun.decision_support.fingerprints import sha256_json  # noqa: E402
from chanlun.decision_support.trading_system.file_lock import (  # noqa: E402
    interprocess_file_lock,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (  # noqa: E402
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    HigherTimeframeSessionEvidence,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (  # noqa: E402
    validate_signal_decision_document,
)
from chanlun.decision_support.trading_system.live_human_review import (  # noqa: E402
    live_screening_snapshot_content_sha256,
    screening_coverage_epoch_id,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (  # noqa: E402
    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (  # noqa: E402
    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.warmup_convergence import (  # noqa: E402
    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (  # noqa: E402
    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
)
from cl_app.services.live_review_runtime_contract import (  # noqa: E402
    WEB_LIVE_REVIEW_RUNTIME_CONTRACT_ID,
    validate_live_review_snapshot,
)
from cl_app.services.trading_screening import (  # noqa: E402
    TradingScreeningConfig,
    _cache_is_valid,
    _screening_policy_document,
    _screening_policy_id,
)


POLICY_FIELD = "sell_only_higher_timeframe_evidence_policy"
POLICY_VALUE = "SCHEMA_COMPLETE_UNRESOLVED_WITHOUT_PROVIDER_CALL"


def _complete_unresolved_risk_document(risk: dict[str, object]) -> bool:
    before = set(risk)
    risk.setdefault(
        "session_evidence_contract_id",
        HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    )
    unavailable = HigherTimeframeSessionEvidence.unavailable().document()
    for subject in ("market", "sector", "symbol"):
        risk.setdefault(f"{subject}_session_evidence", dict(unavailable))
    risk.setdefault(
        "warmup_evidence_contract_id",
        QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
    )
    risk.setdefault(
        "warmup_convergence_contract_id",
        WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    )
    risk.setdefault(
        "warmup_convergence_diagnostic_contract_id",
        WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
    )
    risk.setdefault(
        "warmup_mapping_supply_diagnostic_contract_id",
        WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
    )
    risk.setdefault(
        "warmup_structure_lineage_diagnostic_contract_id",
        WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
    )
    risk.setdefault(
        "native_daily_reconciliation_contract_id",
        QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
    )
    risk.setdefault(
        "native_daily_calendar_coverage_contract_id",
        QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    )
    for subject in ("market", "sector", "symbol"):
        for suffix in (
            "warmup_evidence",
            "warmup_convergence_evidence",
            "warmup_convergence_diagnostic_evidence",
            "warmup_mapping_supply_diagnostic_evidence",
            "warmup_structure_lineage_diagnostic_evidence",
            "native_daily_reconciliation_evidence",
            "native_daily_calendar_coverage_evidence",
        ):
            risk.setdefault(f"{subject}_{suffix}", None)
    return set(risk) != before


def migrate_snapshot(source: dict[str, object]) -> tuple[dict[str, object], int]:
    declared_hash = source.get("snapshot_content_sha256")
    if (
        not isinstance(declared_hash, str)
        or declared_hash != live_screening_snapshot_content_sha256(source)
    ):
        raise ValueError("source snapshot content identity is invalid")

    current_policy = _screening_policy_document()
    if current_policy.get(POLICY_FIELD) != POLICY_VALUE:
        raise RuntimeError("current sell-only evidence policy is unexpected")
    predecessor_policy = dict(current_policy)
    predecessor_policy.pop(POLICY_FIELD)
    predecessor_policy_id = sha256_json(predecessor_policy)
    manifest = source.get("coverage_manifest")
    if (
        source.get("available") is not True
        or source.get("scan_state") != "complete"
        or source.get("full_coverage_state") != "complete"
        or source.get("screening_policy") != predecessor_policy
        or source.get("screening_policy_id") != predecessor_policy_id
        or not isinstance(manifest, dict)
        or manifest.get("complete") is not True
        or manifest.get("screening_policy_id") != predecessor_policy_id
        or not isinstance(source.get("decision_core_id"), str)
        or not isinstance(source.get("selection_research_revision"), str)
    ):
        raise ValueError("source is not the exact complete predecessor snapshot")

    migrated = dict(source)
    migrated_manifest = dict(manifest)
    migrated["coverage_manifest"] = migrated_manifest
    raw_signals = source.get("signals")
    if not isinstance(raw_signals, list):
        raise ValueError("source signals are unavailable")
    signals: list[dict[str, object]] = []
    changed_count = 0
    for raw in raw_signals:
        if not isinstance(raw, dict):
            raise ValueError("source signal is malformed")
        original_decision_id = validate_signal_decision_document(raw)
        signal = dict(raw)
        raw_risk = raw.get("higher_timeframe_risk")
        if not isinstance(raw_risk, dict):
            raise ValueError("source signal risk evidence is malformed")
        risk = dict(raw_risk)
        signal["higher_timeframe_risk"] = risk
        if _complete_unresolved_risk_document(risk):
            changed_count += 1
        if validate_signal_decision_document(signal) != original_decision_id:
            raise ValueError("presentation evidence changed a signal decision identity")
        signals.append(signal)
    if changed_count == 0:
        raise ValueError("source snapshot does not contain the known evidence gap")
    migrated["signals"] = signals

    current_policy_id = _screening_policy_id()
    migrated["screening_policy"] = current_policy
    migrated["screening_policy_id"] = current_policy_id
    migrated_manifest["screening_policy_id"] = current_policy_id
    market_data_as_of = datetime.fromisoformat(
        str(migrated_manifest["market_data_as_of"])
    )
    coverage_epoch_id = screening_coverage_epoch_id(
        market_data_as_of=market_data_as_of,
        universe_revision=str(migrated_manifest["universe_revision"]),
        sector_catalog_revision=str(migrated_manifest["sector_catalog_revision"]),
        sector_strength_evidence_revision=(
            str(migrated_manifest["sector_strength_evidence_revision"])
            if isinstance(
                migrated_manifest.get("sector_strength_evidence_revision"), str
            )
            else None
        ),
        decision_core_id=str(migrated["decision_core_id"]),
        screening_policy_id=current_policy_id,
        structure_contract_id=str(migrated["structure_contract_id"]),
        parameter_set_id=str(migrated["parameter_set_id"]),
        signal_document_contract_id=str(
            migrated_manifest["signal_document_contract_id"]
        ),
    )
    migrated["coverage_epoch_id"] = coverage_epoch_id
    migrated_manifest["coverage_epoch_id"] = coverage_epoch_id
    migrated["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        migrated
    )
    if not _cache_is_valid(
        migrated,
        TradingScreeningConfig(),
        str(migrated["decision_core_id"]),
        str(migrated["selection_research_revision"]),
    ):
        raise ValueError("migrated snapshot cache contract is invalid")
    validate_live_review_snapshot(migrated)
    return migrated, changed_count


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_file(*, source_path: Path, target_path: Path) -> dict[str, object]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise ValueError("source snapshot root is malformed")
    migrated, changed_count = migrate_snapshot(source)
    migrated_hash = str(migrated["snapshot_content_sha256"])
    generation_directory = target_path.parent / f".{target_path.name}.generations"
    generation_path = generation_directory / (
        f"{migrated_hash.removeprefix('sha256:')}.json"
    )
    lock_path = target_path.with_suffix(target_path.suffix + ".lock")
    with interprocess_file_lock(lock_path, timeout_seconds=30.0):
        _write_json_atomic(target_path, migrated)
        generation_directory.mkdir(parents=True, exist_ok=True)
        if generation_path.exists():
            existing = json.loads(generation_path.read_text(encoding="utf-8"))
            if existing != migrated:
                raise ValueError("content-addressed migrated generation conflicts")
        else:
            temporary = generation_directory / (
                f".{generation_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
            )
            try:
                with (
                    target_path.open("rb") as source_handle,
                    temporary.open("xb") as destination_handle,
                ):
                    shutil.copyfileobj(
                        source_handle,
                        destination_handle,
                        length=1024 * 1024,
                    )
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
                os.replace(temporary, generation_path)
            finally:
                temporary.unlink(missing_ok=True)
    return {
        "schema": "chanlun-live-screening-sell-only-evidence-migration",
        "source_path": str(source_path),
        "target_path": str(target_path),
        "generation_path": str(generation_path),
        "changed_signal_count": changed_count,
        "signal_count": len(migrated["signals"]),
        "coverage_epoch_id": migrated["coverage_epoch_id"],
        "snapshot_content_sha256": migrated_hash,
        "screening_policy_id": migrated["screening_policy_id"],
        "runtime_review_contract_id": WEB_LIVE_REVIEW_RUNTIME_CONTRACT_ID,
        "review_boundary_valid": True,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source", type=Path, required=True)
    result.add_argument("--target", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    result = migrate_file(
        source_path=args.source.resolve(),
        target_path=args.target.resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
