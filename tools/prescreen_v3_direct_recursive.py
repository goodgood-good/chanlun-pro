#!/usr/bin/env python3
"""Prescreen the primary 30m/5m/1m direct-recursive V3 structure path.

Every instrument is rebuilt from its longest complete local QMT-backed 1m
interval.  The same immutable 1m graph supplies logical L0=30m, L1=5m and
L2=1m.  Independent physical charts are not used for signal authority.

This tool writes research evidence only.  It does not connect an account,
create an order, or claim performance when no fully qualified entry exists.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    strict_state,
)
from chanlun.decision_support.trading_system.structure_adapter import (  # noqa: E402
    extract_confirmed_points,
)
from chanlun.decision_support.trading_system.v3_direct_recursive_structure import (  # noqa: E402
    build_v3_direct_recursive_structure_path,
)
from chanlun.decision_support.trading_system.v3_qmt_higher_timeframe import (  # noqa: E402
    build_qmt_higher_timeframe_risk,
    qmt_higher_timeframe_inputs,
)
from chanlun.decision_support.trading_system.v3_qmt_same_base_stream import (  # noqa: E402
    build_qmt_same_base_stream_frames,
)
from chanlun.decision_support.trading_system.v3_structure_signal_adapter import (  # noqa: E402
    build_v3_structure_signal_ledger,
    frozen_completed_trend_fact,
)
from tools.chanlun_v3_research_data import (  # noqa: E402
    DEFAULT_PIT_DATABASE,
    atomic_json,
    content_sha256,
    sha256_file,
)
from tools.prescreen_v31_cached_symbols import (  # noqa: E402
    _build_frames,
    provider_to_project_code,
)


DEFAULT_UNIVERSE = Path(
    "audit/chanlun_live_integration/csi300_broad_etf_universe_v1.json"
)
DEFAULT_MARKET_DATABASE = Path(
    ".cache/chanlun_v31_csi300_broad_pool/financial_data_query_bars.sqlite3"
)
DEFAULT_CORPORATE_ACTIONS = Path(
    "audit/chanlun_live_integration/qmt_etf_corporate_actions_v1.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/direct_recursive_v3_etf_prescreen.json"
)


def _formal_structure_counts(
    *,
    formal_chain_eligibility: bool,
    diagnostic_strategic_points: int,
    diagnostic_aligned_entries: int,
    diagnostic_replay_eligible_signals: int,
) -> dict[str, object]:
    """Separate structural diagnostics from data-gate-eligible facts.

    A raw, unadjusted chart remains useful for diagnosing recursive supply, but
    it is not an admissible price basis for a formal candidate.  Keeping both
    counts prevents a missing corporate-action ledger from being silently
    promoted merely because the structure engine produced a point.
    """

    for label, value in (
        ("diagnostic_strategic_points", diagnostic_strategic_points),
        ("diagnostic_aligned_entries", diagnostic_aligned_entries),
        (
            "diagnostic_replay_eligible_signals",
            diagnostic_replay_eligible_signals,
        ),
    ):
        if value < 0:
            raise ValueError(f"{label} cannot be negative")
    eligible = bool(formal_chain_eligibility)
    return {
        "formal_signal_eligible": eligible,
        "formal_signal_gate_reason": (
            "POINT_IN_TIME_CAUSAL_ADJUSTMENT_AVAILABLE"
            if eligible
            else "MISSING_PIT_CAUSAL_ADJUSTMENT_LEDGER"
        ),
        "diagnostic_strategic_point_count": diagnostic_strategic_points,
        "strategic_point_count": (
            diagnostic_strategic_points if eligible else 0
        ),
        "diagnostic_aligned_entry_count": diagnostic_aligned_entries,
        "aligned_entry_count": diagnostic_aligned_entries if eligible else 0,
        "diagnostic_replay_eligible_structure_signal_count": (
            diagnostic_replay_eligible_signals
        ),
        "replay_eligible_structure_signal_count": (
            diagnostic_replay_eligible_signals if eligible else 0
        ),
    }


def _point_document(point) -> dict[str, object]:
    return {
        "point_id": point.point_id,
        "point_type": point.point_type,
        "raw_source_frequency": point.source_frequency,
        "raw_recursive_level": point.recursive_level,
        "logical_level": {2: "L0_30M", 1: "L1_5M", 0: "L2_1M"}.get(
            point.recursive_level,
            "OUTSIDE_V3_MAPPING",
        ),
        "center_id": point.center_id,
        "center_ordinal": point.center_ordinal,
        "anchor_at": point.anchor_at,
        "confirmed_at": point.confirmed_at,
        "available_at": point.available_at,
        "anchor_price": point.structure_anchor_price,
        "invalidation_price": point.structure_invalidation_price,
    }


def _higher_timeframe_risk_documents(
    *,
    code: str,
    one_minute_frame,
    strategic_points,
) -> tuple[dict[str, object], ...]:
    """Evaluate M/W/D risk at each strategic point from the same 1m base.

    The full one-minute frame is supplied once, but every risk adapter applies
    its own ``decision_time`` prefix.  Physical daily and 30m bars therefore
    provide risk/mapping evidence only; they never replace the direct-recursive
    30m/5m/1m signal authority.
    """

    points = tuple(strategic_points)
    if not points:
        return ()
    sessions = tuple(sorted(set(one_minute_frame["date"].dt.date)))
    end = one_minute_frame["date"].iloc[-1].to_pydatetime()
    try:
        same_base = build_qmt_same_base_stream_frames(
            symbol=code,
            one_minute_frame=one_minute_frame,
            decision_time=end,
            expected_sessions=sessions,
        )
    except ValueError as exc:
        # Structure diagnostics remain useful, but invalid OHLCV or a broken
        # session grid must reject every higher-timeframe candidate rather
        # than aborting the universe run or being silently deleted.
        return tuple(
            {
                "strategic_point_id": point.point_id,
                "decision_time": point.available_at,
                "same_base_stream_revision": None,
                "same_base_stream_grade": "UNRESOLVED",
                "risk_grade": "UNRESOLVED",
                "risk_gate": "UNRESOLVED",
                "monthly_state": None,
                "weekly_state": None,
                "daily_state": None,
                "warmup_evidence": None,
                "blocker_codes": ("HIGHER_TIMEFRAME_SAME_BASE_INPUT_INVALID",),
                "blocker_detail": str(exc),
                "physical_chart_role": "HIGHER_TIMEFRAME_RISK_AND_MAPPING_ONLY",
                "signal_authority": "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH",
            }
            for point in points
        )
    documents: list[dict[str, object]] = []
    for point in points:
        decision = point.available_at
        inputs = qmt_higher_timeframe_inputs(
            symbol=code,
            daily_frame=same_base.daily,
            thirty_minute_frame=same_base.thirty_minute,
            decision_time=decision,
        )
        envelope = build_qmt_higher_timeframe_risk(
            inputs=inputs,
            trading_sessions=same_base.complete_sessions,
            calendar_coverage_end=same_base.complete_sessions[-1],
            snapshot_id=f"risk:{code}:{point.point_id}",
        )
        snapshot = envelope.risk.snapshot
        documents.append(
            {
                "strategic_point_id": point.point_id,
                "decision_time": decision,
                "same_base_stream_revision": same_base.source_base_stream_revision,
                "same_base_stream_grade": same_base.grade,
                "risk_grade": envelope.grade,
                "risk_gate": envelope.risk.gate,
                "monthly_state": None if snapshot is None else snapshot.monthly,
                "weekly_state": None if snapshot is None else snapshot.weekly,
                "daily_state": None if snapshot is None else snapshot.daily,
                "warmup_evidence": envelope.warmup.document(),
                "blocker_codes": tuple(value.code for value in envelope.blockers),
                "physical_chart_role": "HIGHER_TIMEFRAME_RISK_AND_MAPPING_ONLY",
                "signal_authority": "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH",
            }
        )
    return tuple(documents)


def _instrument_report(
    *,
    database: Path,
    pit_database: Path,
    corporate_actions: Path,
    provider_symbol: str,
    source_ledger_sha256: str,
) -> dict[str, object]:
    code = provider_to_project_code(provider_symbol)
    frames, interval, adjustment = _build_frames(
        database=database,
        pit_database=pit_database,
        corporate_actions=corporate_actions,
        benchmark_symbol="000300.CSI",
        provider_symbol=provider_symbol,
    )
    frame = frames["1m"]
    state = strict_state(code, "1m", frame)
    state.process_klines(frame)
    evidence = state.get_strict_evidence()
    path = build_v3_direct_recursive_structure_path(
        evidence=evidence,
        code=code,
    )
    higher_timeframe_risk = _higher_timeframe_risk_documents(
        code=code,
        one_minute_frame=frame,
        strategic_points=path.strategic_points,
    )
    points = extract_confirmed_points(
        evidence,
        code=code,
        source_frequency="1m",
        as_of=evidence.source_closed_at,
    )
    points_by_level = {
        level: tuple(point for point in points if point.recursive_level == level)
        for level in (0, 1, 2)
    }
    trends = tuple(
        frozen_completed_trend_fact(trend, source_frequency="1m")
        for level in evidence.structure.levels
        for trend in level.completed_trends
    )
    signal_ledger = build_v3_structure_signal_ledger(
        symbol=code,
        l0_points=points_by_level[2],
        l1_points=points_by_level[1],
        l2_points=points_by_level[0],
        completed_trends=trends,
        l1_center_phases=(),
        execution_facts=(),
        coverage_start=frame["date"].iloc[0].to_pydatetime(),
        coverage_end=frame["date"].iloc[-1].to_pydatetime(),
        source_ledger_sha256=source_ledger_sha256,
        level_relation_mode="DIRECT_RECURSIVE",
    )
    signal_document = signal_ledger.document()
    diagnostic_counts = Counter(
        item.code for item in signal_ledger.diagnostics
    )
    signal_kind_counts = Counter(
        str(item["signal_kind"])
        for item in signal_ledger.structure_signal_facts
    )
    replay_eligible_signals = sum(
        bool(item["emit_to_replay"])
        for item in signal_ledger.structure_signal_facts
    )
    formal_counts = _formal_structure_counts(
        formal_chain_eligibility=bool(adjustment["formal_chain_eligibility"]),
        diagnostic_strategic_points=len(path.strategic_points),
        diagnostic_aligned_entries=path.aligned_entry_count,
        diagnostic_replay_eligible_signals=replay_eligible_signals,
    )
    return {
        "provider_symbol": provider_symbol,
        "project_code": code,
        "source_start": interval["start"],
        "source_end": interval["end"],
        "source_sessions": interval["sessions"],
        "all_complete_runs": interval["all_complete_runs"],
        "rows_1m": len(frame),
        "adjustment_gate": adjustment,
        "price_basis_revision": evidence.price_basis_revision,
        "structure_revision": evidence.structure_revision,
        "structure_levels": tuple(
            {
                "raw_recursive_level": level.structural_level,
                "logical_level": {0: "L2_1M", 1: "L1_5M", 2: "L0_30M"}.get(
                    level.structural_level,
                    "ABOVE_STRATEGY",
                ),
                "unit_count": len(level.units),
                "center_count": len(level.center_result.centers),
                "completed_center_count": sum(
                    center.state.value == "completed"
                    for center in level.center_result.centers
                ),
                "completed_trend_count": len(level.completed_trends),
            }
            for level in evidence.structure.levels
        ),
        "strategic_points": tuple(
            _point_document(point) for point in path.strategic_points
        ),
        "higher_timeframe_risk_at_strategic_points": higher_timeframe_risk,
        "higher_timeframe_risk_gate_counts": dict(
            sorted(
                Counter(
                    str(value["risk_gate"]) for value in higher_timeframe_risk
                ).items()
            )
        ),
        "higher_timeframe_risk_eligible_strategic_point_count": sum(
            value["risk_grade"] == "FULL_SYSTEM_ELIGIBLE"
            and value["risk_gate"] == "GREEN"
            for value in higher_timeframe_risk
        ),
        "alignment_decisions": tuple(
            decision.document() for decision in path.decisions
        ),
        "alignment_rejection_counts": dict(path.rejection_counts),
        "resolved_nine_segment_count": path.resolved_nine_segment_count,
        "unresolved_nine_segment_count": path.unresolved_nine_segment_count,
        "relevant_expansion_count": path.relevant_expansion_count,
        "structure_signal_ledger_sha256": signal_document["content_sha256"],
        "structure_signal_kind_counts": dict(sorted(signal_kind_counts.items())),
        "structure_signal_diagnostic_counts": dict(
            sorted(diagnostic_counts.items())
        ),
        "structure_signal_rule_coverage": dict(signal_ledger.rule_coverage),
        **formal_counts,
        "data_grade": "COMPONENT_ONLY",
        "performance_evaluable": False,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


def build_report(args: argparse.Namespace) -> dict[str, object]:
    universe_path = args.universe.resolve()
    payload = __import__("json").loads(universe_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "chanlun-csi300-broad-etf-universe/v1":
        raise ValueError("unsupported ETF universe artifact")
    available = tuple(item["symbol"] for item in payload["instruments"])
    symbols = tuple(args.symbol) if args.symbol else available
    unknown = tuple(value for value in symbols if value not in available)
    if unknown:
        raise ValueError(f"symbols are outside the frozen ETF universe: {unknown}")

    market_hash = sha256_file(args.database)
    reports_by_symbol: dict[str, dict[str, object]] = {}
    workers = max(1, min(args.workers, len(symbols)))
    with ProcessPoolExecutor(max_workers=workers) as executor:
        jobs = {
            executor.submit(
                _instrument_report,
                database=args.database,
                pit_database=args.pit_database,
                corporate_actions=args.corporate_actions,
                provider_symbol=symbol,
                source_ledger_sha256=market_hash,
            ): symbol
            for symbol in symbols
        }
        for index, future in enumerate(as_completed(jobs), start=1):
            symbol = jobs[future]
            reports_by_symbol[symbol] = future.result()
            print(
                f"[{index}/{len(symbols)}] direct recursive complete {symbol}",
                flush=True,
            )
    reports = tuple(reports_by_symbol[symbol] for symbol in symbols)
    rejection_counts = Counter(
        code
        for report in reports
        for code, count in report["alignment_rejection_counts"].items()
        for _ in range(count)
    )
    data_gate_rejections = Counter(
        str(report["formal_signal_gate_reason"])
        for report in reports
        if not report["formal_signal_eligible"]
    )
    report: dict[str, object] = {
        "schema": "chanlun-v3-direct-recursive-etf-prescreen/v1",
        "scope": "30M_STRATEGIC_5M_TACTICAL_1M_LOCATOR_COMPONENT",
        "logical_level_mapping": {
            "L0_30M_STRATEGIC": "1m graph raw recursive level 2",
            "L1_5M_TACTICAL": "1m graph raw recursive level 1",
            "L2_1M_LOCATOR": "1m graph raw recursive level 0",
        },
        "signal_authority": "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH",
        "physical_chart_role": "CROSS_VALIDATION_ONLY",
        "universe_artifact": str(universe_path),
        "universe_artifact_sha256": sha256_file(universe_path),
        "universe_content_sha256": payload["content_sha256"],
        "universe_symbols": symbols,
        "selection_path": "ETF_PROXY",
        "individual_path": {
            "status": "UNRESOLVED",
            "reason": (
                "HISTORICAL_POINT_IN_TIME_QMT_SECTOR_MEMBERSHIP_AND_SIGNED_"
                "THREE_PROGRAM_ADJUDICATIONS_UNAVAILABLE"
            ),
        },
        "source_database": {
            "market_path": str(args.database.resolve()),
            "market_sha256": market_hash,
            "pit_path": str(args.pit_database.resolve()),
            "pit_sha256": (
                sha256_file(args.pit_database)
                if args.pit_database.exists()
                else "MISSING"
            ),
            "corporate_actions_path": str(args.corporate_actions.resolve()),
            "corporate_actions_sha256": sha256_file(args.corporate_actions),
        },
        "instrument_reports": reports,
        "totals": {
            "instruments": len(reports),
            "adjustment_eligible_instruments": sum(
                bool(report["formal_signal_eligible"]) for report in reports
            ),
            "diagnostic_strategic_points": sum(
                int(report["diagnostic_strategic_point_count"])
                for report in reports
            ),
            "strategic_points": sum(
                int(report["strategic_point_count"]) for report in reports
            ),
            "diagnostic_aligned_entries": sum(
                int(report["diagnostic_aligned_entry_count"])
                for report in reports
            ),
            "aligned_entries": sum(
                int(report["aligned_entry_count"]) for report in reports
            ),
            "strategic_points_with_higher_timeframe_risk": sum(
                len(report["higher_timeframe_risk_at_strategic_points"])
                for report in reports
            ),
            "higher_timeframe_risk_eligible_strategic_points": sum(
                int(
                    report[
                        "higher_timeframe_risk_eligible_strategic_point_count"
                    ]
                )
                for report in reports
            ),
            "higher_timeframe_risk_gate_counts": dict(
                sorted(
                    Counter(
                        str(value["risk_gate"])
                        for report in reports
                        for value in report[
                            "higher_timeframe_risk_at_strategic_points"
                        ]
                    ).items()
                )
            ),
            "alignment_rejection_counts": dict(sorted(rejection_counts.items())),
            "data_gate_rejection_counts": dict(sorted(data_gate_rejections.items())),
            "diagnostic_replay_eligible_structure_signals": sum(
                int(
                    report[
                        "diagnostic_replay_eligible_structure_signal_count"
                    ]
                )
                for report in reports
            ),
            "replay_eligible_structure_signals": sum(
                int(report["replay_eligible_structure_signal_count"])
                for report in reports
            ),
        },
        "data_grade": "COMPONENT_ONLY",
        "performance_evaluable": False,
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
    value.add_argument("--workers", type=int, default=2)
    value.add_argument("--symbol", action="append")
    return value


def main() -> int:
    args = parser().parse_args()
    report = build_report(args)
    atomic_json(args.output, report)
    print(
        f"wrote {args.output}: strategic={report['totals']['strategic_points']}; "
        f"aligned={report['totals']['aligned_entries']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
