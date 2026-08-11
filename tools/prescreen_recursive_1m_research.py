#!/usr/bin/env python3
"""Causal prescreen for the user-authorized L0=1m research hierarchy.

The tool is read-only with respect to source databases.  It rebuilds every
strict structure from the longest complete contiguous local interval, calls
the same ``evaluate_recursive_1m_entry`` function intended for paper trading,
and writes a deterministic research artifact.  It never sends an order and
can never claim full-system eligibility.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.core.strict_structure.upgrade_evidence import (  # noqa: E402
    UpgradeEvidenceKind,
    collect_recursive_upgrade_evidence,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    strict_state,
)
from chanlun.decision_support.trading_system.recursive_1m_decision import (  # noqa: E402
    Recursive1mDataFacts,
    evaluate_recursive_1m_entry,
)
from chanlun.decision_support.trading_system.recursive_1m_research import (  # noqa: E402
    recursive_1m_parameter_manifest,
    recursive_1m_parameter_snapshot,
)
from chanlun.decision_support.trading_system.structure_adapter import (  # noqa: E402
    extract_confirmed_points,
)
from tools.research_data import (  # noqa: E402
    DEFAULT_PIT_DATABASE,
    atomic_json,
    content_sha256,
    sha256_file,
)
from tools.prescreen_cached_symbols import (  # noqa: E402
    _build_frames,
    provider_to_project_code,
)


DEFAULT_UNIVERSE = Path(
    "audit/chanlun_live_integration/csi300_broad_etf_universe.json"
)
DEFAULT_MARKET_DATABASE = Path(
    ".cache/chanlun_csi300_broad_pool/financial_data_query_bars.sqlite3"
)
DEFAULT_CORPORATE_ACTIONS = Path(
    "audit/chanlun_live_integration/qmt_etf_corporate_actions.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/recursive_1m_etf_prescreen.json"
)
SPLITS = (
    ("TRAIN_60_PERCENT", date(2018, 10, 9), date(2021, 5, 6)),
    ("VALIDATION_20_PERCENT", date(2021, 5, 7), date(2022, 3, 15)),
    ("FINAL_HOLDOUT_20_PERCENT", date(2022, 3, 16), date(2023, 1, 19)),
)


def _split_name(value: date) -> str:
    for name, start, end in SPLITS:
        if start <= value <= end:
            return name
    return "OUTSIDE_FROZEN_SPLITS"


def _point_document(point) -> dict[str, object]:
    return {
        "point_id": point.point_id,
        "point_type": point.point_type,
        "source_frequency": point.source_frequency,
        "recursive_level": point.recursive_level,
        "center_id": point.center_id,
        "center_ordinal": point.center_ordinal,
        "center_zd": point.center_zd,
        "center_zg": point.center_zg,
        "anchor_at": point.anchor_at,
        "confirmed_at": point.confirmed_at,
        "available_at": point.available_at,
        "structure_anchor_price": point.structure_anchor_price,
        "structure_invalidation_price": point.structure_invalidation_price,
        "price_basis_revision": point.price_basis_revision,
        "evidence_codes": point.evidence_codes,
    }


def _instrument_report(
    *,
    database: Path,
    pit_database: Path,
    corporate_actions: Path,
    provider_symbol: str,
) -> dict[str, object]:
    project_code = provider_to_project_code(provider_symbol)
    frames, interval, adjustment = _build_frames(
        database=database,
        pit_database=pit_database,
        corporate_actions=corporate_actions,
        benchmark_symbol="000300.CSI",
        provider_symbol=provider_symbol,
    )
    frame = frames["1m"]
    state = strict_state(project_code, "1m", frame)
    state.process_klines(frame)
    evidence = state.get_strict_evidence()
    structure = evidence.structure
    as_of = frame["date"].iloc[-1]
    points = extract_confirmed_points(
        evidence,
        code=project_code,
        source_frequency="1m",
        as_of=as_of,
    )
    candidates = tuple(
        point
        for point in points
        if point.recursive_level == 0
        and point.point_type == "3buy"
        and point.center_ordinal == 1
    )
    parameters = recursive_1m_parameter_snapshot("ETF_PROXY")
    data_facts = Recursive1mDataFacts(
        complete_contiguous_interval=True,
        point_in_time_adjustment_complete=bool(
            adjustment["formal_chain_eligibility"]
        ),
        missing_data_inferred=bool(adjustment["missing_data_was_inferred"]),
        source_fact_ids=tuple(
            dict.fromkeys(
                (
                    str(adjustment["effective_dated_adjustment_ledger_sha256"]),
                    str(adjustment["corporate_action_snapshot_sha256"]),
                    evidence.price_basis_revision,
                )
            )
        ),
    )
    decisions = tuple(
        evaluate_recursive_1m_entry(
            point=point,
            structure=structure,
            observed_at=as_of,
            parameters=parameters,
            data_facts=data_facts,
        )
        for point in candidates
    )
    decision_documents = []
    for point, decision in zip(candidates, decisions):
        document = asdict(decision)
        document["point"] = _point_document(point)
        document["split"] = _split_name(point.available_at.date())
        decision_documents.append(document)

    final_upgrades = collect_recursive_upgrade_evidence(structure)
    levels = []
    for level in structure.levels:
        levels.append(
            {
                "structural_level": level.structural_level,
                "unit_count": len(level.units),
                "center_count": len(level.center_result.centers),
                "completed_center_count": sum(
                    center.state.value == "completed"
                    for center in level.center_result.centers
                ),
                "trend_count": len(level.trend_types),
                "locked_trend_count": sum(trend.locked for trend in level.trend_types),
            }
        )
    rejections = Counter(
        code
        for decision in decisions
        if not decision.component_eligible
        for code in decision.rejected_reason_codes
    )
    by_split = {}
    for name, _start, _end in SPLITS:
        selected = tuple(
            document
            for document in decision_documents
            if document["split"] == name
        )
        by_split[name] = {
            "candidates": len(selected),
            "component_eligible": sum(
                bool(document["component_eligible"]) for document in selected
            ),
            "rejected": sum(
                not bool(document["component_eligible"]) for document in selected
            ),
        }
    return {
        "provider_symbol": provider_symbol,
        "project_code": project_code,
        "source_start": interval["start"],
        "source_end": interval["end"],
        "source_sessions": interval["sessions"],
        "all_complete_runs": interval["all_complete_runs"],
        "rows_by_frequency": {
            frequency: len(frames[frequency]) for frequency in ("1m", "5m", "30m")
        },
        "adjustment_gate": adjustment,
        "price_basis_revision": evidence.price_basis_revision,
        "strict_config_revision": evidence.strict_config_revision,
        "structure_revision": evidence.structure_revision,
        "structure_levels": levels,
        "confirmed_point_count": len(points),
        "l0_first_center_third_buy_count": len(candidates),
        "component_eligible_count": sum(
            decision.component_eligible for decision in decisions
        ),
        "full_system_eligible_count": 0,
        "rejection_counts": dict(sorted(rejections.items())),
        "candidate_counts_by_split": by_split,
        "final_upgrade_context_counts": dict(
            sorted(Counter(item.kind.value for item in final_upgrades).items())
        ),
        "final_l2_nine_segment_context_count": sum(
            item.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
            and item.target_level == 2
            for item in final_upgrades
        ),
        "candidate_decisions": decision_documents,
        "data_grade": "COMPONENT_ONLY",
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


def build_report(args: argparse.Namespace) -> dict[str, object]:
    universe = args.universe.resolve()
    payload = __import__("json").loads(universe.read_text(encoding="utf-8"))
    if payload.get("schema") != "chanlun-csi300-broad-etf-universe":
        raise ValueError("unsupported ETF universe artifact")
    symbols = tuple(item["symbol"] for item in payload["instruments"])
    if args.symbol:
        requested = tuple(args.symbol)
        unknown = tuple(value for value in requested if value not in symbols)
        if unknown:
            raise ValueError(f"symbols are outside frozen ETF universe: {unknown}")
        symbols = requested

    reports = []
    for index, symbol in enumerate(symbols, start=1):
        print(
            f"[{index}/{len(symbols)}] recursive-1m causal prescreen {symbol}",
            flush=True,
        )
        reports.append(
            _instrument_report(
                database=args.database,
                pit_database=args.pit_database,
                corporate_actions=args.corporate_actions,
                provider_symbol=symbol,
            )
        )

    total_rejections = Counter(
        code
        for report in reports
        for code, count in report["rejection_counts"].items()
        for _ in range(count)
    )
    manifest = recursive_1m_parameter_manifest()
    report: dict[str, object] = {
        "schema": "chanlun-recursive-1m-etf-prescreen",
        "scope": "STRATEGIC_STRUCTURE_COMPONENT_PRESCREEN",
        "universe_artifact": str(universe),
        "universe_artifact_sha256": sha256_file(universe),
        "universe_content_sha256": payload["content_sha256"],
        "universe_symbols": symbols,
        "selection_path": "ETF_PROXY",
        "individual_path": {
            "status": "NOT_RUN",
            "reason": "MISSING_POINT_IN_TIME_INDUSTRY_FUNDAMENTAL_RELATIVE_VALUE_SNAPSHOTS",
            "parameter_set_id": manifest["snapshots"]["INDIVIDUAL_THREE_PROGRAM"][
                "parameter_set_id"
            ],
        },
        "parameter_manifest": manifest,
        "split_policy": {
            "name": "CHRONOLOGICAL_60_20_20_NO_PARAMETER_REFIT",
            "anchor_symbol": "510300.SH",
            "splits": tuple(
                {"name": name, "start": start, "end": end}
                for name, start, end in SPLITS
            ),
        },
        "source_database": {
            "market_path": str(args.database.resolve()),
            "market_sha256": sha256_file(args.database),
            "pit_path": str(args.pit_database.resolve()),
            "pit_sha256": sha256_file(args.pit_database),
            "corporate_actions_path": str(args.corporate_actions.resolve()),
            "corporate_actions_sha256": sha256_file(args.corporate_actions),
        },
        "instrument_reports": reports,
        "totals": {
            "instruments": len(reports),
            "l0_first_center_third_buys": sum(
                report["l0_first_center_third_buy_count"] for report in reports
            ),
            "component_eligible": sum(
                report["component_eligible_count"] for report in reports
            ),
            "full_system_eligible": 0,
            "rejection_counts": dict(sorted(total_rejections.items())),
        },
        "data_grade": "COMPONENT_ONLY",
        "complete_system_return_claim_allowed": False,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    report["content_sha256"] = content_sha256(report)
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--database", type=Path, default=DEFAULT_MARKET_DATABASE)
    value.add_argument("--pit-database", type=Path, default=DEFAULT_PIT_DATABASE)
    value.add_argument(
        "--corporate-actions",
        type=Path,
        default=DEFAULT_CORPORATE_ACTIONS,
    )
    value.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    value.add_argument("--symbol", action="append")
    return value


def main() -> int:
    args = parser().parse_args()
    report = build_report(args)
    atomic_json(args.output, report)
    print(
        f"wrote {args.output}: component_eligible="
        f"{report['totals']['component_eligible']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
