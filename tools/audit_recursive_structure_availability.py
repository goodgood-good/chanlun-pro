#!/usr/bin/env python3
"""Audit whether frozen one-minute structure proves the strict strategy L0/L1/L2 chain."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.core.strict_structure.level_catalog import recursive_level_labels
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FRAME_COLUMNS,
    final_confirmed_points,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)
from chanlun.exchange.kline_precision import resolve_structure_price_quantum
from tools.research_data import (
    BENCHMARK_SYMBOL,
    CANONICAL_ETF_CODE,
    CN,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_PIT_DATABASE,
    PROVIDER_ETF_SYMBOL,
    apply_causal_forward_adjustments,
    atomic_json,
    causal_adjustment_ledger,
    content_sha256,
    load_distributions,
    longest_complete_interval,
    read_cached_series,
    sha256_file,
)


DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/recursive_structure_availability.json"
)


def _structure_frame(one_minute, adjustment_ledger):
    frame = one_minute.copy()
    for field in ("open", "high", "low", "close"):
        frame[f"raw_{field}"] = frame[field]
    frame = apply_causal_forward_adjustments(frame, adjustment_ledger)
    frame.insert(0, "code", CANONICAL_ETF_CODE)
    frame = frame.loc[:, list(FRAME_COLUMNS)]
    quantum = resolve_structure_price_quantum("a", CANONICAL_ETF_CODE)
    if quantum is None:
        raise RuntimeError("ETF price quantum is unavailable")
    metadata = build_provider_price_basis_metadata(
        provider="financial-data-query+dated-distribution-ledger",
        market="a",
        code=CANONICAL_ETF_CODE,
        adjustment="causal-forward-cash-distribution",
        structure_price_quantum=quantum,
    )
    return attach_price_basis_metadata(frame, metadata)


def audit(
    *,
    market_database: Path,
    pit_database: Path,
) -> dict[str, object]:
    benchmark = read_cached_series(
        market_database,
        symbol=BENCHMARK_SYMBOL,
        period="P_Day1",
    )
    one_raw = read_cached_series(
        market_database,
        symbol=PROVIDER_ETF_SYMBOL,
        period="P_Min1",
    )
    one_minute, interval = longest_complete_interval(one_raw, benchmark)
    distributions = load_distributions(pit_database)
    ledger = causal_adjustment_ledger(one_minute, distributions)
    frame = _structure_frame(one_minute, ledger)
    print(
        {
            "stage": "strict_frozen_recursive_replay",
            "rows": len(frame),
            "start": interval["start"].isoformat(),
            "end": interval["end"].isoformat(),
        },
        flush=True,
    )
    points = final_confirmed_points(CANONICAL_ETF_CODE, "1m", frame)
    counts = Counter((point.recursive_level, point.point_type) for point in points)
    parent_edges = tuple(
        (point.point_id, point.parent_point_id, point.recursive_level)
        for point in points
        if point.parent_point_id is not None
    )
    observed_levels = tuple(sorted({point.recursive_level for point in points}))
    required_levels = (0, 1, 2)
    every_level_observed = all(level in observed_levels for level in required_levels)
    required_entry_points = {
        "l0_level2_third_buy": counts.get((2, "3buy"), 0),
        "l1_level1_points": sum(
            count for (level, _point_type), count in counts.items() if level == 1
        ),
        "l2_level0_first_or_second_buy": (
            counts.get((0, "1buy"), 0) + counts.get((0, "2buy"), 0)
        ),
    }
    entry_types_observed = all(value > 0 for value in required_entry_points.values())
    if not every_level_observed:
        decision = "BLOCKED_BY_FROZEN_STRUCTURE"
        reason = "frozen causal evidence emitted no confirmed points at every required recursive level"
    elif not entry_types_observed:
        decision = "STRICT_ENTRY_FACTS_UNAVAILABLE"
        reason = "the required L0 third-buy, L1, and L2 locator point types did not all occur"
    else:
        decision = "RELATION_ID_AUDIT_REQUIRED"
        reason = "all levels and point types exist, but complete direct relation identity still requires proof"
    result: dict[str, object] = {
        "schema": "chanlun-recursive-structure-availability",
        "generated_at": datetime.now(CN),
        "symbol": CANONICAL_ETF_CODE,
        "source_frequency": "1m",
        "source_timestamp_contract": (
            "provider start labels shifted to completion; 11:30 and 15:00 "
            "boundary events merged into their preceding minute"
        ),
        "expected_recursive_level_labels": recursive_level_labels("1m"),
        "required_levels": {"L2": 0, "L1": 1, "L0": 2},
        "source_rows": len(frame),
        "source_sessions": interval["sessions"],
        "source_start": interval["start"],
        "source_end": interval["end"],
        "interval_audit": interval,
        "adjustment_events_applied": len(ledger),
        "confirmed_point_count": len(points),
        "observed_recursive_levels": observed_levels,
        "counts_by_level_and_type": {
            f"level{level}:{point_type}": count
            for (level, point_type), count in sorted(counts.items())
        },
        "required_entry_point_counts": required_entry_points,
        "point_parent_edge_count": len(parent_edges),
        "direct_recursive_l0_l1_l2_point_levels_available": every_level_observed,
        "strict_entry_fact_types_available": entry_types_observed,
        "decision": decision,
        "reason": reason,
        "market_database_sha256": sha256_file(market_database),
        "pit_database_sha256": sha256_file(pit_database),
        "frozen_core_modified": False,
        "live_status": "LIVE_DISABLED",
    }
    result["content_sha256"] = content_sha256(result)
    return result


def main() -> int:
    result = audit(
        market_database=DEFAULT_MARKET_DATABASE,
        pit_database=DEFAULT_PIT_DATABASE,
    )
    atomic_json(DEFAULT_OUTPUT, result)
    print(
        {
            "output": str(DEFAULT_OUTPUT.resolve()),
            "decision": result["decision"],
            "confirmed_point_count": result["confirmed_point_count"],
            "observed_recursive_levels": result["observed_recursive_levels"],
            "content_sha256": result["content_sha256"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
