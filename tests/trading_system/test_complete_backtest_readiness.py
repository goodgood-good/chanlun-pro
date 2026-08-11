from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "audit_complete_backtest_readiness_test",
    ROOT / "tools/audit_complete_backtest_readiness.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_failed_upstream_facts_produce_na_metrics_not_zero_performance() -> None:
    prescreen = {
        "live_status": "LIVE_DISABLED",
        "logical_level_mapping": {
            "L0_30M_STRATEGIC": "raw level 2",
            "L1_5M_TACTICAL": "raw level 1",
            "L2_1M_LOCATOR": "raw level 0",
        },
        "totals": {
            "instruments": 8,
            "adjustment_eligible_instruments": 2,
            "diagnostic_strategic_points": 6,
            "strategic_points": 2,
            "strategic_points_with_higher_timeframe_risk": 6,
            "higher_timeframe_risk_eligible_strategic_points": 0,
            "higher_timeframe_risk_gate_counts": {"AMBER": 2, "UNRESOLVED": 4},
            "aligned_entries": 0,
            "alignment_rejection_counts": {"NO_L2_LOCATOR": 6},
        },
    }
    backtest = {
        "live_status": "LIVE_DISABLED",
        "replay": {
            "metrics": {
                "performance_evaluable": False,
                "net_return": "0",
                "max_drawdown": "0",
            }
        },
    }
    data_audit = {
        "live_status": "LIVE_DISABLED",
        "full_system_data_gate": {
            "eligibility": "RESEARCH_ONLY",
            "full_system_failures": (
                "point_in_time_sector_membership",
                "historical_quotes_and_trades",
            ),
        },
    }
    qmt_audit = {
        "membership_audit": {
            "status": "CURRENT_BACKFILL_PROVEN",
            "historical_point_in_time_eligible": False,
            "future_listed_members": ({"member": "SH.688981"},),
        },
        "historical_tick_audit": {
            "all_requested_ranges_available": False,
            "historical_quote_and_trade_gate": "FAIL_MISSING_HISTORICAL_TICKS",
        },
    }

    report = MODULE.build_readiness(
        prescreen=prescreen,
        backtest=backtest,
        data_audit=data_audit,
        qmt_audit=qmt_audit,
        prospective_sector_capture_count=1,
        financial_probe_available=True,
        artifact_hashes={"prescreen": "sha256:" + "a" * 64},
    )

    assert report["execution_result"]["status"] == (
        "BLOCKED_BEFORE_PERFORMANCE_EVALUATION"
    )
    assert report["execution_result"]["net_return"] is None
    assert report["execution_result"]["max_drawdown"] is None
    assert report["execution_result"]["empty_ledger_fields_are_not_performance"] == {
        "net_return": "0",
        "max_drawdown": "0",
        "interpretation": "ACCOUNTING_IDENTITY_ONLY",
    }
    codes = {value["code"] for value in report["blockers"]}
    assert {
        "QMT_HISTORICAL_SECTOR_CURRENT_BACKFILL",
        "SIGNED_THREE_PROGRAM_HISTORY_MISSING",
        "HISTORICAL_QUOTES_AND_TRADES_MISSING",
        "NO_FULLY_ALIGNED_30M_5M_1M_ENTRY",
    }.issubset(codes)
    assert report["live_status"] == "LIVE_DISABLED"
