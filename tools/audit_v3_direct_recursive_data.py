#!/usr/bin/env python3
"""Audit data eligibility for the direct-recursive V3 research replay.

This audit deliberately separates three questions:

* whether the Chanlun structure component can be reproduced;
* whether a candidate has an admissible point-in-time price basis; and
* whether the complete selection/execution history is sufficient for a P&L
  claim.

Passing the first question never promotes the latter two.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.decision_support.trading_system.data_audit_v3 import (  # noqa: E402
    V3DataContractEvidence,
    audit_v3_data_contract,
)
from chanlun.decision_support.trading_system.v3_individual_research import (  # noqa: E402
    FINANCIAL_SERVICE_CATALOG_ID,
    PROGRAM_SERVICE_URLS,
)
from tools.chanlun_v3_research_data import (  # noqa: E402
    atomic_json,
    content_sha256,
    sha256_file,
)


DEFAULT_PRESCREEN = Path(
    "audit/chanlun_live_integration/direct_recursive_v3_etf_prescreen.json"
)
DEFAULT_BACKTEST = Path(
    "audit/chanlun_live_integration/direct_recursive_v3_component_backtest.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/direct_recursive_v3_data_acceptance.json"
)


def _validate_content_hash(payload: Mapping[str, object], *, label: str) -> None:
    recorded = payload.get("content_sha256")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if recorded != content_sha256(stable):
        raise ValueError(f"{label} content hash changed")


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0 or not 0 <= numerator <= denominator:
        raise ValueError("coverage ratio inputs are invalid")
    return Decimal(numerator) / Decimal(denominator)


def build_audit(
    prescreen: Mapping[str, object],
    backtest: Mapping[str, object],
    *,
    prescreen_file_sha256: str,
    backtest_file_sha256: str,
) -> dict[str, object]:
    _validate_content_hash(prescreen, label="direct prescreen")
    _validate_content_hash(backtest, label="direct backtest")
    if prescreen.get("schema") != "chanlun-v3-direct-recursive-etf-prescreen/v1":
        raise ValueError("unsupported direct prescreen schema")
    if backtest.get("schema") != "chanlun-v3-direct-recursive-component-backtest/v1":
        raise ValueError("unsupported direct backtest schema")
    if backtest.get("prescreen_content_sha256") != prescreen.get("content_sha256"):
        raise ValueError("backtest does not bind the audited prescreen")
    if prescreen.get("live_status") != "LIVE_DISABLED" or backtest.get(
        "live_status"
    ) != "LIVE_DISABLED":
        raise ValueError("audit inputs cannot enable live trading")

    totals = prescreen.get("totals")
    reports = prescreen.get("instrument_reports")
    replay = backtest.get("replay")
    if not isinstance(totals, Mapping) or not isinstance(reports, list) or not reports:
        raise ValueError("direct prescreen inventory is unavailable")
    if not isinstance(replay, Mapping) or not isinstance(replay.get("metrics"), Mapping):
        raise ValueError("direct replay metrics are unavailable")
    instruments = int(totals.get("instruments", 0))
    adjustment_eligible = int(totals.get("adjustment_eligible_instruments", 0))
    recursive_three_levels = sum(
        len(item.get("structure_levels", ())) >= 3
        for item in reports
        if isinstance(item, Mapping)
    )
    source_ranges = tuple(
        (
            str(item["provider_symbol"]),
            f"{item['source_start']}..{item['source_end']} "
            f"({item['source_sessions']} sessions; {item['rows_1m']} 1m rows)",
        )
        for item in reports
        if isinstance(item, Mapping)
    )
    coverage = (
        ("one_minute_instrument_coverage", Decimal("1")),
        (
            "point_in_time_adjustment_instrument_coverage",
            _ratio(adjustment_eligible, instruments),
        ),
        (
            "three_recursive_level_instrument_coverage",
            _ratio(recursive_three_levels, instruments),
        ),
    )
    full_gate = audit_v3_data_contract(
        V3DataContractEvidence(
            one_minute_available=True,
            five_minute_from_same_one_minute_source=True,
            thirty_minute_from_same_one_minute_source=True,
            daily_from_same_source=True,
            weekly_from_completed_daily=True,
            monthly_from_completed_daily=True,
            completed_bar_enforcement=True,
            point_in_time_adjustment_factors=(adjustment_eligible == instruments),
            point_in_time_security_master=False,
            point_in_time_sector_membership=False,
            point_in_time_suspension_st_limits=False,
            delisting_and_continuity_events=False,
            point_in_time_corporate_actions=(adjustment_eligible == instruments),
            point_in_time_fundamental_research=False,
            point_in_time_market_cap_and_peer_sets=False,
            t_plus_one_and_sellable_quantity=False,
            effective_fee_schedule=False,
            buy_sell_quantity_increments=False,
            historical_quotes_and_trades=False,
            frozen_broker_latency=False,
            survivorship_free_universe=False,
            missing_data_retained_as_rejection=True,
            historical_quotes_for_selection=False,
            source_ranges=source_ranges,
            coverage=coverage,
        )
    )
    metrics = replay["metrics"]
    performance_evaluable = bool(metrics.get("performance_evaluable"))
    if performance_evaluable or backtest.get("return_claim_allowed") is not False:
        raise ValueError("current direct replay must not claim performance")

    requirements = (
        {
            "requirement": "1m_5m_30m_same_source",
            "status": "PASS_COMPONENT",
            "evidence": (
                "logical 30m/5m/1m signal authority is one immutable 1m graph; "
                "physical 5m/30m charts are cross-validation only"
            ),
        },
        {
            "requirement": "completed_bars_only",
            "status": "PASS_COMPONENT",
            "evidence": "normalizers and adapters reject future/incomplete prefixes",
        },
        {
            "requirement": "point_in_time_adjustment",
            "status": "PARTIAL",
            "evidence": (
                f"{adjustment_eligible}/{instruments} ETF instruments have an "
                "effective-dated causal adjustment ledger; missing ledgers are "
                "diagnostic-only and contribute zero formal candidates"
            ),
        },
        {
            "requirement": "historical_pool_industry_sector_membership",
            "status": "UNRESOLVED_SURVIVOR_RISK",
            "evidence": (
                "QMT GICS3 supports a current sector-first capture, but the capture "
                "is not an effective-dated historical constituent ledger"
            ),
        },
        {
            "requirement": "suspension_ST_limits_listing_delisting_actions",
            "status": "PARTIAL",
            "evidence": (
                "corporate-action ledgers are partial; historical ETF trade state, "
                "ST/limits and listing continuity are unavailable"
            ),
        },
        {
            "requirement": "fundamental_market_cap_relative_value",
            "status": "INTERFACES_AVAILABLE_SIGNED_HISTORY_MISSING",
            "evidence": (
                "financial-data-query URLs are allow-listed as raw evidence; no "
                "vendor metric is converted into PASS/LEADER/UNDERVALUED without "
                "a signed, disclosure-time adjudication"
            ),
        },
        {
            "requirement": "survivor_and_missing_deletion_bias",
            "status": "PARTIAL",
            "evidence": (
                "missing facts are retained as rejection, but the frozen current ETF "
                "universe cannot exclude historically delisted peers"
            ),
        },
        {
            "requirement": "T1_fee_quantity_execution",
            "status": "ENGINE_VERIFIED_HISTORICAL_FACTS_MISSING",
            "evidence": (
                "shared replay enforces T+1, fees, minimum commission, increments, "
                "partial fills and persistent exits when facts are supplied; the "
                "historical broker-vintage envelopes are absent"
            ),
        },
        {
            "requirement": "higher_timeframe_M_W_D_risk",
            "status": "IMPLEMENTED_COMPONENT",
            "evidence": (
                "daily is derived from the completed 1m base; W/M risk states and "
                "five-period lines consume completed daily prefixes"
            ),
        },
        {
            "requirement": "direct_recursive_30m_5m_1m_structure",
            "status": "PASS_COMPONENT",
            "evidence": (
                f"{recursive_three_levels}/{instruments} instruments reached all "
                "three recursive levels; standard center, nine-segment derivation "
                "and active expansion reclassification are all auditable"
            ),
        },
    )
    document: dict[str, object] = {
        "schema": "chanlun-v3-direct-recursive-data-acceptance/v1",
        "prescreen": {
            "content_sha256": prescreen["content_sha256"],
            "file_sha256": prescreen_file_sha256,
        },
        "backtest": {
            "content_sha256": backtest["content_sha256"],
            "file_sha256": backtest_file_sha256,
        },
        "signal_authority": "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH",
        "logical_level_mapping": prescreen["logical_level_mapping"],
        "technical_component_grade": "COMPONENT_ONLY",
        "full_system_data_gate": asdict(full_gate),
        "requirements": requirements,
        "coverage": dict(coverage),
        "source_ranges": dict(source_ranges),
        "selection_paths": {
            "ETF_PROXY": {
                "status": "COMPONENT_ONLY",
                "diagnostic_strategic_candidates": int(
                    totals.get("diagnostic_strategic_points", 0)
                ),
                "formal_strategic_candidates": int(
                    totals.get("strategic_points", 0)
                ),
                "aligned_entries": int(totals.get("aligned_entries", 0)),
            },
            "INDIVIDUAL_THREE_PROGRAM": {
                "status": "UNRESOLVED",
                "reason": (
                    "HISTORICAL_PIT_QMT_SECTOR_MEMBERSHIP_AND_SIGNED_"
                    "THREE_PROGRAM_ADJUDICATIONS_UNAVAILABLE"
                ),
            },
        },
        "financial_data_service": {
            "catalog_id": FINANCIAL_SERVICE_CATALOG_ID,
            "role": "RAW_EVIDENCE_ONLY",
            "program_service_urls": {
                program: tuple(sorted(urls))
                for program, urls in PROGRAM_SERVICE_URLS.items()
            },
            "automatic_three_program_judgment_allowed": False,
        },
        "return_evaluation": {
            "performance_evaluable": performance_evaluable,
            "formal_execution_return_allowed": False,
            "full_system_return_allowed": False,
            "empty_replay": bool(metrics.get("empty_replay")),
            "net_return_field": metrics.get("net_return"),
            "max_drawdown_field": metrics.get("max_drawdown"),
            "interpretation": (
                "EMPTY_LEDGER_ACCOUNTING_IDENTITY_NOT_STRATEGY_PERFORMANCE"
            ),
        },
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    document["content_sha256"] = content_sha256(document)
    return document


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--prescreen", type=Path, default=DEFAULT_PRESCREEN)
    value.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return value


def main() -> int:
    args = parser().parse_args()
    prescreen = json.loads(args.prescreen.read_text(encoding="utf-8"))
    backtest = json.loads(args.backtest.read_text(encoding="utf-8"))
    report = build_audit(
        prescreen,
        backtest,
        prescreen_file_sha256=sha256_file(args.prescreen),
        backtest_file_sha256=sha256_file(args.backtest),
    )
    atomic_json(args.output, report)
    print(
        f"wrote {args.output}: component={report['technical_component_grade']}; "
        f"full_gate={report['full_system_data_gate']['eligibility']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
