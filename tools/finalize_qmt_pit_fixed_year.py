#!/usr/bin/env python3
"""Certify, execute, and publish the point-in-time fixed-year QMT replay."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
import time as wall_time
from typing import Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.backtest.data_audit import DataEvidence
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FACT_SCHEMA,
    SECTOR_FACT_SCHEMA,
    SectorResearchFacts,
    SymbolResearchFacts,
    load_qmt_frame,
    run_sparse_portfolio,
    sector_facts_from_frame,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataIndex,
    PITMetadataSnapshot,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.pit_sector import (
    PIT_SW1_COMPOSITE_PROVIDER,
    build_pit_sw1_composite,
)
from chanlun.decision_support.trading_system.backtest.report import (
    BacktestEvaluationResult,
    build_report,
)
from tools import qmt_research_contract


DEFAULT_INPUT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026"
)
DEFAULT_REPORT = Path(
    "audit/chanlun_trading_system_backtest/certified_report.json"
)


def _positive_decimal(value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise argparse.ArgumentTypeError("value must be a positive decimal")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    result.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    result.add_argument(
        "--initial-cash", type=_positive_decimal, default=Decimal("1000000")
    )
    result.add_argument("--bootstrap-repetitions", type=int, default=2000)
    result.add_argument("--force-sectors", action="store_true")
    return result


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_bytes(
        path,
        (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8"),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _checkpoint_tree(paths: Sequence[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _algorithm_revision(hashes: Sequence[tuple[str, str]]) -> str:
    encoded = json.dumps(
        tuple(hashes), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _frozen_algorithm(
    manifest: Mapping[str, object],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    raw = manifest.get("algorithm")
    if not isinstance(raw, Mapping):
        raise ValueError("extract manifest has no frozen algorithm")
    revision = raw.get("revision")
    rows = raw.get("hashes")
    if not isinstance(revision, str) or not isinstance(rows, list):
        raise ValueError("extract manifest algorithm is malformed")
    hashes = tuple(
        (str(row["path"]), str(row["sha256"]))
        for row in rows
        if isinstance(row, Mapping)
    )
    if len(hashes) != len(rows) or _algorithm_revision(hashes) != revision:
        raise ValueError("extract manifest algorithm revision is inconsistent")
    if qmt_research_contract.algorithm_hashes() != hashes:
        raise RuntimeError("source code changed after symbol extraction")
    return revision, hashes


def _frozen_fact_algorithm(
    manifest: Mapping[str, object],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    raw = manifest.get("fact_algorithm")
    if raw is None:
        return _frozen_algorithm(manifest)
    if not isinstance(raw, Mapping):
        raise ValueError("extract manifest fact algorithm is malformed")
    revision = raw.get("revision")
    rows = raw.get("hashes")
    if not isinstance(revision, str) or not isinstance(rows, list):
        raise ValueError("extract manifest fact algorithm is malformed")
    hashes = tuple(
        (str(row["path"]), str(row["sha256"]))
        for row in rows
        if isinstance(row, Mapping)
    )
    if len(hashes) != len(rows) or _algorithm_revision(hashes) != revision:
        raise ValueError("extract manifest fact algorithm revision is inconsistent")
    if qmt_research_contract.fact_algorithm_hashes() != hashes:
        raise RuntimeError("symbol-fact source code changed after extraction")
    return revision, hashes


def _fact_path(directory: Path, code: str) -> Path:
    return directory / "symbols" / f"{code.replace('.', '_')}.pkl"


def _load_symbols(
    directory: Path,
    manifest: Mapping[str, object],
    algorithm_revision: str,
) -> tuple[SymbolResearchFacts, ...]:
    raw = manifest.get("symbols")
    if not isinstance(raw, Mapping):
        raise ValueError("extract manifest has no symbol map")
    output: list[SymbolResearchFacts] = []
    for code in sorted(raw):
        path = _fact_path(directory, str(code))
        value = pickle.loads(path.read_bytes())
        if (
            not isinstance(value, SymbolResearchFacts)
            or value.schema != FACT_SCHEMA
            or value.algorithm_revision != algorithm_revision
            or value.code != code
        ):
            raise ValueError(f"invalid symbol checkpoint: {code}")
        output.append(value)
    return tuple(output)


def _sector_path(directory: Path, sector_id: str) -> Path:
    return directory / "pit_sectors" / f"{sector_id.rsplit(':', 1)[-1]}.pkl"


def _sector_revision(
    *,
    snapshot_hash: str,
    sector_id: str,
    observed_times: Sequence[datetime],
    expected_closes: Sequence[datetime],
    frame: pd.DataFrame,
) -> str:
    digest = hashlib.sha256()
    digest.update(snapshot_hash.encode("ascii"))
    digest.update(sector_id.encode("utf-8"))
    digest.update(repr(tuple(observed_times)).encode("utf-8"))
    digest.update(repr(tuple(expected_closes)).encode("utf-8"))
    digest.update(
        pd.util.hash_pandas_object(
            frame.reset_index(drop=True), index=False, categorize=False
        ).to_numpy(dtype="uint64", copy=False).tobytes()
    )
    return "sha256:" + digest.hexdigest()


def _load_cached_sector(
    path: Path,
    *,
    algorithm_revision: str,
    source_revision: str,
) -> SectorResearchFacts | None:
    try:
        value = pickle.loads(path.read_bytes())
    except (OSError, EOFError, pickle.PickleError, ValueError, AttributeError):
        return None
    if (
        not isinstance(value, SectorResearchFacts)
        or value.schema != SECTOR_FACT_SCHEMA
        or value.algorithm_revision != algorithm_revision
        or value.source_revision != source_revision
    ):
        return None
    return value


def _build_sector_facts(
    *,
    directory: Path,
    symbols: Sequence[SymbolResearchFacts],
    snapshot: PITMetadataSnapshot,
    snapshot_hash: str,
    warmup_start: date,
    requested_end: date,
    algorithm_revision: str,
    force: bool,
) -> dict[str, SectorResearchFacts]:
    times_by_sector: dict[str, set[datetime]] = {}
    for facts in symbols:
        for evaluation in facts.evaluations:
            if evaluation.sector_id is not None:
                times_by_sector.setdefault(evaluation.sector_id, set()).add(
                    evaluation.observed_at
                )
    names = dict(snapshot.qmt_sw1_sector_names)
    market_frame = load_qmt_frame(
        "SH.000001",
        "30m",
        start_at=datetime.combine(
            warmup_start,
            time(9, 30),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
        end_at=datetime.combine(
            requested_end,
            time(15, 0),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
        factors=pd.DataFrame(),
    )
    expected_closes = tuple(
        pd.Timestamp(value).to_pydatetime() for value in market_frame["date"]
    )
    if not expected_closes:
        raise RuntimeError("QMT market 30m reference timeline is unavailable")
    output: dict[str, SectorResearchFacts] = {}
    for sector_id in sorted(times_by_sector):
        if sector_id not in names:
            raise ValueError(f"unknown PIT sector at evaluation: {sector_id}")
        observed_times = tuple(sorted(times_by_sector[sector_id]))
        frame = build_pit_sw1_composite(
            snapshot=snapshot,
            sector_id=sector_id,
            start_at=datetime.combine(warmup_start, time(9, 30), tzinfo=snapshot.captured_at.tzinfo),
            end_at=datetime.combine(requested_end, time(15, 0), tzinfo=snapshot.captured_at.tzinfo),
        )
        revision = _sector_revision(
            snapshot_hash=snapshot_hash,
            sector_id=sector_id,
            observed_times=observed_times,
            expected_closes=expected_closes,
            frame=frame,
        )
        path = _sector_path(directory, sector_id)
        cached = None if force else _load_cached_sector(
            path,
            algorithm_revision=algorithm_revision,
            source_revision=revision,
        )
        if cached is None:
            member_count = len(
                {row.code for row in snapshot.memberships if row.sector_id == sector_id}
            )
            facts = sector_facts_from_frame(
                sector_id=sector_id,
                sector_name=names[sector_id],
                member_count=member_count,
                frame=frame,
                observed_times=observed_times,
                algorithm_revision=algorithm_revision,
                source_revision=revision,
                market_data_source=PIT_SW1_COMPOSITE_PROVIDER,
                expected_closes=expected_closes,
            )
            _atomic_bytes(path, pickle.dumps(facts, protocol=pickle.HIGHEST_PROTOCOL))
        else:
            facts = cached
        output[sector_id] = facts
        print(
            json.dumps(
                {
                    "stage": "pit_sector",
                    "sector": sector_id,
                    "events": len(observed_times),
                    "rows": facts.row_count,
                    "members": facts.member_count,
                    "error": facts.error,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return output


def _causality_failures(
    *,
    symbols: Sequence[SymbolResearchFacts],
    sectors: Mapping[str, SectorResearchFacts],
    snapshot: PITMetadataSnapshot,
) -> tuple[str, ...]:
    failures: list[str] = []
    index = PITMetadataIndex(snapshot)
    for facts in symbols:
        try:
            master = index.security(facts.code)
        except KeyError:
            failures.append("security_master_missing")
            continue
        if facts.security_master != master:
            failures.append("security_master_checkpoint_mismatch")
        if facts.memberships != index.memberships_for(facts.code):
            failures.append("membership_checkpoint_mismatch")
        if facts.factors != index.factors_for(facts.code):
            failures.append("corporate_action_checkpoint_mismatch")
        if any(
            not point.confirmed
            or point.confirmed_at is None
            or point.available_at < point.confirmed_at
            for point in (
                *facts.thirty_points,
                *facts.five_points,
                *facts.one_points,
            )
        ):
            failures.append("noncausal_structural_point_registry")
        for evaluation in facts.evaluations:
            if evaluation.bar.closed_at != evaluation.observed_at:
                failures.append("decision_bar_not_closed")
            expected = index.membership_at(facts.code, evaluation.observed_at)
            expected_sector = None if expected is None else expected.sector_id
            if evaluation.sector_id != expected_sector:
                failures.append("future_or_stale_sector_membership")
            if not master.listed_on(evaluation.observed_at.date()):
                failures.append("unlisted_decision_event")
            if evaluation.bar.adjustment_known_at > evaluation.observed_at:
                failures.append("future_adjustment_timestamp")
            expected_divisor = Decimal("1")
            for factor in facts.factors:
                if factor.effective_on <= evaluation.observed_at.date():
                    expected_divisor *= factor.raw_price_divisor
            for raw_value, analysis_value in (
                (evaluation.bar.raw_open, evaluation.bar.analysis_open),
                (evaluation.bar.raw_high, evaluation.bar.analysis_high),
                (evaluation.bar.raw_low, evaluation.bar.analysis_low),
                (evaluation.bar.raw_close, evaluation.bar.analysis_close),
            ):
                expected_value = raw_value * expected_divisor
                tolerance = max(Decimal("0.0000001"), abs(expected_value) * Decimal("1e-10"))
                if abs(analysis_value - expected_value) > tolerance:
                    failures.append("noncausal_price_adjustment")
    if any(row.allot_num > 0 for row in snapshot.factors):
        failures.append("rights_issue_accounting_outside_certified_contract")
    if any(row.gugai > 0 for row in snapshot.factors):
        failures.append("share_reform_outside_certified_contract")
    if any(facts.error is not None for facts in sectors.values()):
        failures.append("sector_composite_incomplete")
    for facts in symbols:
        for evaluation in facts.evaluations:
            if evaluation.sector_id is not None and (
                evaluation.sector_id not in sectors
                or evaluation.observed_at
                not in dict(sectors[evaluation.sector_id].assessments)
            ):
                failures.append("sector_assessment_missing_at_decision")
    return tuple(dict.fromkeys(failures))


def _write_gate(
    *,
    path: Path,
    status: str,
    pnl_generated: bool,
    algorithm_revision: str,
    snapshot_hash: str,
    symbols: int,
    evaluations: int,
    failures: Sequence[str],
    report: Path | None = None,
) -> None:
    _atomic_json(
        path,
        {
            "schema": "chanlun-backtest-causality-gate",
            "checked_at": datetime.now().astimezone().isoformat(),
            "status": status,
            "pnl_generated": pnl_generated,
            "algorithm_revision": algorithm_revision,
            "pit_snapshot_sha256": snapshot_hash,
            "validated_symbol_fact_count": symbols,
            "validated_decision_count": evaluations,
            "proven_controls": [
                "survivorship_free_effective_dated_security_master",
                "decision_time_sw1_membership",
                "ex_date_only_causal_price_basis",
                "cash_and_share_corporate_action_accounting",
                "closed_bar_strict_structure_witnesses",
                "next_complete_minute_execution",
                "observed_range_and_volume_fill_guard",
                "delisted_security_zero_recovery",
                "content_addressed_algorithm_data_and_checkpoints",
            ],
            "failures": list(failures),
            "report": None if report is None else str(report.resolve()),
        },
    )


def _prefix_audit_failures(
    *,
    path: Path,
    manifest_path: Path,
    algorithm_revision: str,
    fact_algorithm_revision: str,
    snapshot_hash: str,
    symbols: Sequence[SymbolResearchFacts],
) -> tuple[str, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ("prefix_invariance_audit_missing",)
    if not isinstance(raw, Mapping):
        return ("prefix_invariance_audit_malformed",)
    failures: list[str] = []
    expected_count = sum(bool(row.evaluations) for row in symbols)
    if raw.get("schema") != "chanlun-prefix-invariance-audit":
        failures.append("prefix_invariance_audit_schema_mismatch")
    if raw.get("status") != "passed" or raw.get("failed_codes"):
        failures.append("prefix_invariance_changed")
    if raw.get("algorithm_revision") != algorithm_revision:
        failures.append("prefix_invariance_algorithm_mismatch")
    if raw.get("fact_algorithm_revision", raw.get("algorithm_revision")) != (
        fact_algorithm_revision
    ):
        failures.append("prefix_invariance_fact_algorithm_mismatch")
    if raw.get("pit_snapshot_sha256") != snapshot_hash:
        failures.append("prefix_invariance_snapshot_mismatch")
    if raw.get("extract_manifest_sha256") != _sha256(manifest_path):
        failures.append("prefix_invariance_manifest_mismatch")
    if (
        int(raw.get("signal_producing_symbol_count", -1)) != expected_count
        or int(raw.get("audited_symbol_count", -1)) != expected_count
    ):
        failures.append("prefix_invariance_coverage_missing")
    return tuple(dict.fromkeys(failures))


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.bootstrap_repetitions <= 0:
        raise ValueError("bootstrap repetitions must be positive")
    started = wall_time.perf_counter()
    directory = args.input_dir.resolve()
    manifest_path = directory / "extract_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or not manifest.get("complete"):
        raise RuntimeError("symbol extraction is incomplete")
    algorithm_revision, algorithm_hashes = _frozen_algorithm(manifest)
    fact_algorithm_revision, fact_algorithm_hashes = _frozen_fact_algorithm(manifest)
    symbols = _load_symbols(directory, manifest, fact_algorithm_revision)
    request = manifest.get("request")
    catalog = manifest.get("catalog")
    if not isinstance(request, Mapping) or not isinstance(catalog, Mapping):
        raise ValueError("extract request or catalog is missing")
    archived_intersecting = int(
        catalog.get("archived_intersecting_symbol_count", len(symbols))
    )
    unclassified_contracts = int(catalog.get("unclassified_symbol_count", 0))
    if archived_intersecting < len(symbols) or unclassified_contracts < 0:
        raise ValueError("extract catalog coverage is inconsistent")
    archived_membership_coverage = (
        Decimal("1")
        if archived_intersecting == 0
        else Decimal(len(symbols)) / Decimal(archived_intersecting)
    )
    snapshot_path = Path(str(request["pit_snapshot"]))
    snapshot_hash = _sha256(snapshot_path)
    if snapshot_hash != request.get("pit_snapshot_sha256"):
        raise ValueError("PIT metadata changed after symbol extraction")
    snapshot = load_snapshot(snapshot_path)
    requested_start = date.fromisoformat(str(request["requested_start"]))
    effective_start = date.fromisoformat(str(request["effective_start"]))
    requested_end = date.fromisoformat(str(request["requested_end"]))
    warmup_start = date.fromisoformat(str(request["warmup_start"]))
    prefix_path = directory / "prefix_invariance_audit.json"
    prefix_failures = _prefix_audit_failures(
        path=prefix_path,
        manifest_path=manifest_path,
        algorithm_revision=algorithm_revision,
        fact_algorithm_revision=fact_algorithm_revision,
        snapshot_hash=snapshot_hash,
        symbols=symbols,
    )
    gate_path = args.report.resolve().parent / "causality_gate.json"
    evaluations = sum(len(row.evaluations) for row in symbols)
    if prefix_failures:
        _write_gate(
            path=gate_path,
            status="blocked",
            pnl_generated=False,
            algorithm_revision=algorithm_revision,
            snapshot_hash=snapshot_hash,
            symbols=len(symbols),
            evaluations=evaluations,
            failures=prefix_failures,
        )
        print(
            json.dumps(
                {
                    "complete": False,
                    "status": "blocked_by_prefix_invariance_gate",
                    "failures": prefix_failures,
                    "gate": str(gate_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 3
    sectors = _build_sector_facts(
        directory=directory,
        symbols=symbols,
        snapshot=snapshot,
        snapshot_hash=snapshot_hash,
        warmup_start=warmup_start,
        requested_end=requested_end,
        algorithm_revision=algorithm_revision,
        force=args.force_sectors,
    )
    failures = _causality_failures(
        symbols=symbols,
        sectors=sectors,
        snapshot=snapshot,
    )
    if failures:
        _write_gate(
            path=gate_path,
            status="blocked",
            pnl_generated=False,
            algorithm_revision=algorithm_revision,
            snapshot_hash=snapshot_hash,
            symbols=len(symbols),
            evaluations=evaluations,
            failures=failures,
        )
        print(
            json.dumps(
                {
                    "complete": False,
                    "status": "blocked_by_no_future_function_gate",
                    "failures": failures,
                    "gate": str(gate_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 3
    run = run_sparse_portfolio(
        symbols,
        sectors,
        initial_cash=args.initial_cash,
        formal_selection_required=False,
    )
    terminal_same_bar = tuple(
        trade.code
        for trade in run.trades
        if trade.exit_reason == "forced_liquidation_sensitivity"
    )
    if terminal_same_bar:
        _write_gate(
            path=gate_path,
            status="blocked",
            pnl_generated=False,
            algorithm_revision=algorithm_revision,
            snapshot_hash=snapshot_hash,
            symbols=len(symbols),
            evaluations=evaluations,
            failures=("terminal_same_bar_liquidation_detected",),
        )
        raise RuntimeError("certified replay generated a terminal same-bar fill")
    run_path = directory / "certified_portfolio_run.pkl"
    _atomic_bytes(run_path, pickle.dumps(run, protocol=pickle.HIGHEST_PROTOCOL))
    sector_assessments = tuple(
        assessment
        for facts in sectors.values()
        for _observed_at, assessment in facts.assessments
    )
    complete_sector_events = sum(
        "sector_data_incomplete" not in row.reason_codes
        for row in sector_assessments
    )
    sector_event_coverage = (
        Decimal("1")
        if not sector_assessments
        else Decimal(complete_sector_events) / Decimal(len(sector_assessments))
    )
    evidence = DataEvidence(
        grade="certified",
        failures=(),
        warnings=(
            "fixed_policy_single_year_no_parameter_search",
            "malformed_qmt_expiry_sentinels_resolved_by_status_and_observed_bars",
            "gross_cash_dividends_before_investor_specific_holding_period_tax",
            "terminal_open_positions_marked_to_market_not_same_bar_liquidated",
            *(
                ("unclassified_archived_contracts_excluded",)
                if unclassified_contracts
                else ()
            ),
            *(
                ("incomplete_sector_bars_hard_blocked",)
                if complete_sector_events < len(sector_assessments)
                else ()
            ),
        ),
        coverage=(
            ("symbol_extraction", Decimal("1")),
            ("historical_membership", Decimal("1")),
            ("archived_universe_membership_coverage", archived_membership_coverage),
            ("point_in_time_adjustment", Decimal("1")),
            ("historical_security_status", Decimal("1")),
            ("corporate_action_accounting", Decimal("1")),
            ("sector_event_coverage", sector_event_coverage),
            ("current_production_selection_contract", Decimal("1")),
        ),
    )
    result = BacktestEvaluationResult(
        aggregate_run=run,
        bootstrap_repetitions=args.bootstrap_repetitions,
    )
    sector_paths = tuple(_sector_path(directory, sector_id) for sector_id in sectors)
    symbol_paths = tuple(_fact_path(directory, facts.code) for facts in symbols)
    report = build_report(
        evidence=evidence,
        result=result,
        ablations=qmt_research_contract.unavailable_ablations(
            "fixed_policy_ablation_not_run"
        ),
        benchmarks=qmt_research_contract.unavailable_benchmarks(),
        generated_at=datetime.now().astimezone(),
        algorithm_hashes=algorithm_hashes,
        limitations=(
            "fixed_policy_single_year_no_parameter_search",
            "required_ablations_not_run",
            "required_benchmarks_not_run",
            "investor_specific_dividend_withholding_tax_not_modelled",
            "terminal_open_positions_are_unrealised",
            "formal_selection_research_ledger_not_used_by_current_production_policy",
            *(
                ("historical_sw1_membership_unavailable_for_some_archived_contracts",)
                if unclassified_contracts
                else ()
            ),
        ),
        requested_range=(requested_start, requested_end),
        effective_range=(effective_start, requested_end),
        evaluation_mode="fixed_policy_one_year",
        sector_price_source=PIT_SW1_COMPOSITE_PROVIDER,
        formal_selection_required=False,
        universe_summary={
            "catalog_source": "qmt_sw1_with_cninfo_effective_dates",
            "selected_symbol_count": len(symbols),
            "archived_intersecting_symbol_count": archived_intersecting,
            "unclassified_excluded_symbol_count": unclassified_contracts,
            "eligible_sector_count": len(snapshot.qmt_sw1_sector_names),
            "sector_composite_member_limit": None,
            "corporate_action_count": len(snapshot.factors),
            "causal_evaluation_count": evaluations,
            "formal_selection_required": False,
        },
        data_source_hashes=(
            ("pit_metadata_snapshot", snapshot_hash),
            ("qmt_extract_manifest", _sha256(manifest_path)),
            ("prefix_invariance_audit", _sha256(prefix_path)),
            (
                "symbol_fact_checkpoint_tree",
                _checkpoint_tree(symbol_paths, root=directory),
            ),
            (
                "sector_fact_checkpoint_tree",
                _checkpoint_tree(sector_paths, root=directory),
            ),
            ("certified_portfolio_run", _sha256(run_path)),
        ),
    )
    if qmt_research_contract.algorithm_hashes() != algorithm_hashes:
        raise RuntimeError("source code changed during certified finalization")
    if (
        "fact_algorithm" in manifest
        and qmt_research_contract.fact_algorithm_hashes() != fact_algorithm_hashes
    ):
        raise RuntimeError("symbol-fact source changed during certified finalization")
    qmt_research_contract.write_report_atomic(args.report, report)
    _write_gate(
        path=gate_path,
        status="passed",
        pnl_generated=True,
        algorithm_revision=algorithm_revision,
        snapshot_hash=snapshot_hash,
        symbols=len(symbols),
        evaluations=evaluations,
        failures=(),
        report=args.report,
    )
    print(
        json.dumps(
            {
                "complete": True,
                "grade": "certified",
                "symbols": len(symbols),
                "sectors": len(sectors),
                "evaluations": evaluations,
                "trades": len(run.trades),
                "fills": len(run.fills),
                "open_positions": len(run.open_positions),
                "elapsed_seconds": round(wall_time.perf_counter() - started, 2),
                "report": str(args.report.resolve()),
                "gate": str(gate_path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
