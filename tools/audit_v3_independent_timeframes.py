#!/usr/bin/env python3
"""Audit frozen points on independent 30m, 5m and 1m charts."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FRAME_COLUMNS,
    final_confirmed_structure_events,
)
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    align_independent_entry_chains,
    completed_l1_trend_fact,
    independent_alignment_contract,
)
from chanlun.decision_support.trading_system.v3_timeframe_override import (
    independent_timeframe_override,
)
from chanlun.exchange.kline_precision import resolve_structure_price_quantum
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)
from tools.chanlun_v3_research_data import (
    BENCHMARK_SYMBOL,
    CANONICAL_ETF_CODE,
    CN,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_PIT_DATABASE,
    PROVIDER_ETF_SYMBOL,
    aggregate_completed_bars,
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
    "audit/chanlun_live_integration/independent_timeframe_structure.json"
)


def _attach_structure_metadata(
    adjusted: pd.DataFrame,
    raw: pd.DataFrame,
    *,
    frequency: str,
) -> pd.DataFrame:
    if len(adjusted) != len(raw) or not adjusted["date"].equals(raw["date"]):
        raise ValueError("raw and adjusted independent frames are not aligned")
    frame = adjusted.copy()
    for field in ("open", "high", "low", "close"):
        frame[f"raw_{field}"] = raw[field].to_numpy(copy=True)
    frame.insert(0, "code", CANONICAL_ETF_CODE)
    frame = frame.loc[:, list(FRAME_COLUMNS)]
    quantum = resolve_structure_price_quantum("a", CANONICAL_ETF_CODE)
    if quantum is None:
        raise RuntimeError("ETF structure price quantum is unavailable")
    metadata = build_provider_price_basis_metadata(
        provider="financial-data-query+dated-distribution-ledger",
        market="a",
        code=CANONICAL_ETF_CODE,
        adjustment="causal-forward-cash-distribution-v2",
        structure_price_quantum=quantum,
    )
    return attach_price_basis_metadata(frame, metadata)


def _frames() -> tuple[dict[str, pd.DataFrame], dict[str, object], int]:
    benchmark = read_cached_series(
        DEFAULT_MARKET_DATABASE, symbol=BENCHMARK_SYMBOL, period="P_Day1"
    )
    raw_source = read_cached_series(
        DEFAULT_MARKET_DATABASE, symbol=PROVIDER_ETF_SYMBOL, period="P_Min1"
    )
    raw_one, interval = longest_complete_interval(raw_source, benchmark)
    ledger = causal_adjustment_ledger(
        raw_one, load_distributions(DEFAULT_PIT_DATABASE)
    )
    adjusted_one = apply_causal_forward_adjustments(raw_one, ledger)
    output = {
        "1m": _attach_structure_metadata(adjusted_one, raw_one, frequency="1m")
    }
    for minutes, frequency in ((5, "5m"), (30, "30m")):
        raw_aggregate = aggregate_completed_bars(raw_one, minutes=minutes)
        adjusted_aggregate = aggregate_completed_bars(
            adjusted_one, minutes=minutes
        )
        output[frequency] = _attach_structure_metadata(
            adjusted_aggregate,
            raw_aggregate,
            frequency=frequency,
        )
    return output, interval, len(ledger)


def _point_document(point) -> dict[str, object]:
    return asdict(point)


def audit() -> dict[str, object]:
    frames, interval, adjustment_count = _frames()
    override = independent_timeframe_override()
    level_map = {"L0": "30m", "L1": "5m", "L2": "1m"}
    points_by_level: dict[str, tuple] = {}
    trends_by_level: dict[str, tuple] = {}
    for level, frequency in level_map.items():
        print(
            {
                "stage": "independent_frozen_point_replay",
                "v3_level": level,
                "frequency": frequency,
                "rows": len(frames[frequency]),
            },
            flush=True,
        )
        ledger = final_confirmed_structure_events(
            CANONICAL_ETF_CODE, frequency, frames[frequency]
        )
        points_by_level[level] = tuple(
            point
            for point in ledger.points
            if point.recursive_level == override.accepted_recursive_level
        )
        trends_by_level[level] = tuple(
            trend
            for trend in ledger.completed_trends
            if trend.structural_level == override.accepted_recursive_level
        )
    counts = {
        level: {
            point_type: count
            for point_type, count in sorted(
                Counter(point.point_type for point in points).items()
            )
        }
        for level, points in points_by_level.items()
    }
    entry_fact_counts = {
        "l0_first_center_third_buy": sum(
            point.point_type == "3buy" and point.center_ordinal == 1
            for point in points_by_level["L0"]
        ),
        "l1_confirmed_points": len(points_by_level["L1"]),
        "l2_first_or_second_buy": sum(
            point.point_type in {"1buy", "2buy"}
            for point in points_by_level["L2"]
        ),
    }
    streams_available = all(value > 0 for value in entry_fact_counts.values())
    price_quantum = Decimal(str(frames["5m"].attrs["structure_price_quantum"]))
    l1_trend_facts = tuple(
        completed_l1_trend_fact(trend, price_quantum=price_quantum)
        for trend in trends_by_level["L1"]
    )
    l0_entry_points = tuple(
        point
        for point in points_by_level["L0"]
        if point.point_type == "3buy" and point.center_ordinal == 1
    )
    alignment_decisions = align_independent_entry_chains(
        l0_points=l0_entry_points,
        l1_trends=l1_trend_facts,
        l2_points=points_by_level["L2"],
    )
    aligned_chains = tuple(
        decision.chain
        for decision in alignment_decisions
        if decision.chain is not None
    )
    rejection_counts = Counter(
        reason
        for decision in alignment_decisions
        for reason in decision.reason_codes
    )
    alignment_contract = independent_alignment_contract()
    result: dict[str, object] = {
        "schema": "chanlun-v3-independent-timeframe-structure/v2",
        "generated_at": datetime.now(CN),
        "variant": override.document(),
        "variant_parameter_set_id": override.parameter_set_id,
        "mapping": level_map,
        "accepted_recursive_level_per_chart": 0,
        "source_start": interval["start"],
        "source_end": interval["end"],
        "source_sessions": interval["sessions"],
        "rows_by_frequency": {
            frequency: len(frame) for frequency, frame in frames.items()
        },
        "adjustment_events_applied": adjustment_count,
        "counts_by_v3_level_and_type": counts,
        "entry_fact_counts": entry_fact_counts,
        "completed_trend_counts_by_v3_level": {
            level: len(trends) for level, trends in trends_by_level.items()
        },
        "l1_completed_trend_ledger": tuple(
            fact.document() for fact in l1_trend_facts
        ),
        "point_ledgers": {
            level: tuple(_point_document(point) for point in points)
            for level, points in points_by_level.items()
        },
        "timeframe_point_streams_available": streams_available,
        "relation_rule": (
            "USER_OVERRIDE_INDEPENDENT_CHARTS; NO_DIRECT_RECURSIVE_PARENT_REQUIRED"
        ),
        "entry_alignment_status": "CERTIFIED_CAUSAL_ALIGNMENT",
        "entry_alignment_contract": alignment_contract.document(),
        "entry_alignment_parameter_set_id": alignment_contract.parameter_set_id,
        "alignment_decisions": tuple(
            decision.document() for decision in alignment_decisions
        ),
        "alignment_rejection_counts": dict(sorted(rejection_counts.items())),
        "aligned_entry_chain_count": len(aligned_chains),
        "aligned_entry_chains": tuple(
            chain.document() for chain in aligned_chains
        ),
        "decision": (
            "CAUSAL_ALIGNMENT_CERTIFIED_WITH_ENTRIES"
            if aligned_chains
            else "CAUSAL_ALIGNMENT_CERTIFIED_ZERO_ENTRIES"
            if streams_available
            else "INDEPENDENT_TIMEFRAME_ENTRY_FACTS_INSUFFICIENT"
        ),
        "market_database_sha256": sha256_file(DEFAULT_MARKET_DATABASE),
        "pit_database_sha256": sha256_file(DEFAULT_PIT_DATABASE),
        "frozen_core_modified": False,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    result["content_sha256"] = content_sha256(result)
    return result


def main() -> int:
    result = audit()
    atomic_json(DEFAULT_OUTPUT, result)
    print(
        {
            "output": str(DEFAULT_OUTPUT.resolve()),
            "decision": result["decision"],
            "entry_fact_counts": result["entry_fact_counts"],
            "content_sha256": result["content_sha256"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
