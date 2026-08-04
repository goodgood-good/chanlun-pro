#!/usr/bin/env python3
"""Run the bounded V3 sector-release gates without launching a full replay.

This command is deliberately read-only.  It validates the current research
artifact and, when a local QMT directory is supplied, rebuilds one deterministic
sector's physical-source evidence and one historical sector decision.  It never
evaluates every candidate, matches an order, writes QMT data, accesses an
account, or enables live status.

The intended release sequence is:

1. normal static/unit checks;
2. this command with ``--qmt-local-data-dir``;
3. one full-year replay only after both bounded gates pass;
4. this command again against the published artifact.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.fingerprints import sha256_json  # noqa: E402
from chanlun.decision_support.trading_system.backtest.current_sector import (  # noqa: E402
    CURRENT_GICS3_COMPOSITE_PROVIDER,
    CurrentQmtGics3CompositeReplaySource,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    sector_facts_from_frame,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    PITMetadataIndex,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (  # noqa: E402
    read_qmt_local_derived_30m,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (  # noqa: E402
    replay_decision_source_snapshot_matches_current,
)
from chanlun.decision_support.trading_system.higher_timeframe_execution_attribution import (  # noqa: E402
    higher_timeframe_execution_attribution,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (  # noqa: E402
    QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID,
    QmtSectorSameBaseCoverageEvidence,
    higher_timeframe_effectiveness_audit,
)
from chanlun.decision_support.trading_system.qmt_sector_same_base import (  # noqa: E402
    derive_qmt_sector_thirty_minute_frame,
)
from chanlun.decision_support.trading_system.v3_recent_year_provenance import (  # noqa: E402
    recent_year_research_algorithm_hashes,
    recent_year_research_algorithm_revision,
)
from chanlun.decision_support.trading_system.v3_qmt_sector_ledger import (  # noqa: E402
    load_sector_ledger,
)
from chanlun.research_release.v3_sector_release_manifest import (  # noqa: E402
    verify_sector_release_manifest,
)


SCHEMA = "chanlun-v3-sector-release-preflight/v1"
DEFAULT_ARTIFACT = Path(
    "audit/chanlun_trading_system_backtest/"
    "recent_year_current_sector_no3p_mwd_strength/"
    "approximate_technical_backtest_sector_mwd_strength_tactical_lifecycle.json"
)
DEFAULT_PIT = Path(
    "audit/chanlun_trading_system_backtest/"
    "fixed_year_2025_2026/pit_metadata.json"
)
DEFAULT_CATALOG = Path(
    ".cache/chanlun_v3_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=PROJECT_ROOT)
    value.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    value.add_argument(
        "--pit-snapshot",
        type=Path,
        help="override the release-bound PIT snapshot",
    )
    value.add_argument(
        "--catalog-ledger",
        type=Path,
        help="override the release-bound immutable catalog ledger",
    )
    value.add_argument(
        "--release-manifest",
        type=Path,
        help=(
            "immutable upstream binding; defaults to "
            "v3_release_manifest.json beside --artifact"
        ),
    )
    value.add_argument("--qmt-local-data-dir", type=Path)
    value.add_argument("--sample-symbol")
    value.add_argument("--sample-decision-at", type=datetime.fromisoformat)
    value.add_argument(
        "--allow-stale-source",
        action="store_true",
        help=(
            "permit an internally valid old artifact while changed decision "
            "sources are being tested before their single release replay"
        ),
    )
    value.add_argument(
        "--require-qmt-sample",
        action="store_true",
        help="fail unless the real single-sector QMT gate is executed",
    )
    return value


def _resolve(root: Path, path: Path) -> Path:
    root = root.resolve()
    result = path.resolve() if path.is_absolute() else (root / path).resolve()
    result.relative_to(root)
    return result


def _release_input_path(
    *,
    root: Path,
    explicit: Path | None,
    release_receipt: Mapping[str, object] | None,
    key: str,
    fallback: Path,
) -> Path:
    if explicit is not None:
        return _resolve(root, explicit)
    if release_receipt is not None:
        files = _mapping(release_receipt.get("bound_files"), "release bound files")
        binding = _mapping(files.get(key), f"release bound file {key}")
        raw = binding.get("path")
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"release bound file {key} has no path")
        path = _resolve(root, Path(raw))
        if _sha256_file(path) != binding.get("file_sha256"):
            raise ValueError(f"release bound file {key} changed")
        return path
    return _resolve(root, fallback)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda raw: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {raw}")
        ),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _validate_artifact(
    artifact: Mapping[str, object],
    *,
    root: Path,
    allow_stale_source: bool,
) -> dict[str, object]:
    stable = dict(artifact)
    reported_content = stable.pop("content_sha256", None)
    if reported_content != sha256_json(stable):
        raise ValueError("artifact content_sha256 does not bind its document")
    if artifact.get("highest_status") != "RESEARCH_ONLY":
        raise ValueError("release preflight accepts RESEARCH_ONLY only")
    if artifact.get("live_status") != "LIVE_DISABLED":
        raise ValueError("release preflight requires LIVE_DISABLED")

    source_snapshot = _mapping(
        artifact.get("decision_source_snapshot"),
        "decision_source_snapshot",
    )
    source_current = replay_decision_source_snapshot_matches_current(
        source_snapshot,
        root,
    )
    if not source_current and not allow_stale_source:
        raise ValueError("artifact decision sources do not match the workspace")

    raw_candidates = _sequence(artifact.get("candidate_audit"), "candidate_audit")
    candidates = tuple(_mapping(value, "candidate row") for value in raw_candidates)
    expected_risk = higher_timeframe_effectiveness_audit(candidates)
    if artifact.get("higher_timeframe_effectiveness_audit") != expected_risk:
        raise ValueError("higher-timeframe effectiveness audit is not reproducible")

    research = _mapping(
        artifact.get("research_variant_result"),
        "research_variant_result",
    )
    replay = _mapping(research.get("replay"), "research replay")
    terminal = _mapping(
        research.get("terminal_accounting_attribution"),
        "terminal accounting attribution",
    )
    expected_execution = higher_timeframe_execution_attribution(
        candidates,
        replay,
        terminal,
    )
    if artifact.get("higher_timeframe_execution_attribution") != expected_execution:
        raise ValueError("higher-timeframe execution attribution is not reproducible")

    return {
        "content_sha256": reported_content,
        "decision_source_aggregate_sha256": source_snapshot.get(
            "aggregate_sha256"
        ),
        "decision_source_matches_current": source_current,
        "risk_audit_sha256": expected_risk["audit_sha256"],
        "execution_audit_sha256": expected_execution["audit_sha256"],
        "candidate_count": len(candidates),
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


def _coverage_candidate(
    artifact: Mapping[str, object],
    *,
    symbol: str | None,
    decision_at: datetime | None,
) -> Mapping[str, object]:
    rows: list[Mapping[str, object]] = []
    for raw in _sequence(artifact.get("candidate_audit"), "candidate_audit"):
        row = _mapping(raw, "candidate row")
        if symbol is not None and row.get("symbol") != symbol:
            continue
        if decision_at is not None and row.get("decision_at") != decision_at.isoformat():
            continue
        warmup = row.get("sector_risk_warmup_evidence")
        if not isinstance(warmup, Mapping):
            continue
        coverage = warmup.get("strict_same_5m_source_coverage")
        if not isinstance(coverage, Mapping):
            continue
        if coverage.get("contract_id") != (
            QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID
        ):
            continue
        rows.append(row)
    if not rows:
        raise ValueError("no current v3 sector-coverage candidate matches the sample")
    rows.sort(
        key=lambda row: (
            row.get("accepted") is not True,
            int(
                _mapping(
                    _mapping(
                        row["sector_risk_warmup_evidence"],
                        "sector warmup",
                    )["strict_same_5m_source_coverage"],
                    "sector coverage",
                )["physical_source_representative_member_count"]
            ),
            str(row.get("decision_at")),
            str(row.get("symbol")),
        )
    )
    if symbol is not None or decision_at is not None:
        if len(rows) != 1:
            raise ValueError("sample selector is ambiguous")
    return rows[0]


def _catalog_sector(
    artifact: Mapping[str, object],
    ledger: Mapping[str, object],
    sector_id: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    input_hashes = _mapping(artifact.get("input_hashes"), "input_hashes")
    expected_entry = input_hashes.get("current_catalog_entry")
    matches: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for raw_entry in _sequence(ledger.get("entries"), "catalog entries"):
        entry = _mapping(raw_entry, "catalog entry")
        if entry.get("entry_sha256") != expected_entry:
            continue
        for raw_sector in _sequence(entry.get("sectors"), "catalog sectors"):
            sector = _mapping(raw_sector, "catalog sector")
            if sector.get("sector_id") == sector_id:
                matches.append((entry, sector))
    if len(matches) != 1:
        raise ValueError("artifact-bound catalog sector is unavailable or ambiguous")
    return matches[0]


def _physical_projection(raw: Mapping[str, object]) -> dict[str, object]:
    return {
        "physical_source_boundary_status": raw.get("boundary_status"),
        "physical_source_requested_start_at": raw.get("requested_start_at"),
        "physical_source_required_contributor_start_at": raw.get(
            "required_contributor_physical_start_at"
        ),
        "physical_source_representative_member_count": raw.get(
            "representative_member_count"
        ),
        "physical_source_available_member_count": raw.get(
            "available_member_file_count"
        ),
        "physical_source_required_contributor_count": raw.get(
            "required_contributor_count"
        ),
        "physical_source_inventory_revision": raw.get(
            "source_inventory_revision"
        ),
    }


def _validate_physical_projection(
    raw: Mapping[str, object],
    coverage: Mapping[str, object],
) -> dict[str, object]:
    stable = dict(raw)
    audit_sha256 = stable.pop("audit_sha256", None)
    if audit_sha256 != sha256_json(stable):
        raise ValueError("QMT physical-source audit hash is invalid")
    if (
        raw.get("diagnostic_only") is not True
        or raw.get("decision_core_input") is not False
        or raw.get("warmup_requirement_unchanged") is not True
        or raw.get("data_grade") != "RESEARCH_ONLY"
        or raw.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("QMT physical-source audit changed its safety role")
    parsed = QmtSectorSameBaseCoverageEvidence.from_document(coverage)
    if parsed.document() != dict(coverage):
        raise ValueError("sector coverage is not a canonical v3 document")
    expected = {
        key: coverage.get(key) for key in _physical_projection(raw)
    }
    actual = _physical_projection(raw)
    if actual != expected:
        changed = tuple(
            key for key in sorted(actual) if actual[key] != expected[key]
        )
        raise ValueError(
            "single-sector QMT physical evidence diverged: " + ",".join(changed)
        )
    return {
        **actual,
        "physical_audit_sha256": audit_sha256,
        "physical_projection_unchanged": True,
        "data_grade": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


def _sector_decision_projection(
    row: Mapping[str, object],
    assessment: object,
) -> dict[str, object]:
    """Compare one freshly recomputed sector decision with the old artifact.

    A physical-file inventory can prove that QMT bytes still exist, but it
    cannot prove that changed decision code still classifies those bytes the
    same way.  Keep this projection deliberately small: one deterministic
    sector and one already-audited decision timestamp are sufficient for the
    bounded gate; the full-year replay remains the release gate.
    """

    expected = {
        "eligible": row.get("sector_eligible"),
        "hard_block": row.get("sector_hard_block"),
        "regime": row.get("sector_regime"),
        "reason_codes": tuple(row.get("sector_rank_reason_codes") or ()),
    }
    actual = {
        "eligible": getattr(assessment, "eligible", None),
        "hard_block": getattr(assessment, "hard_block", None),
        "regime": getattr(assessment, "regime", None),
        "reason_codes": tuple(getattr(assessment, "reason_codes", ())),
    }
    if any(expected[key] is None for key in ("eligible", "hard_block", "regime")):
        raise ValueError("artifact sector decision projection is incomplete")
    return {
        "decision_recomputed": True,
        "decision_projection_unchanged": actual == expected,
        "artifact_sector_decision": expected,
        "current_sector_decision": actual,
    }


def _sample_frame_revision(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update(
        pd.util.hash_pandas_object(
            frame.reset_index(drop=True),
            index=False,
            categorize=False,
        )
        .to_numpy(dtype="uint64", copy=False)
        .tobytes()
    )
    digest.update(repr(tuple(sorted(frame.attrs.items()))).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _recompute_sector_decision(
    row: Mapping[str, object],
    *,
    root: Path,
    source: CurrentQmtGics3CompositeReplaySource,
    qmt_data_dir: Path,
    sector_id: str,
    sector_name: str,
    members: Sequence[str],
    requested_at: datetime,
    decision_at: datetime,
) -> dict[str, object]:
    five = source.five_minute_prefix(
        sector_id=sector_id,
        member_codes=members,
        observed_at=decision_at,
    )
    thirty = derive_qmt_sector_thirty_minute_frame(five)
    market, _ = read_qmt_local_derived_30m(
        data_dir=qmt_data_dir,
        code="SH.000001",
        start_at=requested_at,
        end_at=decision_at,
    )
    expected_closes = tuple(
        pd.Timestamp(value).to_pydatetime()
        for value in pd.to_datetime(
            market["time"],
            unit="ms",
            utc=True,
        ).dt.tz_convert("Asia/Shanghai")
    )
    hashes = recent_year_research_algorithm_hashes(root)
    revision = recent_year_research_algorithm_revision(hashes)
    facts = sector_facts_from_frame(
        sector_id=sector_id,
        sector_name=sector_name,
        member_count=len(members),
        frame=thirty,
        observed_times=(decision_at,),
        algorithm_revision=revision,
        source_revision=_sample_frame_revision(thirty),
        market_data_source=CURRENT_GICS3_COMPOSITE_PROVIDER,
        expected_closes=expected_closes,
    )
    if len(facts.assessments) != 1 or facts.assessments[0][0] != decision_at:
        raise ValueError("single-sector decision replay did not produce one assessment")
    return {
        **_sector_decision_projection(row, facts.assessments[0][1]),
        "current_algorithm_revision": revision,
        "five_minute_row_count": len(five),
        "thirty_minute_row_count": len(thirty),
        "confirmed_thirty_minute_point_count": len(facts.thirty_points),
        "direction_unavailable_count": facts.direction_unavailable_count,
    }


def _run_qmt_sample(
    artifact: Mapping[str, object],
    *,
    root: Path,
    pit_path: Path,
    catalog_path: Path,
    qmt_data_dir: Path,
    sample_symbol: str | None,
    sample_decision_at: datetime | None,
) -> dict[str, object]:
    row = _coverage_candidate(
        artifact,
        symbol=sample_symbol,
        decision_at=sample_decision_at,
    )
    warmup = _mapping(
        row["sector_risk_warmup_evidence"],
        "sector warmup evidence",
    )
    coverage = _mapping(
        warmup["strict_same_5m_source_coverage"],
        "sector same-base coverage",
    )
    decision = datetime.fromisoformat(str(row["decision_at"]))
    requested = datetime.fromisoformat(
        str(coverage["physical_source_requested_start_at"])
    )
    if decision.tzinfo is None or requested.tzinfo is None:
        raise ValueError("sample timestamps must be timezone-aware")

    input_hashes = _mapping(artifact.get("input_hashes"), "input_hashes")
    if _sha256_file(pit_path) != input_hashes.get("pit_snapshot"):
        raise ValueError("PIT snapshot does not match the replay artifact")
    if _sha256_file(catalog_path) != input_hashes.get("current_catalog_ledger"):
        raise ValueError("catalog ledger does not match the replay artifact")
    ledger = load_sector_ledger(catalog_path)
    entry, sector = _catalog_sector(artifact, ledger, str(row["sector_id"]))
    members = tuple(str(value) for value in _sequence(
        sector.get("member_codes"),
        "sector members",
    ))
    pit = PITMetadataIndex(load_snapshot(pit_path))
    source = CurrentQmtGics3CompositeReplaySource(
        data_dir=qmt_data_dir,
        start_at=requested,
        end_at=decision,
        factors_by_code={code: pit.factors_for(code) for code in members},
    )
    raw = source.five_minute_physical_source_coverage(
        sector_id=str(row["sector_id"]),
        member_codes=members,
        observed_at=decision,
    )
    projection = _validate_physical_projection(raw, coverage)
    decision_projection = _recompute_sector_decision(
        row,
        root=root,
        source=source,
        qmt_data_dir=qmt_data_dir,
        sector_id=str(row["sector_id"]),
        sector_name=str(row["sector_name"]),
        members=members,
        requested_at=requested,
        decision_at=decision,
    )
    return {
        "symbol": row["symbol"],
        "decision_at": row["decision_at"],
        "sector_id": row["sector_id"],
        "sector_name": row["sector_name"],
        "catalog_entry_sha256": entry["entry_sha256"],
        "catalog_member_count": len(members),
        "accepted": row.get("accepted"),
        "sector_risk_gate": row.get("sector_risk_gate"),
        **projection,
        **decision_projection,
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    started = perf_counter()
    root = args.root.resolve()
    artifact_path = _resolve(root, args.artifact)
    artifact = _load_json(artifact_path)
    artifact_started = perf_counter()
    artifact_receipt = _validate_artifact(
        artifact,
        root=root,
        allow_stale_source=args.allow_stale_source,
    )
    artifact_elapsed = perf_counter() - artifact_started
    source_current = bool(artifact_receipt["decision_source_matches_current"])

    release_started = perf_counter()
    release_path = (
        artifact_path.parent / "v3_release_manifest.json"
        if args.release_manifest is None
        else _resolve(root, args.release_manifest)
    )
    if release_path.is_file():
        release_receipt: dict[str, object] | None = (
            verify_sector_release_manifest(
                root=root,
                manifest_path=release_path,
                expected_artifact_path=artifact_path,
                require_current_algorithm=(
                    source_current or not args.allow_stale_source
                ),
            )
        )
        release_status = (
            "VERIFIED"
            if source_current
            else "VERIFIED_STALE_PUBLISHED_GRAPH_FOR_BOUNDED_SAMPLE"
        )
    elif source_current or not args.allow_stale_source:
        raise ValueError(
            "current release artifact requires v3_release_manifest.json"
        )
    else:
        # A stale candidate is inspected before its one allowed release replay;
        # it is not a published result yet and therefore has no release binding.
        release_receipt = None
        release_status = "NOT_YET_PUBLISHED_STALE_CANDIDATE"
    release_elapsed = perf_counter() - release_started

    sample_receipt: dict[str, object] | None = None
    sample_elapsed: float | None = None
    if args.qmt_local_data_dir is not None:
        sample_started = perf_counter()
        sample_receipt = _run_qmt_sample(
            artifact,
            root=root,
            pit_path=_release_input_path(
                root=root,
                explicit=args.pit_snapshot,
                release_receipt=release_receipt,
                key="pit_snapshot",
                fallback=DEFAULT_PIT,
            ),
            catalog_path=_release_input_path(
                root=root,
                explicit=args.catalog_ledger,
                release_receipt=release_receipt,
                key="current_catalog_ledger",
                fallback=DEFAULT_CATALOG,
            ),
            qmt_data_dir=args.qmt_local_data_dir.resolve(),
            sample_symbol=args.sample_symbol,
            sample_decision_at=args.sample_decision_at,
        )
        sample_elapsed = perf_counter() - sample_started
    elif args.require_qmt_sample:
        raise ValueError("--require-qmt-sample requires --qmt-local-data-dir")

    if (
        source_current
        and sample_receipt is not None
        and sample_receipt["decision_projection_unchanged"] is not True
    ):
        raise ValueError(
            "current artifact disagrees with the recomputed sector decision sample"
        )
    sample_changed = bool(
        sample_receipt is not None
        and sample_receipt["decision_projection_unchanged"] is not True
    )
    status = (
        "READY_CURRENT_ARTIFACT_AND_QMT_SAMPLE"
        if source_current and sample_receipt is not None
        else "READY_CURRENT_ARTIFACT_ONLY"
        if source_current
        else "SAMPLE_DECISION_CHANGED_REVIEW_REQUIRED"
        if sample_changed
        else "READY_FOR_SINGLE_RELEASE_REPLAY"
        if sample_receipt is not None
        else "SOURCE_CHANGED_QMT_SAMPLE_REQUIRED"
    )
    return {
        "schema": SCHEMA,
        "status": status,
        "artifact": {
            "path": artifact_path.relative_to(root).as_posix(),
            "file_sha256": _sha256_file(artifact_path),
            **artifact_receipt,
        },
        "release_manifest": {
            "status": release_status,
            "receipt": release_receipt,
        },
        "qmt_single_sector_sample": sample_receipt,
        "timing_seconds": {
            "artifact": round(artifact_elapsed, 6),
            "release_manifest": round(release_elapsed, 6),
            "qmt_single_sector_sample": (
                None if sample_elapsed is None else round(sample_elapsed, 6)
            ),
            "total": round(perf_counter() - started, 6),
        },
        "safety": {
            "full_replay_executed": False,
            "qmt_data_written": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "parameters_changed": False,
            "data_grade": "RESEARCH_ONLY",
            "live_status": "LIVE_DISABLED",
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        receipt = run(args)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "FAILED_CLOSED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "full_replay_executed": False,
                    "live_status": "LIVE_DISABLED",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
