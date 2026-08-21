#!/usr/bin/env python3
"""Finalize sector gates, sparse portfolio execution, and the one-year report."""

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
from zoneinfo import ZoneInfo


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
    run_sparse_portfolio,
    sector_facts_from_frame,
    unavailable_sector_facts,
)
from chanlun.decision_support.trading_system.backtest.report import (
    BacktestEvaluationResult,
    build_report,
)
from chanlun.decision_support.trading_system.qmt_sector_same_base import (
    derive_qmt_sector_thirty_minute_frame,
)
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_GICS3_COMPOSITE_MEMBER_LIMIT,
    QmtSectorCompositeSource,
    build_qmt_gics3_sector_catalog,
)
from tools import qmt_research_contract


CN = ZoneInfo("Asia/Shanghai")


def _positive_decimal(value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise argparse.ArgumentTypeError("value must be a positive decimal")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--input-dir",
        type=Path,
        default=Path("audit/chanlun_trading_system_backtest/fixed_year_2025_2026"),
    )
    result.add_argument(
        "--report",
        type=Path,
        default=Path("audit/chanlun_trading_system_backtest/research_report.json"),
    )
    result.add_argument(
        "--initial-cash",
        type=_positive_decimal,
        default=Decimal("1000000"),
    )
    result.add_argument("--bootstrap-repetitions", type=int, default=2000)
    result.add_argument("--allow-partial", action="store_true")
    result.add_argument("--force-sectors", action="store_true")
    result.add_argument(
        "--selection-research",
        type=Path,
        default=None,
        help="正式研究账本；默认读取输入目录下的 selection_research.json",
    )
    result.add_argument(
        "--allow-research-only",
        action="store_true",
        help=(
            "explicitly permit P&L despite unverified point-in-time universe, "
            "membership, status, or corporate-action evidence"
        ),
    )
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
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_bytes(path, encoded)


_CERTIFICATION_FAILURES = (
    {
        "code": "survivorship_free_universe_unverified",
        "evidence": (
            "the QMT GICS3 catalog is captured after the test period and has "
            "not been reconciled to an archived start-date security master"
        ),
        "required": "an immutable start-date universe including later delistings",
    },
    {
        "code": "historical_sector_membership_unverified",
        "evidence": (
            "local QMT historical GICS3 queries returned future IPO members "
            "for dates before those IPOs"
        ),
        "required": "archived effective-dated GICS3 membership records",
    },
    {
        "code": "historical_security_status_missing",
        "evidence": "the sparse replay currently synthesizes listed/ST/suspension flags",
        "required": "effective-dated listing, ST, suspension and price-limit status",
    },
    {
        "code": "corporate_action_accounting_missing",
        "evidence": (
            "raw unadjusted bars are causal, but cash dividends and share "
            "multipliers are not yet applied to held positions"
        ),
        "required": "effective-dated cash/share corporate-action ledger",
    },
)


def _write_causality_gate(
    *,
    directory: Path,
    algorithm_revision: str,
    symbol_count: int,
    published_path: Path | None = None,
    extra_failures: Sequence[Mapping[str, str]] = (),
) -> Path:
    path = directory / "causality_gate.json"
    payload = {
        "schema": "chanlun-backtest-causality-gate",
        "checked_at": datetime.now().astimezone().isoformat(),
        "status": "blocked",
        "pnl_generated": False,
        "algorithm_revision": algorithm_revision,
        "validated_symbol_fact_count": symbol_count,
        "proven_controls": (
            "raw_unadjusted_bar_inputs",
            "strict_structure_causal_lock_witnesses",
            "close_timestamped_full_ohlcv_execution",
            "algorithm_and_source_fingerprinted_checkpoints",
        ),
        "failures": (*_CERTIFICATION_FAILURES, *extra_failures),
    }
    _atomic_json(path, payload)
    if published_path is not None and published_path.resolve() != path.resolve():
        _atomic_json(published_path, payload)
    return path


def _load_pickle(path: Path, expected: type):
    value = pickle.loads(path.read_bytes())
    if not isinstance(value, expected):
        raise TypeError(f"unexpected checkpoint type: {path}")
    return value


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
    payload = json.dumps(
        tuple(hashes),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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
    hashes: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("extract manifest algorithm hash is malformed")
        path, digest = row.get("path"), row.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise ValueError("extract manifest algorithm hash is malformed")
        hashes.append((path, digest))
    frozen = tuple(hashes)
    if _algorithm_revision(frozen) != revision:
        raise ValueError("extract manifest algorithm revision is inconsistent")
    if qmt_research_contract.algorithm_hashes() != frozen:
        raise RuntimeError("source code changed after symbol extraction")
    return revision, frozen


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
    hashes: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("extract manifest fact algorithm hash is malformed")
        path, digest = row.get("path"), row.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise ValueError("extract manifest fact algorithm hash is malformed")
        hashes.append((path, digest))
    frozen = tuple(hashes)
    if _algorithm_revision(frozen) != revision:
        raise ValueError("extract manifest fact algorithm revision is inconsistent")
    if qmt_research_contract.fact_algorithm_hashes() != frozen:
        raise RuntimeError("symbol-fact source code changed after extraction")
    return revision, frozen


def _sector_source_revision(
    *,
    sector_id: str,
    members: Sequence[str],
    observed_times: Sequence[datetime],
    frame=None,
    error: str | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "sector_id": sector_id,
                "members": tuple(members),
                "observed_times": tuple(value.isoformat() for value in observed_times),
                "error": error,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if frame is not None:
        digest.update(frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))
        digest.update(
            json.dumps(
                dict(sorted(frame.attrs.items())),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return "sha256:" + digest.hexdigest()


def _catalog_rows(raw: Mapping[str, object]) -> dict[str, dict[str, object]]:
    rows = raw.get("sectors")
    if not isinstance(rows, list):
        raise ValueError("QMT catalog has no sectors")
    output: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sector_id = row.get("sector_id")
        name = row.get("name")
        members = row.get("member_codes")
        if (
            isinstance(sector_id, str)
            and isinstance(name, str)
            and isinstance(members, list)
            and len(members) >= 8
        ):
            output[sector_id] = {
                "sector_id": sector_id,
                "name": name,
                "members": tuple(
                    sorted(value for value in members if isinstance(value, str))
                ),
            }
    return output


def _load_symbols(
    directory: Path,
    manifest: Mapping[str, object],
    algorithm_revision: str,
) -> tuple[SymbolResearchFacts, ...]:
    raw = manifest.get("symbols")
    if not isinstance(raw, Mapping):
        raise ValueError("extract manifest has no symbol checkpoints")
    output: list[SymbolResearchFacts] = []
    for code in sorted(raw):
        path = directory / "symbols" / f"{code.replace('.', '_')}.pkl"
        facts = _load_pickle(path, SymbolResearchFacts)
        if (
            facts.schema != FACT_SCHEMA
            or facts.code != code
            or facts.algorithm_revision != algorithm_revision
        ):
            raise ValueError(f"symbol checkpoint identity mismatch: {code}")
        output.append(facts)
    return tuple(output)


def _sector_path(directory: Path, sector_id: str) -> Path:
    return directory / "sectors" / f"{sector_id.replace(':', '_')}.pkl"


def _sector_facts(
    *,
    directory: Path,
    symbols: tuple[SymbolResearchFacts, ...],
    catalog: Mapping[str, dict[str, object]],
    requested_end: date,
    force: bool,
    algorithm_revision: str,
) -> dict[str, SectorResearchFacts]:
    event_times: dict[str, set[datetime]] = {}
    for facts in symbols:
        event_times.setdefault(facts.sector_id, set()).update(
            row.observed_at for row in facts.evaluations
        )
    source = QmtSectorCompositeSource()
    output: dict[str, SectorResearchFacts] = {}
    for ordinal, sector_id in enumerate(sorted(event_times), start=1):
        row = catalog.get(sector_id)
        if row is None:
            raise ValueError(f"sector disappeared from QMT catalog: {sector_id}")
        name = str(row["name"])
        members = tuple(row["members"])
        times = tuple(sorted(event_times[sector_id]))
        path = _sector_path(directory, sector_id)
        existing = None
        if path.exists() and not force:
            candidate = _load_pickle(path, SectorResearchFacts)
            if (
                candidate.schema == SECTOR_FACT_SCHEMA
                and candidate.algorithm_revision == algorithm_revision
                and candidate.sector_id == sector_id
                and tuple(value for value, _assessment in candidate.assessments)
                == times
            ):
                existing = candidate
        if existing is not None:
            facts = existing
        else:
            try:
                five_minute = source.frame(
                    sector_id=sector_id,
                    sector_name=name,
                    members=members,
                    frequency="5m",
                    as_of=datetime.combine(
                        requested_end,
                        time(15, 0),
                        tzinfo=CN,
                    ),
                    request_bars=4000 * 6 + 47,
                )
                frame = derive_qmt_sector_thirty_minute_frame(
                    five_minute,
                    request_bars=4000,
                )
                source_revision = _sector_source_revision(
                    sector_id=sector_id,
                    members=members,
                    observed_times=times,
                    frame=frame,
                )
                facts = sector_facts_from_frame(
                    sector_id=sector_id,
                    sector_name=name,
                    member_count=len(members),
                    frame=frame,
                    observed_times=times,
                    algorithm_revision=algorithm_revision,
                    source_revision=source_revision,
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}:{exc}"
                facts = unavailable_sector_facts(
                    sector_id=sector_id,
                    sector_name=name,
                    member_count=len(members),
                    observed_times=times,
                    reason=reason,
                    algorithm_revision=algorithm_revision,
                    source_revision=_sector_source_revision(
                        sector_id=sector_id,
                        members=members,
                        observed_times=times,
                        error=reason,
                    ),
                )
            _atomic_bytes(path, pickle.dumps(facts, protocol=pickle.HIGHEST_PROTOCOL))
        output[sector_id] = facts
        print(
            json.dumps(
                {
                    "sector": ordinal,
                    "sector_total": len(event_times),
                    "sector_id": sector_id,
                    "events": len(times),
                    "rows": facts.row_count,
                    "error": facts.error,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return output


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.bootstrap_repetitions <= 0:
        raise ValueError("bootstrap repetitions must be positive")
    directory = args.input_dir.resolve()
    manifest_path = directory / "extract_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("extract manifest must be a JSON object")
    if not manifest.get("complete") and not args.allow_partial:
        raise RuntimeError("symbol extraction is incomplete")
    algorithm_revision, algorithm_hashes = _frozen_algorithm(manifest)
    fact_algorithm_revision, fact_algorithm_hashes = _frozen_fact_algorithm(manifest)
    symbols = _load_symbols(directory, manifest, fact_algorithm_revision)
    if not symbols:
        raise RuntimeError("no symbol facts are available")
    if not args.allow_research_only:
        published_gate_path = (
            args.report.resolve().parent / "causality_gate.json"
        )
        gate_path = _write_causality_gate(
            directory=directory,
            algorithm_revision=algorithm_revision,
            symbol_count=len(symbols),
            published_path=published_gate_path,
        )
        print(
            json.dumps(
                {
                    "complete": False,
                    "status": "blocked_by_no_future_function_gate",
                    "pnl_generated": False,
                    "failures": [row["code"] for row in _CERTIFICATION_FAILURES],
                    "gate": str(gate_path.resolve()),
                    "published_gate": str(published_gate_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 3
    research_path = (
        args.selection_research.resolve()
        if args.selection_research is not None
        else directory / "selection_research.json"
    )
    try:
        research_snapshots, selection_research_by_code = (
            qmt_research_contract.load_selection_research_ledger(
                research_path,
                replay_symbols={row.code for row in symbols},
            )
        )
    except ValueError as exc:
        failure = {
            "code": "formal_selection_research_ledger_missing_or_invalid",
            "evidence": str(exc),
            "required": "按决策时点有序且字段完整的正式研究账本",
        }
        gate_path = _write_causality_gate(
            directory=directory,
            algorithm_revision=algorithm_revision,
            symbol_count=len(symbols),
            published_path=args.report.resolve().parent / "causality_gate.json",
            extra_failures=(failure,),
        )
        print(
            json.dumps(
                {
                    "complete": False,
                    "status": "blocked_by_formal_selection_research_gate",
                    "pnl_generated": False,
                    "failures": [failure["code"]],
                    "gate": str(gate_path.resolve()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 3
    request = manifest.get("request")
    if not isinstance(request, Mapping):
        raise ValueError("extract request is unavailable")
    requested_start = date.fromisoformat(str(request["requested_start"]))
    effective_start = date.fromisoformat(str(request["effective_start"]))
    requested_end = date.fromisoformat(str(request["requested_end"]))
    snapshot_path = directory / "catalog_snapshot.json"
    raw_catalog = (
        json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot_path.exists()
        else build_qmt_gics3_sector_catalog()
    )
    if not isinstance(raw_catalog, Mapping):
        raise ValueError("catalog snapshot is invalid")
    expected_revision = manifest.get("catalog", {}).get("catalog_revision")
    if raw_catalog.get("catalog_revision") != expected_revision:
        raise ValueError("QMT catalog revision changed after symbol extraction")
    catalog = _catalog_rows(raw_catalog)
    started = wall_time.perf_counter()
    sectors = _sector_facts(
        directory=directory,
        symbols=symbols,
        catalog=catalog,
        requested_end=requested_end,
        force=args.force_sectors,
        algorithm_revision=algorithm_revision,
    )
    run = run_sparse_portfolio(
        symbols,
        sectors,
        initial_cash=args.initial_cash,
        selection_research_by_code=selection_research_by_code,
    )
    run_path = directory / "portfolio_run.pkl"
    _atomic_bytes(run_path, pickle.dumps(run, protocol=pickle.HIGHEST_PROTOCOL))
    completed = int(manifest["summary"]["completed_symbol_count"])
    selected = int(manifest["summary"]["selected_symbol_count"])
    symbols_with_market_data = sum(
        any(count > 0 for _frequency, count in facts.row_counts)
        for facts in symbols
    )
    symbols_without_market_data = (
        len(symbols) - symbols_with_market_data
    )
    sector_failures = sum(facts.error is not None for facts in sectors.values())
    evidence = DataEvidence(
        grade="research_only",
        failures=(
            "survivorship_free_universe_unverified",
            "historical_sector_membership_unverified",
            "historical_security_status_missing",
            "corporate_action_accounting_missing",
        ),
        warnings=(
            "current_qmt_gics3_membership_survivorship_bias",
            "raw_prices_include_corporate_action_discontinuities",
            "uniform_one_minute_warmup_excluded_before_effective_start",
            "sector_composite_uses_deterministic_member_sample",
            *(
                ("qmt_catalog_members_without_market_history",)
                if symbols_without_market_data
                else ()
            ),
        ),
        coverage=(
            ("symbol_extraction", Decimal(completed) / Decimal(selected)),
            (
                "market_data_available",
                Decimal(symbols_with_market_data) / Decimal(selected),
            ),
            (
                "sector_event_coverage",
                Decimal(len(sectors) - sector_failures) / Decimal(max(1, len(sectors))),
            ),
            ("historical_membership", Decimal("0")),
            ("point_in_time_adjustment", Decimal("1")),
            ("historical_security_status", Decimal("0")),
            (
                "formal_selection_research_symbols",
                Decimal(len(selection_research_by_code)) / Decimal(selected),
            ),
        ),
    )
    result = BacktestEvaluationResult(
        aggregate_run=run,
        bootstrap_repetitions=args.bootstrap_repetitions,
    )
    limitations = (
        "fixed_policy_single_year_no_parameter_search",
        "current_qmt_gics3_membership_survivorship_bias",
        "raw_price_corporate_action_accounting_missing",
        "historical_security_status_unavailable",
        "sector_composite_uses_deterministic_24_member_sample",
        *(
            ("qmt_catalog_members_without_market_history",)
            if symbols_without_market_data
            else ()
        ),
        "one_minute_uniform_warmup_until_2025_08_01",
        "required_ablations_not_run",
        "required_benchmarks_not_run",
    )
    report = build_report(
        evidence=evidence,
        result=result,
        ablations=qmt_research_contract.unavailable_ablations(
            "fixed_policy_ablation_not_run"
        ),
        benchmarks=qmt_research_contract.unavailable_benchmarks(),
        generated_at=datetime.now().astimezone(),
        algorithm_hashes=algorithm_hashes,
        limitations=limitations,
        requested_range=(requested_start, requested_end),
        effective_range=(effective_start, requested_end),
        evaluation_mode="fixed_policy_one_year",
        sector_price_source="qmt_gics3_component_composite",
        universe_summary={
            "catalog_source": "qmt_gics3_components",
            "catalog_revision": expected_revision,
            "eligible_sector_count": int(manifest["catalog"]["eligible_sector_count"]),
            "sector_composite_member_limit": QMT_GICS3_COMPOSITE_MEMBER_LIMIT,
            "selected_symbol_count": selected,
            "completed_symbol_count": completed,
            "symbols_with_market_data": symbols_with_market_data,
            "symbols_without_market_data": symbols_without_market_data,
            "symbols_with_evaluations": int(
                manifest["summary"]["symbols_with_evaluations"]
            ),
            "causal_evaluation_count": int(manifest["summary"]["evaluation_count"]),
            "formal_selection_research_snapshot_count": len(research_snapshots),
            "formal_selection_research_symbol_count": len(
                selection_research_by_code
            ),
        },
        data_source_hashes=(
            ("qmt_extract_manifest", _sha256(manifest_path)),
            ("qmt_catalog_snapshot", _sha256(snapshot_path)),
            ("formal_selection_research_ledger", _sha256(research_path)),
            (
                "symbol_fact_checkpoint_tree",
                _checkpoint_tree(
                    tuple(
                        directory / "symbols" / f"{facts.code.replace('.', '_')}.pkl"
                        for facts in symbols
                    ),
                    root=directory,
                ),
            ),
            (
                "sector_fact_checkpoint_tree",
                _checkpoint_tree(
                    tuple(
                        _sector_path(directory, sector_id)
                        for sector_id in sectors
                    ),
                    root=directory,
                ),
            ),
            ("portfolio_run", _sha256(run_path)),
        ),
    )
    if qmt_research_contract.algorithm_hashes() != algorithm_hashes:
        raise RuntimeError("source code changed during finalization")
    if (
        "fact_algorithm" in manifest
        and qmt_research_contract.fact_algorithm_hashes() != fact_algorithm_hashes
    ):
        raise RuntimeError("symbol-fact source changed during finalization")
    qmt_research_contract.write_report_atomic(args.report, report)
    print(
        json.dumps(
            {
                "complete": True,
                "symbols": len(symbols),
                "sectors": len(sectors),
                "sector_failures": sector_failures,
                "trades": len(run.trades),
                "fills": len(run.fills),
                "elapsed_seconds": round(wall_time.perf_counter() - started, 2),
                "report": str(args.report.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
