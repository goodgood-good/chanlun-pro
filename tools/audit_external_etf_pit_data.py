#!/usr/bin/env python3
"""Publish the point-in-time ETF data decision for the strict strategy replay."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.research_data import (
    CN,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_PIT_DATABASE,
    DEFAULT_PIT_MANIFEST,
    atomic_json,
    content_sha256,
    sha256_file,
)


DEFAULT_RECURSIVE_AUDIT = Path(
    "audit/chanlun_live_integration/recursive_structure_availability.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/external_data_acceptance.json"
)


def audit() -> dict[str, object]:
    manifest = json.loads(DEFAULT_PIT_MANIFEST.read_text(encoding="utf-8"))
    recursive = json.loads(DEFAULT_RECURSIVE_AUDIT.read_text(encoding="utf-8"))
    pit_hash = sha256_file(DEFAULT_PIT_DATABASE)
    if manifest["database_sha256"] != pit_hash:
        raise RuntimeError("external PIT database does not match its manifest")
    uri = f"file:{DEFAULT_PIT_DATABASE.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        candidate_dates = tuple(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT candidate_session FROM memberships ORDER BY 1"
            )
        )
        snapshots = tuple(
            {
                "candidate_session": candidate,
                "member_count": int(values[0]),
                "source_update_date": values[1],
            }
            for candidate in candidate_dates
            for values in (
                connection.execute(
                    """
                    SELECT COUNT(*), MAX(source_update_date)
                    FROM memberships WHERE candidate_session=?
                    """,
                    (candidate,),
                ).fetchone(),
            )
        )
        first = {
            row[0]
            for row in connection.execute(
                "SELECT code FROM memberships WHERE candidate_session=?",
                (candidate_dates[0],),
            )
        }
        last = {
            row[0]
            for row in connection.execute(
                "SELECT code FROM memberships WHERE candidate_session=?",
                (candidate_dates[-1],),
            )
        }
        statistics = {
            "candidate_membership_snapshots": len(candidate_dates),
            "union_members": int(
                connection.execute(
                    "SELECT COUNT(DISTINCT code) FROM memberships"
                ).fetchone()[0]
            ),
            "first_last_common_members": len(first & last),
            "removed_since_first_snapshot": len(first - last),
            "added_since_first_snapshot": len(last - first),
            "daily_bar_rows": int(
                connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            ),
            "daily_bar_start": connection.execute(
                "SELECT MIN(session) FROM daily_bars"
            ).fetchone()[0],
            "daily_bar_end": connection.execute(
                "SELECT MAX(session) FROM daily_bars"
            ).fetchone()[0],
            "historical_suspended_rows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM daily_bars WHERE trade_status='0'"
                ).fetchone()[0]
            ),
            "historical_st_rows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM daily_bars WHERE is_st='1'"
                ).fetchone()[0]
            ),
            "currently_inactive_historical_members": int(
                connection.execute(
                    "SELECT COUNT(*) FROM security_master WHERE status!='1' OR out_date!=''"
                ).fetchone()[0]
            ),
            "dated_adjustment_factor_rows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM adjustment_factors"
                ).fetchone()[0]
            ),
            "etf_distribution_rows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM etf_distributions"
                ).fetchone()[0]
            ),
            "exchange_trading_sessions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM trading_calendar WHERE is_trading_day='1'"
                ).fetchone()[0]
            ),
        }
    basket_failures = tuple(
        row for row in snapshots if row["member_count"] != 300
    )
    requirements = (
        {
            "requirement": "1m_5m_30m_same_source",
            "status": "PASS_RESEARCH",
            "evidence": (
                "5m and 30m are formed only from normalized completed "
                "financial-data-query one-minute rows"
            ),
        },
        {
            "requirement": "completed_bars_only",
            "status": "PASS_RESEARCH",
            "evidence": (
                "source start labels shift to completion; 11:30 and 15:00 boundary "
                "events merge into their preceding completed minute"
            ),
        },
        {
            "requirement": "point_in_time_adjustment",
            "status": "PASS_RESEARCH",
            "evidence": (
                "raw prices remain immutable and dated cash distributions apply "
                "forward only from their ex-date"
            ),
        },
        {
            "requirement": "historical_pool_and_basket_membership",
            "status": "PARTIAL_RESEARCH" if not basket_failures else "FAIL",
            "evidence": (
                f"{len(snapshots)} pre-frozen exploratory CSI300 snapshots; "
                f"{len(first - last)} removals and {len(last - first)} additions; "
                "they are not strict entry dates and cannot certify the final "
                "selection path"
            ),
        },
        {
            "requirement": "suspension_ST_listing_delisting_actions",
            "status": "PASS_RESEARCH",
            "evidence": (
                "effective-dated member master, raw daily trade status/ST, factors, "
                "and distribution ledgers are retained"
            ),
        },
        {
            "requirement": "fundamental_market_cap_relative_value",
            "status": "NOT_APPLICABLE_ETF_PROXY",
            "evidence": "the independent ETF_PROXY parameter snapshot is active",
        },
        {
            "requirement": "historical_quotes_and_trade_prints",
            "status": "WAIVED_FOR_RESEARCH_BY_USER",
            "evidence": (
                "the user requested a completed-minute-bar replay; this waiver cannot "
                "promote the result above RESEARCH_ONLY"
            ),
        },
        {
            "requirement": "T1_fee_quantity_execution",
            "status": "PARTIAL",
            "evidence": (
                "the engine models T+1, fees, minimum commission, quantities and "
                "strict later-bar crossing, but broker-vintage schedules are absent"
            ),
        },
    )
    structural_pass = (
        recursive.get("decision") == "STRICT_STRUCTURE_FACTS_CERTIFIED"
    )
    result: dict[str, object] = {
        "schema": "chanlun-external-data-acceptance",
        "generated_at": datetime.now(CN),
        "market_database_sha256": sha256_file(DEFAULT_MARKET_DATABASE),
        "external_pit_database_sha256": pit_hash,
        "manifest_sha256": sha256_file(DEFAULT_PIT_MANIFEST),
        "membership_snapshots": snapshots,
        "statistics": statistics,
        "basket_snapshot_failures": len(basket_failures),
        "strict_candidate_membership_snapshots_available": False,
        "membership_snapshot_scope": (
            "EXPLORATORY_TECHNICAL_CANDIDATE_DATES_NOT_STRICT_ENTRIES"
        ),
        "requirements": requirements,
        "recursive_structure_decision": recursive["decision"],
        "data_grade": "COMPONENT_ONLY",
        "strict_full_return_evaluation_allowed": False,
        "component_diagnostic_allowed": True,
        "blocking_reasons": tuple(
            reason
            for reason, blocked in (
                ("BLOCKED_BY_FROZEN_STRUCTURE", not structural_pass),
                ("STRICT_CANDIDATE_PIT_SNAPSHOT_SET_UNAVAILABLE", True),
                ("BROKER_VINTAGE_EXECUTION_RULES_UNAVAILABLE", True),
                ("HIGH_TIMEFRAME_FACT_ADAPTER_NOT_CERTIFIED", True),
            )
            if blocked
        ),
        "live_status": "LIVE_DISABLED",
    }
    result["content_sha256"] = content_sha256(result)
    return result


def main() -> int:
    result = audit()
    atomic_json(DEFAULT_OUTPUT, result)
    print(
        json.dumps(
            {
                "output": str(DEFAULT_OUTPUT.resolve()),
                "data_grade": result["data_grade"],
                "strict_full_return_evaluation_allowed": result[
                    "strict_full_return_evaluation_allowed"
                ],
                "blocking_reasons": result["blocking_reasons"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
