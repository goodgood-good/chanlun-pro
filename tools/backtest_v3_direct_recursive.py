#!/usr/bin/env python3
"""Run the shared V3 replay engine for the direct-recursive prescreen.

If the certified prescreen contains no aligned entry, the only honest replay
is an empty ledger.  Its 0 return and 0 drawdown are accounting identities and
are explicitly marked non-evaluable.  A non-empty aligned set is rejected by
this tool until matching point-in-time selection, risk, account, execution and
post-entry signal facts are supplied; it never fabricates them.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.decision_support.trading_system.v3_execution import (  # noqa: E402
    V3FeeModel,
    V3FeeRateAt,
)
from chanlun.decision_support.trading_system.v3_multisymbol_replay import (  # noqa: E402
    StrictV3MultiSymbolReplayEngine,
    strict_v3_direct_replay_contract,
)
from tools.chanlun_v3_research_data import (  # noqa: E402
    atomic_json,
    content_sha256,
    sha256_file,
)


CN = ZoneInfo("Asia/Shanghai")
DEFAULT_PRESCREEN = Path(
    "audit/chanlun_live_integration/direct_recursive_v3_etf_prescreen.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/direct_recursive_v3_component_backtest.json"
)


def _validate_prescreen(payload: dict[str, object]) -> None:
    if payload.get("schema") != "chanlun-v3-direct-recursive-etf-prescreen/v1":
        raise ValueError("unsupported direct-recursive prescreen schema")
    recorded = payload.get("content_sha256")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if recorded != content_sha256(stable):
        raise ValueError("direct-recursive prescreen content hash changed")
    if payload.get("signal_authority") != "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH":
        raise ValueError("direct-recursive signal authority changed")
    if payload.get("live_status") != "LIVE_DISABLED":
        raise ValueError("direct-recursive prescreen cannot enable live trading")


def build_backtest(
    payload: dict[str, object],
    *,
    prescreen_file_sha256: str,
) -> dict[str, object]:
    _validate_prescreen(payload)
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("direct-recursive prescreen totals are unavailable")
    aligned = int(totals.get("aligned_entries", -1))
    if aligned < 0:
        raise ValueError("direct-recursive aligned-entry count is invalid")
    if aligned:
        raise RuntimeError(
            "aligned entries require signed selection/risk/account/execution facts"
        )
    reports = payload.get("instrument_reports")
    if not isinstance(reports, list) or not reports:
        raise ValueError("direct-recursive instrument reports are unavailable")
    start = min(date.fromisoformat(str(item["source_start"])) for item in reports)
    started_at = datetime.combine(start, datetime.min.time(), tzinfo=CN).replace(
        hour=9,
        minute=30,
    )
    fee_model = V3FeeModel(
        schedule_id="EMPTY_REPLAY_NO_ORDER_FEE_SENTINEL",
        rates=(
            V3FeeRateAt(
                effective_from=start,
                commission_rate=Decimal("0"),
                minimum_commission=Decimal("0"),
                stock_sell_stamp_rate=Decimal("0"),
                transfer_rate=Decimal("0"),
            ),
        ),
    )
    contract = strict_v3_direct_replay_contract()
    result = StrictV3MultiSymbolReplayEngine(
        initial_cash=Decimal("1000000"),
        started_at=started_at,
        fee_model=fee_model,
        contract=contract,
    ).replay(())
    report: dict[str, object] = {
        "schema": "chanlun-v3-direct-recursive-component-backtest/v1",
        "prescreen_content_sha256": payload["content_sha256"],
        "prescreen_file_sha256": prescreen_file_sha256,
        "initial_cash": "1000000",
        "started_at": started_at,
        "signal_authority": "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH",
        "logical_level_mapping": payload["logical_level_mapping"],
        "diagnostic_strategic_candidate_count": int(
            totals.get("diagnostic_strategic_points", totals.get("strategic_points", 0))
        ),
        "strategic_candidate_count": int(totals.get("strategic_points", 0)),
        "adjustment_eligible_instrument_count": int(
            totals.get("adjustment_eligible_instruments", 0)
        ),
        "aligned_entry_count": aligned,
        "alignment_rejection_counts": totals.get(
            "alignment_rejection_counts",
            {},
        ),
        "data_gate_rejection_counts": totals.get(
            "data_gate_rejection_counts",
            {},
        ),
        "replay": asdict(result),
        "data_grade": "COMPONENT_ONLY",
        "performance_evaluable": result.metrics.performance_evaluable,
        "return_claim_allowed": False,
        "return_field_interpretation": (
            "EMPTY_LEDGER_ACCOUNTING_IDENTITY_NOT_STRATEGY_PERFORMANCE"
        ),
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    report["content_sha256"] = content_sha256(report)
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--prescreen", type=Path, default=DEFAULT_PRESCREEN)
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return value


def main() -> int:
    args = parser().parse_args()
    payload = json.loads(args.prescreen.read_text(encoding="utf-8"))
    report = build_backtest(
        payload,
        prescreen_file_sha256=sha256_file(args.prescreen),
    )
    atomic_json(args.output, report)
    metrics = report["replay"]["metrics"]
    print(
        f"wrote {args.output}: empty={metrics['empty_replay']}; "
        f"performance_evaluable={metrics['performance_evaluable']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
