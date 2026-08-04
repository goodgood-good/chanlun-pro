#!/usr/bin/env python3
"""Build the V3.1 causal entry-evidence ledger without touching structure core."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalCenterCompletionFact,
    final_confirmed_structure_events,
)
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    CompletedL1TrendFact,
)
from chanlun.decision_support.trading_system.v31_parameters import (
    v31_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v31_snapshot import (
    completed_l1_unit_snapshots_at_l0_decisions,
)
from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
    ConfirmationBarFact,
    align_v31_independent_entry_chains,
    confirmation_bar_fact,
    v31_alignment_contract,
)
from tools.audit_v3_independent_timeframes import _frames
from tools.chanlun_v3_research_data import (
    CANONICAL_ETF_CODE,
    CN,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_PIT_DATABASE,
    atomic_json,
    content_sha256,
    sha256_file,
)


V3_STRUCTURE = Path(
    "audit/chanlun_live_integration/independent_timeframe_structure.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/v31_independent_timeframe_structure.json"
)


def _datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _structural_point(document: dict[str, object]) -> StructuralPoint:
    values = dict(document)
    for field in ("anchor_at", "confirmed_at", "available_at"):
        if values.get(field) is not None:
            values[field] = _datetime(values[field])
    values["evidence_codes"] = tuple(values.get("evidence_codes", ()))
    return StructuralPoint(**values)


def _l1_trend(document: dict[str, object]) -> CompletedL1TrendFact:
    values = dict(document)
    for field in (
        "market_start",
        "market_end",
        "confirmed_at",
        "available_at",
        "terminal_start",
        "terminal_end",
    ):
        values[field] = _datetime(values[field])
    for field in ("start_price", "end_price", "low_price", "high_price"):
        values[field] = Decimal(str(values[field]))
    values["evidence_unit_ids"] = tuple(values["evidence_unit_ids"])
    return CompletedL1TrendFact(**values)


def _center_completion(document: dict[str, object]) -> CausalCenterCompletionFact:
    values = dict(document)
    for field in (
        "available_at",
        "completed_at",
        "leave_market_start",
        "leave_market_end",
        "leave_available_at",
        "return_market_start",
        "return_market_end",
        "return_available_at",
    ):
        values[field] = _datetime(values[field])
    return CausalCenterCompletionFact(**values)


def _confirmation_bar(document: dict[str, object]) -> ConfirmationBarFact:
    values = dict(document)
    values["available_at"] = _datetime(values["available_at"])
    for field in ("raw_open", "raw_high", "raw_low", "raw_close"):
        values[field] = Decimal(str(values[field]))
    return ConfirmationBarFact(**values)


def _l0_price_quantum(
    l0_points: tuple[StructuralPoint, ...],
    centers: tuple[CausalCenterCompletionFact, ...],
) -> Decimal:
    """Recover and cross-check the frozen price quantum from cached facts."""

    center_by_id = {value.center_id: value for value in centers}
    values: set[Decimal] = set()
    for point in l0_points:
        if point.center_id is None or point.center_zg is None:
            continue
        center = center_by_id.get(point.center_id)
        if center is None or center.zg_tick <= 0:
            continue
        values.add(Decimal(str(point.center_zg)) / Decimal(center.zg_tick))
    if len(values) != 1:
        raise RuntimeError("cached V3.1 facts do not identify one L0 price quantum")
    return values.pop()


def audit() -> dict[str, object]:
    cached = json.loads(V3_STRUCTURE.read_text(encoding="utf-8"))
    if cached["market_database_sha256"] != sha256_file(DEFAULT_MARKET_DATABASE):
        raise RuntimeError("V3 structure cache and current market database disagree")
    if cached["pit_database_sha256"] != sha256_file(DEFAULT_PIT_DATABASE):
        raise RuntimeError("V3 structure cache and current PIT database disagree")
    prior_v31 = (
        json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        if DEFAULT_OUTPUT.is_file()
        else None
    )
    cache_matches = bool(
        isinstance(prior_v31, dict)
        and prior_v31.get("market_database_sha256")
        == sha256_file(DEFAULT_MARKET_DATABASE)
        and prior_v31.get("pit_database_sha256") == sha256_file(DEFAULT_PIT_DATABASE)
    )
    if (
        cache_matches
        and prior_v31.get("l0_center_completion_ledger")
    ):
        l0_center_completions = tuple(
            _center_completion(item)
            for item in prior_v31["l0_center_completion_ledger"]
        )
        l0_points = tuple(
            _structural_point(item)
            for item in cached["point_ledgers"]["L0"]
            if item["point_type"] == "3buy" and item["center_ordinal"] == 1
        )
        l0_center_source = "HASH_MATCHED_V31_CAUSAL_CACHE"
    else:
        frames, interval, adjustment_count = _frames()
        print(
            {
                "stage": "v31_l0_center_completion_replay",
                "frequency": "30m",
                "rows": len(frames["30m"]),
            },
            flush=True,
        )
        l0_ledger = final_confirmed_structure_events(
            CANONICAL_ETF_CODE, "30m", frames["30m"]
        )
        l0_points = tuple(
            point
            for point in l0_ledger.points
            if point.recursive_level == 0
            and point.point_type == "3buy"
            and point.center_ordinal == 1
        )
        l0_center_completions = l0_ledger.center_completions
        l0_center_source = "FRESH_CAUSAL_REPLAY"
    reusable_unit_diagnostic = bool(
        cache_matches
        and prior_v31.get("l1_completed_unit_ledger")
    )
    if reusable_unit_diagnostic:
        l1_unit_ledger = tuple(prior_v31["l1_completed_unit_ledger"])
        l1_snapshot_unit_counts = dict(
            prior_v31.get("l1_snapshot_unit_counts", {})
        )
        unit_diagnostic_source = "HASH_MATCHED_PRIOR_CAUSAL_DIAGNOSTIC"
    else:
        if "frames" not in locals():
            frames, interval, adjustment_count = _frames()
        print(
            {
                "stage": "v31_l1_incremental_prefix_diagnostic",
                "frequency": "5m",
                "rows": len(frames["5m"]),
                "checkpoints": len(l0_points),
            },
            flush=True,
        )
        l1_snapshots = completed_l1_unit_snapshots_at_l0_decisions(
            code=CANONICAL_ETF_CODE,
            five_minute_frame=frames["5m"],
            l0_points=l0_points,
        )
        l1_unit_by_id = {
            fact.unit_id: fact
            for facts in l1_snapshots.values()
            for fact in facts
        }
        l1_unit_facts = tuple(
            sorted(
                l1_unit_by_id.values(),
                key=lambda item: (item.available_at, item.unit_id),
            )
        )
        l1_unit_ledger = tuple(item.document() for item in l1_unit_facts)
        l1_snapshot_unit_counts = {
            point_id: len(facts) for point_id, facts in l1_snapshots.items()
        }
        unit_diagnostic_source = "FRESH_CAUSAL_PREFIX_DIAGNOSTIC"
    l1_trends = tuple(
        _l1_trend(item) for item in cached["l1_completed_trend_ledger"]
    )
    l2_points = tuple(
        _structural_point(item)
        for item in cached["point_ledgers"]["L2"]
        if item["point_type"] in {"1buy", "2buy"}
    )
    if cache_matches and prior_v31.get("l2_confirmation_bar_ledger"):
        bars = {
            fact.point_id: fact
            for fact in (
                _confirmation_bar(item)
                for item in prior_v31["l2_confirmation_bar_ledger"]
            )
        }
        confirmation_bar_source = "HASH_MATCHED_PRIOR_CAUSAL_LEDGER"
    else:
        if "frames" not in locals():
            frames, interval, adjustment_count = _frames()
        bars = {
            point.point_id: confirmation_bar_fact(point, frames["1m"])
            for point in l2_points
        }
        confirmation_bar_source = "FRESH_RAW_ONE_MINUTE_FRAME"
    if "frames" in locals():
        source_start = interval["start"]
        source_end = interval["end"]
        source_sessions = interval["sessions"]
        rows_by_frequency = {name: len(frame) for name, frame in frames.items()}
        l0_price_quantum = Decimal(
            str(frames["30m"].attrs["structure_price_quantum"])
        )
    else:
        source_start = cached["source_start"]
        source_end = cached["source_end"]
        source_sessions = cached["source_sessions"]
        rows_by_frequency = cached["rows_by_frequency"]
        adjustment_count = cached["adjustment_events_applied"]
        l0_price_quantum = _l0_price_quantum(
            l0_points, l0_center_completions
        )
    decisions = align_v31_independent_entry_chains(
        l0_points=l0_points,
        l0_center_completions=l0_center_completions,
        l1_trends=l1_trends,
        l2_points=l2_points,
        confirmation_bars=bars,
        l0_price_quantum=l0_price_quantum,
    )
    chains = tuple(item.chain for item in decisions if item.chain is not None)
    rejection_counts = Counter(
        reason for decision in decisions for reason in decision.reason_codes
    )
    parameters = v31_parameter_snapshot("ETF_PROXY")
    contract = v31_alignment_contract()
    latest_trend_market_end = max(
        (item.market_end for item in l1_trends), default=None
    )
    latest_trend_available_at = max(
        (item.available_at for item in l1_trends), default=None
    )
    latest_unit_market_end = max(
        (
            _datetime(item["market_end"])
            for item in l1_unit_ledger
            if item.get("market_end") is not None
        ),
        default=None,
    )
    l1_missing_reasons = {
        "NO_COMPLETED_L1_UP_DEPARTURE_ALIGNED_WITH_L0_LEAVE_UNIT",
        "NO_SUBSEQUENT_COMPLETED_L1_DOWN_RETURN",
    }
    frozen_structure_blocked = bool(
        l0_points
        and not chains
        and rejection_counts
        and set(rejection_counts).issubset(l1_missing_reasons)
    )
    result: dict[str, object] = {
        "schema": "chanlun-v31-independent-timeframe-structure/v1",
        "generated_at": datetime.now(CN),
        "strategy": parameters.document(),
        "strategy_parameter_set_id": parameters.parameter_set_id,
        "alignment_contract": contract.document(),
        "alignment_parameter_set_id": contract.parameter_set_id,
        "mapping": {"L0": "30m", "L1": "5m", "L2": "1m"},
        "source_start": source_start,
        "source_end": source_end,
        "source_sessions": source_sessions,
        "rows_by_frequency": rows_by_frequency,
        "adjustment_events_applied": adjustment_count,
        "l0_price_quantum": l0_price_quantum,
        "l0_first_center_third_buy_count": len(l0_points),
        "l0_center_completion_fact_count": len(l0_center_completions),
        "l0_center_completion_source": l0_center_source,
        "l1_completed_trend_count": len(l1_trends),
        "l1_completed_unit_count": len(l1_unit_ledger),
        "l1_evidence_kind": "COMPLETED_TREND",
        "l1_evidence_source": "HASH_VERIFIED_V3_CAUSAL_TREND_LEDGER",
        "l1_constituent_unit_diagnostic_status": (
            "DIAGNOSTIC_ONLY_NOT_ADMISSIBLE_AS_COMPLETE_L1_TREND"
        ),
        "l1_constituent_unit_diagnostic_source": unit_diagnostic_source,
        "l1_snapshot_unit_counts": l1_snapshot_unit_counts,
        "l2_first_or_second_buy_count": len(l2_points),
        "l2_confirmation_bar_fact_count": len(bars),
        "l2_confirmation_bar_source": confirmation_bar_source,
        "alignment_decisions": tuple(item.document() for item in decisions),
        "alignment_rejection_counts": dict(sorted(rejection_counts.items())),
        "aligned_entry_chain_count": len(chains),
        "aligned_entry_chains": tuple(item.document() for item in chains),
        "l0_center_completion_ledger": tuple(
            asdict(item) for item in l0_center_completions
        ),
        "l1_completed_trend_ledger": tuple(
            item.document() for item in l1_trends
        ),
        "l1_completed_unit_ledger": l1_unit_ledger,
        "l2_confirmation_bar_ledger": tuple(
            item.document() for item in bars.values()
        ),
        "parent_v3_structure_sha256": cached["content_sha256"],
        "frozen_structure_sufficiency": {
            "status": (
                "BLOCKED_BY_FROZEN_STRUCTURE"
                if frozen_structure_blocked
                else "SUFFICIENT_FOR_OBSERVED_ENTRY_CHAINS"
            ),
            "reason": (
                "Frozen 5m output contains no complete L1 departure/first-return "
                "sequence aligned by terminal overlap with the L0 completion facts; "
                "the structure core is read-only."
                if frozen_structure_blocked
                else None
            ),
            "latest_completed_l1_trend_market_end": latest_trend_market_end,
            "latest_completed_l1_trend_available_at": latest_trend_available_at,
            "latest_completed_l1_unit_market_end_diagnostic": latest_unit_market_end,
            "source_end": source_end,
            "core_change_permitted": False,
        },
        "market_database_sha256": sha256_file(DEFAULT_MARKET_DATABASE),
        "pit_database_sha256": sha256_file(DEFAULT_PIT_DATABASE),
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    result["decision"] = (
        "V31_CAUSAL_ENTRY_CHAINS_AVAILABLE"
        if chains
        else (
            "BLOCKED_BY_FROZEN_STRUCTURE"
            if frozen_structure_blocked
            else "V31_CAUSAL_ENTRY_CHAIN_ZERO"
        )
    )
    result["content_sha256"] = content_sha256(result)
    return result


def main() -> int:
    result = audit()
    atomic_json(DEFAULT_OUTPUT, result)
    print(
        {
            "output": str(DEFAULT_OUTPUT.resolve()),
            "decision": result["decision"],
            "aligned_entry_chain_count": result["aligned_entry_chain_count"],
            "rejections": result["alignment_rejection_counts"],
            "content_sha256": result["content_sha256"],
        },
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
