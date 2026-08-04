from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user
import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.backtest.data_audit import DataEvidence
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    EquityPoint,
)
from chanlun.decision_support.trading_system.backtest.metrics import calculate_metrics
from chanlun.decision_support.trading_system.backtest.report import (
    BacktestEvaluationResult,
    WalkForwardWindowResult,
    build_report,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    QmtSectorSameBaseCoverageEvidence,
    higher_timeframe_effectiveness_audit,
)
from chanlun.decision_support.trading_system.higher_timeframe_execution_attribution import (
    higher_timeframe_execution_attribution,
)
from chanlun.decision_support.trading_system.v3_etf_proxy_facts import (
    RiskDiagnosticBuyPointEvidenceFacts,
    RiskMappingPointEvidenceFacts,
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.v3_qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
)
from chanlun.research_release.v3_sector_release_manifest import (
    SectorReleaseManifestError,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupMappingSupplySnapshot,
    WarmupPeriodSemanticFacts,
    WarmupPrefixObservation,
    WarmupSemanticSnapshot,
    bind_warmup_convergence_diagnostic,
    bind_warmup_mapping_supply_diagnostic,
    classify_warmup_convergence_envelope,
)
from cl_app.blueprints.decision_support import decision_support_bp
from cl_app.services.research_audit import (
    ResearchAuditUnavailable,
    build_research_audit_snapshot,
    validate_risk_point_chart_lock,
)
from tests.trading_system.backtest.helpers import CN
from tests.trading_system.test_warmup_structure_lineage import lineage_envelope


_CAUSAL_CONTROLS = [
    "survivorship_free_effective_dated_security_master",
    "decision_time_sw1_membership",
    "ex_date_only_causal_price_basis",
    "cash_and_share_corporate_action_accounting",
    "closed_bar_strict_structure_witnesses",
    "next_complete_minute_execution",
    "observed_range_and_volume_fill_guard",
    "delisted_security_zero_recovery",
    "content_addressed_algorithm_data_and_checkpoints",
]
_DATA_SOURCE_HASHES = (
    ("pit_metadata_snapshot", "sha256:" + "1" * 64),
    ("qmt_extract_manifest", "sha256:" + "2" * 64),
    ("prefix_invariance_audit", "sha256:" + "3" * 64),
    ("symbol_fact_checkpoint_tree", "sha256:" + "4" * 64),
    ("sector_fact_checkpoint_tree", "sha256:" + "5" * 64),
    ("certified_portfolio_run", "sha256:" + "6" * 64),
)


class _User(UserMixin):
    id = "researcher"


def _report(
    first_center_selection: bool | None = None,
) -> dict[str, object]:
    generated_at = datetime(2026, 7, 20, 18, 0, tzinfo=CN)
    run = BacktestRun(
        fills=(),
        trades=(),
        equity_curve=(
            EquityPoint(
                generated_at - timedelta(days=30),
                Decimal("100"),
                Decimal("0"),
                Decimal("100"),
                Decimal("0"),
            ),
            EquityPoint(
                generated_at,
                Decimal("101"),
                Decimal("0"),
                Decimal("101"),
                Decimal("0"),
            ),
        ),
        open_positions=(),
        pending_exits=(),
    )
    evaluation = BacktestEvaluationResult(
        aggregate_run=run,
        bootstrap_repetitions=20,
    )
    if first_center_selection is not None:
        window = WalkForwardWindowResult(
            window_id="wf-001",
            train_start=date(2020, 1, 1),
            train_end=date(2022, 12, 31),
            validation_start=date(2023, 1, 6),
            validation_end=date(2023, 7, 5),
            test_start=date(2023, 7, 11),
            test_end=date(2024, 1, 10),
            selected_parameters=(
                ("base_trade_risk", "0.0035"),
                ("first_center_three_buy_only", first_center_selection),
                ("max_portfolio_heat", "0.015"),
                ("first_buy_risk_multiplier", "0.25"),
            ),
            test_metrics=calculate_metrics(run),
            closed_trade_count=0,
        )
        evaluation = replace(evaluation, walk_forward_windows=(window,))
    return build_report(
        evidence=DataEvidence(
            grade="research_only",
            failures=("historical_sector_membership_missing",),
            warnings=(
                "terminal_open_positions_marked_to_market_not_same_bar_liquidated",
            ),
            coverage=(("bar_status_coverage", Decimal("1")),),
        ),
        result=evaluation,
        ablations=(),
        benchmarks=(),
        generated_at=generated_at,
        requested_range=(date(2025, 7, 25), date(2026, 7, 24)),
        effective_range=(date(2025, 8, 1), date(2026, 7, 24)),
        evaluation_mode="fixed_policy_one_year",
        sector_price_source="qmt-sw1-pit-composite",
        algorithm_hashes=(("src/fixture.py", "sha256:" + "a" * 64),),
        data_source_hashes=_DATA_SOURCE_HASHES,
        universe_summary={
            "catalog_source": "qmt_sw1_with_cninfo_effective_dates",
            "eligible_sector_count": 31,
            "sector_composite_member_limit": None,
            "selected_symbol_count": 5201,
            "archived_intersecting_symbol_count": 5227,
            "unclassified_excluded_symbol_count": 26,
            "corporate_action_count": 7763,
            "causal_evaluation_count": 4000,
        },
    )


def _algorithm_revision(report: dict[str, object]) -> str:
    hashes = tuple(
        (str(row["source"]), str(row["sha256"]))
        for row in report["algorithm_hashes"]
    )
    encoded = json.dumps(
        hashes, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_passed_gate(root: Path, report: dict[str, object]) -> None:
    directory = root / "audit/chanlun_trading_system_backtest"
    path = directory / "causality_gate.json"
    report_path = directory / "certified_report.json"
    path.write_text(
        json.dumps(
            {
                "schema": "chanlun-backtest-causality-gate/v2",
                "checked_at": "2026-07-25T12:00:00+08:00",
                "status": "passed",
                "pnl_generated": True,
                "algorithm_revision": _algorithm_revision(report),
                "pit_snapshot_sha256": dict(_DATA_SOURCE_HASHES)[
                    "pit_metadata_snapshot"
                ],
                "validated_symbol_fact_count": 5201,
                "validated_decision_count": 4000,
                "proven_controls": _CAUSAL_CONTROLS,
                "failures": [],
                "report": str(report_path.resolve()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _risk_evidence(
    subject: str,
    *,
    observed_at: datetime,
) -> dict[str, object]:
    blocker = "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
    def semantic_snapshot(
        *,
        weekly_state: str = "FORMED_UNRESOLVED",
        daily_ma5: str = "10",
    ) -> WarmupSemanticSnapshot:
        return WarmupSemanticSnapshot(
            periods=tuple(
                WarmupPeriodSemanticFacts(
                    period=period,
                    state=(
                        weekly_state
                        if period == "W"
                        else "FORMED_UNRESOLVED"
                    ),
                    evidence_bar_end=observed_at - timedelta(days=index + 1),
                    active_top_interval=None,
                    mapping_unique=False,
                    mapped_center_id=None,
                    mapping_candidate_ids=(),
                    blocker_codes=(blocker,),
                    warning_codes=(),
                )
                for index, period in enumerate(("M", "W", "D"))
            ),
            ma5=(
                ("M", Decimal("8")),
                ("W", Decimal("9")),
                ("D", Decimal(daily_ma5)),
            ),
        )

    baseline = semantic_snapshot()
    intermediate = semantic_snapshot(weekly_state="NONE", daily_ma5="11")
    snapshots = (baseline, intermediate, baseline, baseline)
    convergence = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
        observations=tuple(
            WarmupPrefixObservation(
                bar_count=count,
                starts_at=observed_at - timedelta(days=count),
                signature_sha256=snapshot.signature_sha256,
            )
            for count, snapshot in zip((480, 640, 800, 960), snapshots)
        ),
    )
    convergence = bind_warmup_convergence_diagnostic(
        convergence,
        snapshots=snapshots,
    )
    assert convergence.diagnostic is not None
    evidence: dict[str, object] = {
        "source_revision": "sha256:" + "7" * 64,
        "monthly": "FORMED_UNRESOLVED",
        "weekly": "FORMED_UNRESOLVED",
        "daily": "FORMED_UNRESOLVED",
        "grade": "RESEARCH_ONLY",
        "period_diagnostics": [
            {
                "period": period,
                "state": "FORMED_UNRESOLVED",
                "blocker_codes": [blocker],
            }
            for period in ("M", "W", "D")
        ],
        "session_evidence": {"issues": []},
        "warmup": {
            "reason_code": "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
        },
        "warmup_convergence": convergence.document(),
        "warmup_convergence_diagnostic": convergence.diagnostic.document(),
        "source_mode": (
            "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH"
            if subject == "sector"
            else None
        ),
        "strict_same_5m_warmup": (
            {
                "converged": False,
                "reason_code": (
                    "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
                ),
            }
            if subject == "sector"
            else None
        ),
    }
    if subject == "sector":
        evidence["strict_same_5m_warmup"] = {
            "converged": False,
            "reason_code": "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
            "full_daily_bar_count": 236,
            "required_daily_bar_count": 480,
        }
        evidence["strict_same_5m_source_coverage"] = (
            QmtSectorSameBaseCoverageEvidence(
                observed_at=observed_at,
                calendar_first_session=date(2023, 5, 4),
                first_visible_bar_at=datetime.fromisoformat(
                    "2025-04-29T11:05:00+08:00"
                ),
                last_visible_bar_at=observed_at,
                first_completed_session=date(2025, 4, 30),
                last_completed_session=observed_at.date() - timedelta(days=1),
                visible_five_minute_bar_count=11328,
                completed_daily_bar_count=236,
                required_daily_bar_count=480,
                remaining_daily_bar_count=244,
                missing_leading_calendar_session_count=480,
                warmup_converged=False,
                warmup_reason_code=(
                    "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
                ),
                boundary_status=(
                    "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP"
                ),
                physical_source_boundary_status=(
                    "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
                ),
                physical_source_requested_start_at=datetime.fromisoformat(
                    "2023-05-01T09:30:00+08:00"
                ),
                physical_source_required_contributor_start_at=(
                    datetime.fromisoformat("2025-04-30T10:50:00+08:00")
                ),
                physical_source_representative_member_count=24,
                physical_source_available_member_count=23,
                physical_source_required_contributor_count=15,
                physical_source_inventory_revision="sha256:" + "6" * 64,
            ).document()
        )
        strict_convergence = classify_warmup_convergence_envelope(
            frequency="d",
            as_of=observed_at,
            parameter_set_id=(
                QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
            ),
            observations=(),
        )
        strict_convergence = bind_warmup_convergence_diagnostic(
            strict_convergence,
            snapshots=(),
        )
        assert strict_convergence.diagnostic is not None
        evidence["strict_same_5m_warmup_convergence"] = (
            strict_convergence.document()
        )
        evidence["strict_same_5m_warmup_convergence_diagnostic"] = (
            strict_convergence.diagnostic.document()
        )
    return evidence


def _warmup_mapping_supply(
    *,
    observed_at: datetime,
    unique_sell: bool,
) -> RiskMappingSupplyFacts:
    center_id = "sha256:" + ("a" if unique_sell else "b") * 64
    anchor = observed_at - timedelta(days=15 if unique_sell else 14)
    available = anchor + timedelta(days=1)
    point_type = "1sell" if unique_sell else "3buy"
    point = RiskMappingPointEvidenceFacts(
        point_id=RiskMappingPointEvidenceFacts.identity(
            source_symbol="SH.000001",
            source_frequency="d",
            center_id=center_id,
            center_level_rank=1,
            point_type=point_type,
            point_anchor_at=anchor,
            point_available_at=available,
        ),
        source_symbol="SH.000001",
        source_frequency="d",
        center_id=center_id,
        center_level_rank=1,
        center_completed=True,
        center_expanded=False,
        point_type=point_type,  # type: ignore[arg-type]
        point_anchor_at=anchor,
        point_available_at=available,
        inside_active_top_interval=True,
        highest_mapping_candidate=unique_sell,
    )
    return RiskMappingSupplyFacts(
        classification=(
            "UNIQUE_MAPPING" if unique_sell else "ONLY_THIRD_CLASS_POINTS"
        ),
        lower_structure_available=True,
        point_evidence_count=1,
        point_type_counts=(
            ("1sell", int(unique_sell)),
            ("2sell", 0),
            ("3sell", 0),
            ("3buy", int(not unique_sell)),
        ),
        completed_sell12_count=int(unique_sell),
        in_top_interval_sell12_count=int(unique_sell),
        completed_in_top_interval_sell12_count=int(unique_sell),
        incomplete_in_top_interval_sell12_count=0,
        outside_top_interval_sell12_count=0,
        highest_candidate_center_count=int(unique_sell),
        point_evidence=(point,),
        diagnostic_buy_point_type_counts=(("1buy", 0), ("2buy", 0)),
        diagnostic_buy_point_evidence=(),
    )


def _risk_evidence_with_warmup_mapping_supply_delta(
    *,
    observed_at: datetime,
) -> dict[str, object]:
    """One realistic NON_MONOTONIC case: a shorter-prefix 1sell disappears."""

    blocker = "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
    unique_center_id = "sha256:" + "a" * 64

    def snapshot(*, unique_sell: bool, daily_ma5: str) -> WarmupSemanticSnapshot:
        periods = []
        for index, period in enumerate(("M", "W", "D")):
            is_weekly = period == "W"
            periods.append(
                WarmupPeriodSemanticFacts(
                    period=period,
                    state=(
                        "FORMED"
                        if is_weekly and unique_sell
                        else "FORMED_UNRESOLVED"
                    ),
                    evidence_bar_end=observed_at - timedelta(days=index + 1),
                    active_top_interval=(
                        (
                            observed_at - timedelta(days=30),
                            observed_at - timedelta(days=5),
                        )
                        if is_weekly
                        else None
                    ),
                    mapping_unique=unique_sell if is_weekly else False,
                    mapped_center_id=(
                        unique_center_id if is_weekly and unique_sell else None
                    ),
                    mapping_candidate_ids=(
                        (unique_center_id,)
                        if is_weekly and unique_sell
                        else ()
                    ),
                    blocker_codes=(
                        () if is_weekly and unique_sell else (blocker,)
                    ),
                    warning_codes=(),
                )
            )
        return WarmupSemanticSnapshot(
            periods=tuple(periods),
            ma5=(
                ("M", Decimal("8")),
                ("W", Decimal("9")),
                ("D", Decimal(daily_ma5)),
            ),
        )

    reference = snapshot(unique_sell=False, daily_ma5="10")
    intermediate = snapshot(unique_sell=True, daily_ma5="11")
    snapshots = (reference, intermediate, reference, reference)
    convergence = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
        observations=tuple(
            WarmupPrefixObservation(
                bar_count=count,
                starts_at=observed_at - timedelta(days=count),
                signature_sha256=value.signature_sha256,
            )
            for count, value in zip((480, 620, 800, 960), snapshots)
        ),
    )
    convergence = bind_warmup_convergence_diagnostic(
        convergence,
        snapshots=snapshots,
    )
    unavailable = _warmup_mapping_supply(
        observed_at=observed_at,
        unique_sell=False,
    )
    unique = _warmup_mapping_supply(
        observed_at=observed_at,
        unique_sell=True,
    )
    convergence = bind_warmup_mapping_supply_diagnostic(
        convergence,
        snapshots=tuple(
            WarmupMappingSupplySnapshot(
                periods=(("M", None), ("W", supply), ("D", None))
            )
            for supply in (unavailable, unique, unavailable, unavailable)
        ),
    )
    assert convergence.diagnostic is not None
    assert convergence.mapping_supply_diagnostic is not None

    evidence = _risk_evidence("market", observed_at=observed_at)
    evidence["warmup_convergence"] = convergence.document()
    evidence["warmup_convergence_diagnostic"] = (
        convergence.diagnostic.document()
    )
    evidence["warmup_mapping_supply_diagnostic"] = (
        convergence.mapping_supply_diagnostic.document()
    )
    lineage = lineage_envelope(
        as_of=observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
    )
    assert lineage.diagnostic is not None
    assert lineage.mapping_supply_diagnostic is not None
    assert lineage.structure_lineage_diagnostic is not None
    evidence["warmup_convergence"] = lineage.document()
    evidence["warmup_convergence_diagnostic"] = lineage.diagnostic.document()
    evidence["warmup_mapping_supply_diagnostic"] = (
        lineage.mapping_supply_diagnostic.document()
    )
    evidence["warmup_structure_lineage_diagnostic"] = (
        lineage.structure_lineage_diagnostic.document()
    )
    return evidence


def _current_candidate_audit() -> list[dict[str, object]]:
    blocker = "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
    candidates: list[dict[str, object]] = []
    for index in range(17):
        symbol_resolved = index < 12
        identity = f"{index:064x}"
        observed_at = datetime.fromisoformat(
            f"2026-07-{index + 1:02d}T10:00:00+08:00"
        )
        candidates.append(
            {
                "symbol": f"SZ.{index:06d}",
                "sector_id": "qmt-gics3:test",
                "decision_at": observed_at.isoformat(),
                "structure_snapshot_id": "sha256:" + identity,
                "l0_point_id": "sha256:" + identity,
                "accepted": symbol_resolved,
                "exact_green": False,
                "sector_eligible": True,
                "sector_hard_block": False,
                "market_risk_gate": "AMBER",
                "sector_risk_gate": "AMBER",
                "symbol_risk_gate": (
                    "AMBER" if symbol_resolved else "UNRESOLVED"
                ),
                "market_risk_blocker_codes": [blocker],
                "sector_risk_blocker_codes": [
                    blocker,
                    "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE",
                ],
                "symbol_risk_blocker_codes": (
                    [blocker]
                    if symbol_resolved
                    else ["QMT_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE"]
                ),
                "market_risk_warmup_evidence": _risk_evidence(
                    "market",
                    observed_at=observed_at,
                ),
                "sector_risk_warmup_evidence": _risk_evidence(
                    "sector",
                    observed_at=observed_at,
                ),
                "symbol_risk_warmup_evidence": (
                    _risk_evidence("symbol", observed_at=observed_at)
                    if symbol_resolved
                    else None
                ),
            }
        )
    return candidates


def _current_report() -> dict[str, object]:
    candidates = _current_candidate_audit()
    persistent_id = (
        "persistent:SZ.000001:TACTICAL_SELL:sha256:" + "8" * 64
    )
    curve = {
        "status": "EVALUATED_SAMPLE_INSUFFICIENT",
        "start": "2025-08-01",
        "end": "2026-07-24",
        "observations": 237,
        "net_return": "0.02",
        "annualized_return": "0.021",
        "max_drawdown": "0.12",
        "sharpe": "0.30",
        "adjudication_reason": (
            "STRATEGIC_SAMPLE_BELOW_100+TACTICAL_SAMPLE_BELOW_200"
        ),
    }
    accounting = {**curve, "status": "EVALUATED"}
    accounting.pop("adjudication_reason")
    metrics = {
        "annualized_return": None,
        "empty_replay": False,
        "fill_count": 2,
        "ledger_valid": True,
        "max_drawdown": "0.13",
        "net_return": "0.02",
        "open_cycle_count": 1,
        "order_count": 12,
        "payoff_ratio": None,
        "performance_evaluable": True,
        "profit_factor": None,
        "rejection_count": 1,
        "sharpe": "0.30",
        "strategic_cycle_count": 1,
        "strategic_sample_insufficient": True,
        "tactical_cycle_count": 0,
        "tactical_sample_insufficient": True,
        "total_fees": "5",
        "turnover": "0.10",
        "valid": True,
        "warnings": ["INSUFFICIENT_CALENDAR_SPAN_FOR_ANNUALIZATION"],
        "win_rate": "0",
    }
    def entry_execution_id(index: int) -> str:
        return f"v3-replay-order:{index:064x}:bar:{index + 1}"

    def entry_cycle_id(index: int) -> str:
        return (
            f"v3-cycle:{candidates[index]['symbol']}:"
            f"{entry_execution_id(index)}"
        )

    def entry_order(index: int) -> dict[str, object]:
        candidate = candidates[index]
        decision_at = datetime.fromisoformat(str(candidate["decision_at"]))
        filled = index < 2
        fills = (
            [
                {
                    "execution_id": entry_execution_id(index),
                    "exchange_time": (
                        decision_at + timedelta(minutes=2)
                    ).isoformat(),
                    "quantity": 100,
                }
            ]
            if filled
            else []
        )
        return {
            "event_id": f"fixture:entry:{index}",
            "intent_action": "ENTRY_INTENT",
            "order": {
                "symbol": candidate["symbol"],
                "created_at": candidate["decision_at"],
                "structure_snapshot_id": candidate[
                    "structure_snapshot_id"
                ],
            },
            "match": {
                "order_id": f"fixture-order:{index}",
                "state": "O_FILLED" if filled else "O_IDLE",
                "filled_quantity": 100 if filled else 0,
                "fills": fills,
                "rejection_and_unfilled_reasons": (
                    []
                    if filled
                    else ["ORDER_EXPIRED_WITH_UNFILLED_QUANTITY"]
                ),
            },
        }

    entry_orders = [entry_order(index) for index in range(12)]
    closed_cycle = {
        "symbol": "SZ.000000",
        "cycle_id": entry_cycle_id(0),
        "slot_number": 1,
        "opened_at": "2026-07-01T10:02:00+08:00",
        "closed_at": "2026-07-10T10:00:00+08:00",
        "entry_cash": "100000",
        "net_pnl": "-1000",
    }
    open_position = {
        "symbol": "SZ.000001",
        "cycle_id": entry_cycle_id(1),
        "slot_number": 2,
        "opened_at": "2026-07-02T10:02:00+08:00",
        "quantity": 10000,
        "entry_cash": "189000",
        "cumulative_cash_flow": "-189000",
        "cumulative_fees": "5",
        "turnover_notional": "189000",
        "tactical_cycles_completed": 0,
        "last_price": "21",
        "market_value": "210000",
        "marked_at": "2026-07-24T15:00:00+08:00",
        "mark_complete": True,
    }
    replay = {
        "initial_cash": "1000000",
        "final_cash": "810000",
        "closed_cycles": [closed_cycle],
        "contract": {
            "l0_source_frequency": "30m",
            "l1_source_frequency": "5m",
            "l2_source_frequency": "1m",
            "selection_path": "QMT_CURRENT_SECTOR_TECHNICAL_ONLY",
            "result_status": "RESEARCH_ONLY",
            "live_status": "LIVE_DISABLED",
            "level_relation_mode": "CAUSAL_CONFIRMED_POINT_APPROXIMATION",
        },
        "intents": [{}, {}],
        "live_status": "LIVE_DISABLED",
        "metrics": metrics,
        "orders": entry_orders,
        "positions": [open_position],
        "equity_curve": [
            {
                "observed_at": "2026-07-24T15:00:00+08:00",
                "cash": "810000",
                "market_value": "210000",
                "equity": "1020000",
                "complete": True,
                "reason_codes": [],
            }
        ],
        "rejections": [{}],
        "resolved_persistent_intent_ids": [persistent_id],
        "result_status": "RESEARCH_ONLY",
        "suppressed_persistent_event_counts": [[persistent_id, 3]],
    }
    no_tactical_replay = copy.deepcopy(replay)
    no_tactical_replay["resolved_persistent_intent_ids"] = []
    no_tactical_replay["suppressed_persistent_event_counts"] = []
    exact_green_replay = copy.deepcopy(replay)

    def ablation_scheduler(
        *,
        schedule_digit: str,
        resolved_ids: list[str],
        initial_events: int,
        final_events: int,
        removed_events: int,
    ) -> dict[str, object]:
        return {
            "schema": "chanlun-v3-session-checkpoint-scheduler-audit/v1",
            "mode": "HISTORICAL_SESSION_CHECKPOINT_FIXED_POINT",
            "converged": True,
            "build_pass_count": 2,
            "replay_pass_count": 1,
            "maximum_build_passes": 7,
            "source_signal_count": 2,
            "initial_event_count": initial_events,
            "final_event_count": final_events,
            "newly_exposed_event_count": 0,
            "removed_stale_retry_event_count": removed_events,
            "resolved_persistent_signal_count": len(resolved_ids),
            "resolution_sessions": [
                {
                    "persistent_intent_id": identity,
                    "resolved_on": "2026-07-20",
                    "retirement_effective_after": "2026-07-20",
                }
                for identity in resolved_ids
            ],
            "event_schedule_sha256": "sha256:" + schedule_digit * 64,
            "retirement_boundary": (
                "RESOLUTION_SESSION_END_EFFECTIVE_NEXT_SESSION"
            ),
            "forward_checkpoint_equivalent": True,
            "parameters_changed": False,
            "live_status": "LIVE_DISABLED",
        }
    terminal_accounting = {
        "schema": "chanlun-v3-terminal-accounting-attribution/v1",
        "status": "EVALUATED",
        "reason_codes": [],
        "sector_membership_mode": (
            "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
        ),
        "terminal": {
            "observed_at": "2026-07-24T15:00:00+08:00",
            "initial_cash": "1000000",
            "final_cash": "810000",
            "cash": "810000",
            "market_value": "210000",
            "equity": "1020000",
            "total_net_pnl": "20000",
        },
        "pnl_decomposition": {
            "closed_cycle_realized_net_pnl": "-1000",
            "open_cycle_marked_net_pnl": "21000",
            "pure_unrealized_net_pnl": None,
            "pure_unrealized_reason": (
                "OPEN_CYCLE_TACTICAL_AND_CORPORATE_CASH_FLOWS_REQUIRE_"
                "A_SEPARATE_COST_BASIS_LEDGER"
            ),
            "identity_difference": "0",
        },
        "accounting_identity": {
            "cash_market_equity_difference": "0",
            "terminal_cash_difference": "0",
            "position_market_value_difference": "0",
        },
        "concentration": {
            "open_position_count": 1,
            "max_symbol": "SZ.000001",
            "max_symbol_equity_fraction": (
                "0.2058823529411764705882352941"
            ),
            "max_symbol_invested_fraction": "1",
            "symbol_invested_hhi": "1",
            "max_sector_id": "QMT_GICS3:bank",
            "max_sector_equity_fraction": (
                "0.2058823529411764705882352941"
            ),
            "max_sector_invested_fraction": "1",
            "sector_invested_hhi": "1",
        },
        "closed_cycles": [
            {
                "symbol": "SZ.000000",
                "sector_id": "QMT_GICS3:bank",
                "sector_name": "银行",
                "cycle_id": entry_cycle_id(0),
                "slot_number": 1,
                "opened_at": "2026-07-01T10:02:00+08:00",
                "closed_at": "2026-07-10T10:00:00+08:00",
                "entry_cash": "100000",
                "realized_net_pnl": "-1000",
            }
        ],
        "open_positions": [
            {
                "symbol": "SZ.000001",
                "sector_id": "QMT_GICS3:bank",
                "sector_name": "银行",
                "cycle_id": entry_cycle_id(1),
                "slot_number": 2,
                "opened_at": "2026-07-02T10:02:00+08:00",
                "quantity": 10000,
                "entry_cash": "189000",
                "cumulative_cash_flow": "-189000",
                "cumulative_fees": "5",
                "turnover_notional": "189000",
                "tactical_cycles_completed": 0,
                "last_price": "21",
                "market_value": "210000",
                "marked_at": "2026-07-24T15:00:00+08:00",
                "marked_net_pnl": "21000",
                "account_equity_fraction": (
                    "0.2058823529411764705882352941"
                ),
                "invested_market_value_fraction": "1",
            }
        ],
        "sector_attribution": [
            {
                "sector_id": "QMT_GICS3:bank",
                "sector_name": "银行",
                "closed_cycle_count": 1,
                "closed_cycle_realized_net_pnl": "-1000",
                "open_position_count": 1,
                "open_market_value": "210000",
                "open_cycle_marked_net_pnl": "21000",
                "total_attributed_net_pnl": "20000",
                "open_market_value_account_equity_fraction": (
                    "0.2058823529411764705882352941"
                ),
                "open_market_value_invested_fraction": "1",
            }
        ],
        "disclosures": [
            "closed-cycle P&L is realised net cash after fees",
            (
                "open-cycle marked P&L equals cumulative cycle cash flow "
                "plus terminal market value"
            ),
            (
                "pure unrealised P&L is unresolved without a separate "
                "cost-basis ledger"
            ),
            (
                "sector attribution uses the explicitly authorised "
                "current-member backfill"
            ),
        ],
    }
    report: dict[str, object] = {
        "schema": "chanlun-v3-sector-first-full-market-research-backtest/v2",
        "result_label": "RECENT_YEAR_APPROXIMATE_CHANLUN_POINT_RESEARCH_BACKTEST",
        "forward_paper_session": None,
        "strict_full_system_result": {
            "status": "NOT_EVALUABLE",
            "reason": "TECHNICAL_POINTS_ARE_EXPLICIT_RESEARCH_APPROXIMATIONS",
            "replay": {},
        },
        "research_variant_result": {
            "replay": replay,
            "terminal_accounting_attribution": terminal_accounting,
            "performance_status": "EVALUATED_SAMPLE_INSUFFICIENT",
            "performance_reason": (
                "STRATEGIC_SAMPLE_BELOW_100+TACTICAL_SAMPLE_BELOW_200"
            ),
            "daily_metrics": curve,
            "accounting_curve_metrics": accounting,
            "periods": {
                "train_validation_holdout": {
                    "FINAL_HOLDOUT_20": {
                        "status": "EVALUATED",
                        "start": "2026-05-18",
                        "end": "2026-07-24",
                        "observations": 49,
                        "net_return": "-0.01",
                        "annualized_return": "-0.05",
                        "max_drawdown": "0.12",
                        "sharpe": "-0.4",
                    }
                }
            },
        },
        "ablations": {
            "NO_TACTICAL": no_tactical_replay,
            "EXACT_GREEN_HIGHER_TIMEFRAME_ONLY": exact_green_replay,
        },
        "ablation_scheduler_causality_audits": {
            "NO_TACTICAL": ablation_scheduler(
                schedule_digit="b",
                resolved_ids=[],
                initial_events=2,
                final_events=2,
                removed_events=0,
            ),
            "EXACT_GREEN_HIGHER_TIMEFRAME_ONLY": ablation_scheduler(
                schedule_digit="c",
                resolved_ids=[persistent_id],
                initial_events=5,
                final_events=2,
                removed_events=3,
            ),
        },
        "benchmarks": [
            {
                "symbol": "SH.000300",
                "definition": "RAW_CLOSE_PRICE_RETURN_NO_DIVIDEND_REINVESTMENT",
                "metrics": {
                    "status": "EVALUATED",
                    "start": "2025-08-01",
                    "end": "2026-07-24",
                    "observations": 237,
                    "net_return": "0.14",
                    "annualized_return": "0.15",
                    "max_drawdown": "0.10",
                    "sharpe": "0.9",
                },
            }
        ],
        "candidate_audit": candidates,
        "structural_rejections": [],
        "signal_counts": {},
        "scheduler_causality_audit": {
            "schema": "chanlun-v3-session-checkpoint-scheduler-audit/v1",
            "mode": "HISTORICAL_SESSION_CHECKPOINT_FIXED_POINT",
            "converged": True,
            "build_pass_count": 3,
            "replay_pass_count": 2,
            "maximum_build_passes": 7,
            "source_signal_count": 2,
            "initial_event_count": 5,
            "final_event_count": 2,
            "newly_exposed_event_count": 0,
            "removed_stale_retry_event_count": 3,
            "resolved_persistent_signal_count": 1,
            "resolution_sessions": [
                {
                    "persistent_intent_id": persistent_id,
                    "resolved_on": "2026-07-20",
                    "retirement_effective_after": "2026-07-20",
                }
            ],
            "event_schedule_sha256": "sha256:" + "a" * 64,
            "retirement_boundary": (
                "RESOLUTION_SESSION_END_EFFECTIVE_NEXT_SESSION"
            ),
            "forward_checkpoint_equivalent": True,
            "parameters_changed": False,
            "live_status": "LIVE_DISABLED",
        },
        "tactical_execution_audit": {
            "schema": "chanlun-v3-tactical-execution-audit/v1",
            "adjudication": "NO_LEGAL_TACTICAL_LOT",
            "generated_signal_count": 1,
            "dispatched_source_signal_count": 1,
            "decision_record_count": 1,
            "order_count": 0,
            "fill_count": 0,
            "suppressed_retry_count": 3,
            "completed_tactical_cycle_count": 0,
            "disposition_counts": {"NO_EXECUTABLE_TACTICAL_LOT": 1},
            "signals": [
                {
                    "symbol": "SZ.000001",
                    "kind": "TACTICAL_SELL",
                    "observed_at": "2026-07-20T14:00:00+08:00",
                    "disposition": "NO_EXECUTABLE_TACTICAL_LOT",
                    "decision_actions": ["WAIT"],
                    "reason_codes": ["NO_SELLABLE_TACTICAL_INVENTORY"],
                    "decision_record_count": 1,
                    "order_count": 0,
                    "fill_count": 0,
                    "suppressed_retry_count": 3,
                    "persistent_intent_id": persistent_id,
                    "signal_identity": "sha256:" + "8" * 64,
                    "structure_snapshot_id": "sha256:" + "9" * 64,
                }
            ],
        },
        "candidate_funnel": {
            "all_market_sector_classified_symbols": 5000,
            "three_program_prefiltered_symbols": None,
            "terminal_recursive_potential_symbols": 28,
            "causally_rescanned_symbols": 28,
            "causal_technical_entry_count": 17,
            "accepted_candidate_count": 12,
            "order_count": 12,
            "fill_count": 2,
        },
        "higher_timeframe_gate_distribution": {
            "market": {"AMBER": 17},
            "sector": {"AMBER": 17},
            "symbol": {"AMBER": 12, "UNRESOLVED": 5},
        },
        "higher_timeframe_effectiveness_audit": (
            higher_timeframe_effectiveness_audit(candidates)
        ),
        "sector_higher_timeframe_source_distribution": {
            "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH": 17
        },
        "scope": {
            "all_market_scope_symbols": 5000,
            "causal_extracted_symbols": 28,
            "audited_direct_symbols": 28,
            "signal_source_symbols": 12,
            "replay_symbols": ["SZ.000001"],
            "carried_holding_symbols": [],
            "sector_count": 56,
            "selection_order": ["QMT_CURRENT_SECTOR_TRIGGER"],
        },
        "higher_timeframe_data_provenance": {},
        "input_hashes": {"trigger_ledger": "sha256:" + "1" * 64},
        "decision_source_snapshot": {
            "schema": "chanlun-v3-decision-source-snapshot/v2",
            "files": [],
            "aggregate_sha256": "sha256:" + "2" * 64,
        },
        "parameter_snapshots": {
            "strategy_parameter_set_id": "sha256:" + "3" * 64,
            "research_parameter_set_id": "sha256:" + "4" * 64,
            "technical_alignment_parameter_set_id": "sha256:" + "5" * 64,
            "replay_contract_parameter_set_id": "sha256:" + "6" * 64,
            "research_variant": {
                "strategic_frequency": "30m",
                "tactical_frequency": "5m",
                "locator_frequency": "1m",
                "selection_path": "QMT_CURRENT_SECTOR_TECHNICAL_ONLY",
                "sector_membership_mode": (
                    "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
                ),
                "three_program_mode": "DISABLED_USER_AUTHORIZED",
                "tick_data_used": False,
                "live_status": "LIVE_DISABLED",
                "requested_start": "2025-07-25",
                "requested_end": "2026-07-24",
                "effective_start": "2025-08-01",
                "warmup_start": "2023-05-01",
                "sector_taxonomy": "QMT_GICS3",
            },
        },
        "approximation_disclosures": ["current membership backfilled"],
        "causality_guards": ["completed bars only"],
        "sample_warnings": [
            "STRATEGIC_SAMPLE_BELOW_100",
            "TACTICAL_SAMPLE_BELOW_200",
        ],
        "data_grade": "RESEARCH_APPROXIMATION",
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    report["higher_timeframe_execution_attribution"] = (
        higher_timeframe_execution_attribution(
            candidates,
            replay,
            terminal_accounting,
        )
    )
    report["content_sha256"] = sha256_json(report)
    return report


def _write_current_report(root: Path) -> Path:
    path = (
        root
        / "audit/chanlun_trading_system_backtest"
        / "recent_year_current_sector_no3p_mwd_strength"
        / "approximate_technical_backtest_sector_mwd_strength_tactical_lifecycle.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_current_report(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _write_release_bound_current_report(root: Path) -> Path:
    report = _current_report()
    report["input_hashes"].update(
        {
            "direct_manifest": "sha256:" + "d" * 64,
            "terminal_query_plan": "sha256:" + "e" * 64,
        }
    )
    report.pop("content_sha256", None)
    report["content_sha256"] = sha256_json(report)
    path = (
        root
        / "audit/chanlun_trading_system_backtest"
        / "recent_year_current_sector_no3p_mwd_strength"
        / "approximate_technical_backtest_sector_mwd_strength_tactical_lifecycle.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _write_current_report_with_warmup_mapping_supply_delta(
    root: Path,
) -> Path:
    report = _current_report()
    candidate = report["candidate_audit"][0]
    observed_at = datetime.fromisoformat(candidate["decision_at"])
    candidate["market_risk_warmup_evidence"] = (
        _risk_evidence_with_warmup_mapping_supply_delta(
            observed_at=observed_at,
        )
    )
    report["higher_timeframe_effectiveness_audit"] = (
        higher_timeframe_effectiveness_audit(report["candidate_audit"])
    )
    report.pop("content_sha256", None)
    report["content_sha256"] = sha256_json(report)
    path = (
        root
        / "audit/chanlun_trading_system_backtest"
        / "recent_year_current_sector_no3p_mwd_strength"
        / "approximate_technical_backtest_sector_mwd_strength_tactical_lifecycle.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _write_current_report_with_identified_risk_point(root: Path) -> tuple[Path, str]:
    report = _current_report()
    candidate = report["candidate_audit"][0]
    diagnostic = candidate["market_risk_warmup_evidence"]["period_diagnostics"][0]
    anchor = datetime.fromisoformat("2026-02-03T15:00:00+08:00")
    available = datetime.fromisoformat("2026-02-04T15:00:00+08:00")
    point_id = RiskMappingPointEvidenceFacts.identity(
        source_symbol="SH.000001",
        source_frequency="w",
        center_id="fixture-market-center",
        center_level_rank=2,
        point_type="3sell",
        point_anchor_at=anchor,
        point_available_at=available,
    )
    point = RiskMappingPointEvidenceFacts(
        point_id=point_id,
        source_symbol="SH.000001",
        source_frequency="w",
        center_id="fixture-market-center",
        center_level_rank=2,
        center_completed=True,
        center_expanded=False,
        point_type="3sell",
        point_anchor_at=anchor,
        point_available_at=available,
        inside_active_top_interval=True,
        highest_mapping_candidate=False,
    )
    diagnostic.update(
        {
            "active_top_interval": [
                "2026-01-01T15:00:00+08:00",
                "2026-03-31T15:00:00+08:00",
            ],
            "mapping_unique": False,
            "mapping_candidate_ids": [],
            "mapping_supply": {
                "classification": "ONLY_THIRD_CLASS_POINTS",
                "lower_structure_available": True,
                "point_evidence_count": 1,
                "point_type_counts": {
                    "1sell": 0,
                    "2sell": 0,
                    "3sell": 1,
                    "3buy": 0,
                },
                "completed_sell12_count": 0,
                "in_top_interval_sell12_count": 0,
                "completed_in_top_interval_sell12_count": 0,
                "incomplete_in_top_interval_sell12_count": 0,
                "outside_top_interval_sell12_count": 0,
                "highest_candidate_center_count": 0,
                "point_evidence": [point.document()],
            },
        }
    )
    report["higher_timeframe_effectiveness_audit"] = (
        higher_timeframe_effectiveness_audit(report["candidate_audit"])
    )
    report.pop("content_sha256", None)
    report["content_sha256"] = sha256_json(report)
    path = (
        root
        / "audit/chanlun_trading_system_backtest"
        / "recent_year_current_sector_no3p_mwd_strength"
        / "approximate_technical_backtest_sector_mwd_strength_tactical_lifecycle.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path, point_id


def _write_current_report_with_identified_diagnostic_buy_point(
    root: Path,
) -> tuple[Path, str]:
    report = _current_report()
    candidate = report["candidate_audit"][0]
    diagnostic = candidate["market_risk_warmup_evidence"][
        "period_diagnostics"
    ][0]
    anchor = datetime.fromisoformat("2026-02-03T15:00:00+08:00")
    available = datetime.fromisoformat("2026-02-04T15:00:00+08:00")
    point_id = RiskDiagnosticBuyPointEvidenceFacts.identity(
        source_symbol="SH.000001",
        source_frequency="w",
        center_id="fixture-market-diagnostic-center",
        center_level_rank=2,
        point_type="1buy",
        point_anchor_at=anchor,
        point_available_at=available,
    )
    point = RiskDiagnosticBuyPointEvidenceFacts(
        point_id=point_id,
        source_symbol="SH.000001",
        source_frequency="w",
        center_id="fixture-market-diagnostic-center",
        center_level_rank=2,
        center_completed=True,
        center_expanded=False,
        point_type="1buy",
        point_anchor_at=anchor,
        point_available_at=available,
        inside_active_top_interval=True,
    )
    diagnostic.update(
        {
            "active_top_interval": [
                "2026-01-01T15:00:00+08:00",
                "2026-03-31T15:00:00+08:00",
            ],
            "mapping_unique": False,
            "mapping_candidate_ids": [],
            "mapping_supply": {
                "classification": "NO_LOWER_POINT_EVIDENCE",
                "lower_structure_available": True,
                "point_evidence_count": 0,
                "point_type_counts": {
                    "1sell": 0,
                    "2sell": 0,
                    "3sell": 0,
                    "3buy": 0,
                },
                "completed_sell12_count": 0,
                "in_top_interval_sell12_count": 0,
                "completed_in_top_interval_sell12_count": 0,
                "incomplete_in_top_interval_sell12_count": 0,
                "outside_top_interval_sell12_count": 0,
                "highest_candidate_center_count": 0,
                "point_evidence": [],
                "diagnostic_buy_point_type_counts": {
                    "1buy": 1,
                    "2buy": 0,
                },
                "diagnostic_directional_classification": (
                    "BUY12_PRESENT_SELL12_ABSENT"
                ),
                "diagnostic_buy_point_evidence": [point.document()],
            },
        }
    )
    report["higher_timeframe_effectiveness_audit"] = (
        higher_timeframe_effectiveness_audit(report["candidate_audit"])
    )
    report.pop("content_sha256", None)
    report["content_sha256"] = sha256_json(report)
    path = (
        root
        / "audit/chanlun_trading_system_backtest"
        / "recent_year_current_sector_no3p_mwd_strength"
        / "approximate_technical_backtest_sector_mwd_strength_tactical_lifecycle.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path, point_id


@pytest.fixture
def audit_root(tmp_path: Path) -> Path:
    path = (
        tmp_path
        / "audit/chanlun_trading_system_backtest/certified_report.json"
    )
    path.parent.mkdir(parents=True)
    report = _report()
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    _write_passed_gate(tmp_path, report)
    return tmp_path


@pytest.fixture
def app(audit_root: Path) -> Flask:
    repository_root = Path(__file__).resolve().parents[2]
    app = Flask(
        __name__,
        template_folder=str(repository_root / "web/chanlun_chart/cl_app/templates"),
        static_folder=str(repository_root / "web/chanlun_chart/cl_app/static"),
    )
    app.config.update(
        TESTING=True,
        SECRET_KEY="research-audit-test-secret",
        RESEARCH_AUDIT_ROOT=audit_root,
    )
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        return _User() if user_id == _User.id else None

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify(ok=False, code="authentication_required"), 401

    @app.get("/_test/login")
    def login():
        login_user(_User())
        return {"ok": True}

    app.register_blueprint(decision_support_bp)
    return app


def test_snapshot_exposes_only_new_read_only_strategy(audit_root: Path) -> None:
    snapshot = build_research_audit_snapshot(audit_root)

    assert snapshot["schema_version"] == "research-audit-page-v12"
    assert snapshot["strategy_id"] == "chanlun_source_faithful_v2"
    assert snapshot["active_strategy_count"] == 1
    assert snapshot["read_only"] is True
    assert snapshot["historical"] is True
    assert snapshot["no_order_execution"] is True
    assert snapshot["data_evidence"]["grade"] == "research_only"
    assert snapshot["aggregate_out_of_sample"]["annualized_return"] is None
    assert snapshot["verdict"]["live_ready"] is False
    assert snapshot["artifact"]["integrity_verified"] is True
    assert snapshot["evaluation_mode"] == "fixed_policy_one_year"
    assert snapshot["universe"]["selected_symbol_count"] == 5201
    assert snapshot["closed_trade_net_pnl"] == "0"
    assert snapshot["terminal_positions_marked_to_market"] is True


def test_current_result_is_preferred_and_exposes_lifecycle(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )

    snapshot = build_research_audit_snapshot(audit_root)

    assert snapshot["schema_version"] == "research-audit-page-v14"
    assert snapshot["source_kind"] == "current_research_variant"
    assert snapshot["strategy_id"] == "chanlun_v3_current_sector_human_assisted"
    assert snapshot["verdict"]["live_ready"] is False
    assert snapshot["data_evidence"]["grade"] == "research_only"
    current = snapshot["current_research"]
    assert current["daily_metrics"]["net_return"] == "0.02"
    assert current["lifecycle"]["intent_count"] == 2
    assert current["lifecycle"]["resolved_persistent_intent_count"] == 1
    assert current["lifecycle"]["suppressed_retry_count"] == 3
    scheduler = current["scheduler_causality_audit"]
    assert scheduler["converged"] is True
    assert scheduler["build_pass_count"] == 3
    assert scheduler["replay_pass_count"] == 2
    assert scheduler["initial_event_count"] == 5
    assert scheduler["final_event_count"] == 2
    assert scheduler["removed_stale_retry_event_count"] == 3
    assert scheduler["resolved_persistent_signal_count"] == 1
    assert scheduler["resolution_sessions"][0]["persistent_intent_id"] == (
        current["lifecycle"]["resolved_persistent_intent_ids"][0]
    )
    ablations = current["causal_ablations"]
    assert set(ablations) == {
        "NO_TACTICAL",
        "EXACT_GREEN_HIGHER_TIMEFRAME_ONLY",
    }
    assert ablations["NO_TACTICAL"]["metrics"]["tactical_cycle_count"] == 0
    assert ablations["NO_TACTICAL"]["scheduler_causality_audit"][
        "event_schedule_sha256"
    ] == "sha256:" + "b" * 64
    assert ablations["EXACT_GREEN_HIGHER_TIMEFRAME_ONLY"][
        "scheduler_causality_audit"
    ]["event_schedule_sha256"] == "sha256:" + "c" * 64
    assert current["tactical_execution_audit"]["generated_signal_count"] == 1
    terminal = current["terminal_accounting_attribution"]
    assert terminal["terminal"]["total_net_pnl"] == "20000"
    assert terminal["pnl_decomposition"][
        "closed_cycle_realized_net_pnl"
    ] == "-1000"
    assert terminal["pnl_decomposition"][
        "open_cycle_marked_net_pnl"
    ] == "21000"
    assert terminal["pnl_decomposition"]["pure_unrealized_net_pnl"] is None
    assert terminal["open_positions"][0]["symbol"] == "SZ.000001"
    assert terminal["sector_attribution"][0]["sector_name"] == "银行"
    risk = current["higher_timeframe_effectiveness_audit"]
    assert risk["status"] == "STRICT_GREEN_EMPTY_RESEARCH_AMBER_ONLY"
    assert risk["candidate_count"] == 17
    assert risk["strict_green_risk_eligible_count"] == 0
    assert risk["research_green_or_amber_risk_eligible_count"] == 12
    assert risk["research_amber_only_risk_eligible_count"] == 12
    assert risk["hard_rejected_candidate_count"] == 5
    assert risk["subjects"]["market"]["all_amber"] is True
    assert risk["subjects"]["symbol"]["gate_counts"] == {
        "AMBER": 12,
        "UNRESOLVED": 5,
    }
    execution = current["higher_timeframe_execution_attribution"]
    assert execution["status"] == (
        "STRICT_GREEN_EXECUTION_EMPTY_RESEARCH_AMBER_ONLY"
    )
    assert execution["causal_identity_status"] == "EXACT"
    assert execution["accepted_candidate_count"] == 12
    assert execution["entry_order_count"] == 12
    assert execution["entry_filled_candidate_count"] == 2
    assert execution["entry_unfilled_candidate_count"] == 10
    assert execution["all_filled_entries_are_research_amber_only"] is True
    assert execution["cohorts"]["STRICT_GREEN"][
        "entry_filled_candidate_count"
    ] == 0
    assert execution["cohorts"]["RESEARCH_AMBER_ONLY"][
        "closed_realized_net_pnl"
    ] == "-1000"
    assert execution["cohorts"]["RESEARCH_AMBER_ONLY"][
        "open_marked_net_pnl"
    ] == "21000"
    assert risk["subjects"]["sector"][
        "strict_same_base_warmup_reason_counts"
    ] == {"QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT": 17}
    assert risk["subjects"]["sector"][
        "strict_same_base_source_boundary_counts"
    ] == {"VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP": 17}
    assert risk["subjects"]["sector"][
        "strict_same_base_physical_source_boundary_counts"
    ] == {
        "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP": 17
    }
    assert risk["subjects"]["sector"][
        "strict_same_base_physical_representative_member_range"
    ] == {"minimum": 24, "maximum": 24}
    assert risk["subjects"]["sector"][
        "strict_same_base_physical_available_member_range"
    ] == {"minimum": 23, "maximum": 23}
    assert risk["subjects"]["sector"][
        "strict_same_base_physical_required_member_range"
    ] == {"minimum": 15, "maximum": 15}
    assert risk["subjects"]["sector"][
        "strict_same_base_physical_requested_start_range"
    ] == {
        "minimum": "2023-05-01T09:30:00+08:00",
        "maximum": "2023-05-01T09:30:00+08:00",
    }
    assert risk["subjects"]["sector"][
        "strict_same_base_physical_required_start_range"
    ] == {
        "minimum": "2025-04-30T10:50:00+08:00",
        "maximum": "2025-04-30T10:50:00+08:00",
    }
    assert risk["subjects"]["sector"][
        "strict_same_base_remaining_daily_bar_range"
    ] == {"minimum": 244, "maximum": 244}
    market_convergence = risk["subjects"]["market"]["warmup_convergence"]
    assert market_convergence["status_counts"] == {"NON_MONOTONIC": 17}
    assert market_convergence["qualified_prefix_count_range"] == {
        "minimum": 4,
        "maximum": 4,
    }
    assert market_convergence[
        "pairwise_stable_without_all_prefix_stability_count"
    ] == 17
    assert market_convergence["semantic_diagnostic_status_counts"] == {
        "NON_MONOTONIC": 17
    }
    assert market_convergence["non_monotonic_changed_path_counts"] == {
        "D.ma5": 17,
        "W.state": 17,
    }
    market_warmup_points = risk["subjects"]["market"][
        "warmup_non_monotonic_point_audit"
    ]
    assert market_warmup_points["point_count"] == 34
    assert market_warmup_points["chart_focus_supported_point_count"] == 34
    assert market_warmup_points["points"][0]["diagnostic_only"] is True
    sector_convergence = risk["subjects"]["sector"]["warmup_convergence"]
    assert sector_convergence["strict_same_base_status_counts"] == {
        "INSUFFICIENT_PREFIXES": 17
    }
    assert sector_convergence[
        "strict_same_base_semantic_diagnostic_status_counts"
    ] == {"INSUFFICIENT_PREFIXES": 17}
    assert risk["subjects"]["symbol"]["warmup_convergence"][
        "status_counts"
    ] == {"NON_MONOTONIC": 12, "NOT_RECORDED_LEGACY": 5}
    assert snapshot["artifact"]["decision_source_matches_current"] is True


def test_current_result_fails_closed_on_tampering(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["research_variant_result"]["daily_metrics"]["net_return"] = "9"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "current_research_artifact_hash_mismatch"


def test_current_result_rejects_rehashed_scheduler_audit_inconsistency(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["scheduler_causality_audit"]["final_event_count"] = 999
    payload.pop("content_sha256")
    payload["content_sha256"] = sha256_json(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "current_research_artifact_invalid"


def test_current_result_rejects_ablation_without_independent_scheduler_audit(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["ablation_scheduler_causality_audits"].pop("NO_TACTICAL")
    payload.pop("content_sha256")
    payload["content_sha256"] = sha256_json(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "current_research_artifact_invalid"


def test_current_result_fails_closed_when_decision_source_is_stale(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: False,
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "current_research_decision_source_stale"


def test_v21_current_result_requires_verified_release_input_binding(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_release_bound_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    monkeypatch.setattr(
        "cl_app.services.research_audit.verify_sector_release_manifest",
        lambda **kwargs: {
            "all_bound_files_verified": True,
            "direct_checkpoint_count": 28,
            "checkpoint_payloads_unpickled": False,
            "live_status": "LIVE_DISABLED",
        },
    )

    snapshot = build_research_audit_snapshot(audit_root)

    assert snapshot["artifact"]["release_manifest_verified"] is True
    assert snapshot["artifact"]["release_manifest"][
        "direct_checkpoint_count"
    ] == 28


def test_v21_current_result_fails_closed_on_unbound_release_inputs(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_release_bound_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )

    def reject(**kwargs: object) -> dict[str, object]:
        raise SectorReleaseManifestError("checkpoint hash changed")

    monkeypatch.setattr(
        "cl_app.services.research_audit.verify_sector_release_manifest",
        reject,
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "current_research_release_manifest_invalid"


def test_current_result_rejects_rehashed_terminal_accounting_tampering(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["research_variant_result"]["terminal_accounting_attribution"][
        "pnl_decomposition"
    ]["identity_difference"] = "1"
    payload.pop("content_sha256")
    payload["content_sha256"] = sha256_json(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "current_research_artifact_invalid"


def test_current_result_rejects_rehashed_risk_effectiveness_tampering(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["higher_timeframe_effectiveness_audit"][
        "strict_green_risk_eligible_count"
    ] = 12
    payload.pop("content_sha256")
    payload["content_sha256"] = sha256_json(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "current_research_artifact_invalid"


def test_current_result_rejects_rehashed_risk_execution_tampering(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    execution = payload["higher_timeframe_execution_attribution"]
    execution["entry_order_count"] = 11
    stable_execution = dict(execution)
    stable_execution.pop("audit_sha256")
    execution["audit_sha256"] = sha256_json(stable_execution)
    payload.pop("content_sha256")
    payload["content_sha256"] = sha256_json(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "current_research_artifact_invalid"


def test_snapshot_rejects_content_tampering(audit_root: Path) -> None:
    path = (
        audit_root
        / "audit/chanlun_trading_system_backtest/certified_report.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["aggregate_out_of_sample"]["net_return"] = "99"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ResearchAuditUnavailable, match="artifact_hash_mismatch"):
        build_research_audit_snapshot(audit_root)


def test_causality_gate_blocks_an_older_research_report(
    audit_root: Path,
) -> None:
    gate = (
        audit_root
        / "audit/chanlun_trading_system_backtest/causality_gate.json"
    )
    gate.write_text(
        json.dumps(
            {
                "schema": "chanlun-backtest-causality-gate/v2",
                "checked_at": "2026-07-25T12:00:00+08:00",
                "status": "blocked",
                "pnl_generated": False,
                "algorithm_revision": "sha256:" + "a" * 64,
                "pit_snapshot_sha256": "sha256:" + "1" * 64,
                "validated_symbol_fact_count": 1,
                "validated_decision_count": 0,
                "proven_controls": _CAUSAL_CONTROLS,
                "failures": ["survivorship_free_universe_unverified"],
                "report": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "causality_gate_blocked"
    assert raised.value.details is not None
    assert raised.value.details["pnl_generated"] is False
    assert raised.value.details["failures"][0]["code"] == (
        "survivorship_free_universe_unverified"
    )


def test_page_explains_that_blocked_gate_generated_no_pnl(
    app: Flask,
    audit_root: Path,
) -> None:
    gate = (
        audit_root
        / "audit/chanlun_trading_system_backtest/causality_gate.json"
    )
    gate.write_text(
        json.dumps(
            {
                "schema": "chanlun-backtest-causality-gate/v2",
                "checked_at": "2026-07-25T12:00:00+08:00",
                "status": "blocked",
                "pnl_generated": False,
                "algorithm_revision": "sha256:" + "b" * 64,
                "pit_snapshot_sha256": "sha256:" + "1" * 64,
                "validated_symbol_fact_count": 1,
                "validated_decision_count": 0,
                "proven_controls": _CAUSAL_CONTROLS,
                "failures": ["historical_sector_membership_unverified"],
                "report": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")
    html = response.get_data(as_text=True)

    assert response.status_code == 503
    assert "未生成正式回测收益" in html
    assert "系统已在计算收益前停止" in html
    assert "historical_sector_membership_unverified" in html
    assert "chanlun_source_faithful_v2" not in html


def test_snapshot_reads_only_the_canonical_current_report(
    audit_root: Path,
) -> None:
    directory = audit_root / "audit/chanlun_trading_system_backtest"
    legacy = directory / "newer_legacy_strategy.json"
    legacy.write_text(
        json.dumps(
            {
                "strategy_id": "obsolete_strategy_v1",
                "generated_at": "2099-01-01T00:00:00+08:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_research_audit_snapshot(audit_root)

    assert snapshot["strategy_id"] == "chanlun_source_faithful_v2"
    assert snapshot["artifact"]["relative_path"].endswith(
        "/certified_report.json"
    )


def test_snapshot_accepts_locked_non_first_center_selection(tmp_path: Path) -> None:
    path = tmp_path / "audit/chanlun_trading_system_backtest/certified_report.json"
    path.parent.mkdir(parents=True)
    report = _report(False)
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    _write_passed_gate(tmp_path, report)

    snapshot = build_research_audit_snapshot(tmp_path)

    contract = snapshot["execution_contract"]
    assert contract["first_center_three_buy_only"] is False
    assert contract["first_center_three_buy_mode"] == "walk_forward_selected"
    assert contract["first_center_three_buy_selected_values"] == [False]


def test_page_and_data_endpoint_are_login_protected_and_read_only(app: Flask) -> None:
    client = app.test_client()
    assert client.get("/decision-support/research-audit").status_code == 401
    assert client.get("/decision-support/research-audit/data").status_code == 401
    assert client.get("/_test/login").status_code == 200

    page = client.get("/decision-support/research-audit")
    data = client.get("/decision-support/research-audit/data")
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "private, no-store"
    assert data.status_code == 200
    assert data.get_json()["data"]["active_strategy_count"] == 1
    assert "历史研究 / 审计成果" in html
    assert "chanlun_source_faithful_v2" in html
    assert "旧策略报告不会加载" in html
    assert "未达到实盘标准" in html
    assert "30m 大级别结构" in html
    assert "5m 可操作级别" in html
    assert "1m 精细触发" in html
    assert "一、二、三类买卖点独立" in html
    assert "第一中枢三买" in html
    assert "历史时点申万一级有效成分" in html
    assert "近一年固定策略回测范围" in html
    assert "5201 / 5227 个历史标的纳入" in html
    assert "总收益包含期末未平仓持仓的盯市损益" in html
    assert "样本门槛" in html
    assert "年化收益" not in html
    assert "旧双轨" not in html
    assert "旧成果" not in html
    assert "sector_first_" + "early_screening" not in html
    assert "<form" not in html.lower()
    assert 'method="post"' not in html.lower()


def test_current_page_and_api_share_the_lifecycle_result(
    app: Flask,
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    page = client.get("/decision-support/research-audit")
    api = client.get("/decision-support/research-audit/data")
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert api.status_code == 200
    assert 'data-source-kind="current_research_variant"' in html
    assert "当前缠论研究回放" in html
    assert "持久信号与幂等抑制" in html
    assert "历史调度与逐日前向检查点一致" in html
    assert "5 → 2" in html
    assert "移除跨日陈旧重试 3" in html
    assert "5m 短差信号为何没有形成合法短差成交" in html
    assert "从源信号独立重排的因果消融" in html
    assert "禁用短差" in html
    assert "仅严格三门全 GREEN" in html
    assert "不是从主回放批次中删事件" in html
    assert "总收益由已平仓周期与期末开放周期共同构成" in html
    assert "纯浮盈未声明" in html
    assert "正收益尚未由已平仓周期验证" in html
    assert "高级别风险门在候选时点实际做了什么" in html
    assert "多前缀收敛诊断（不改变现有双窗口交易门）" in html
    assert "NON_MONOTONIC" in html
    assert "合格前缀 4–4 个" in html
    assert "双窗口稳定但未获全部前缀稳定证明 17 个候选" in html
    assert "严格5m同源多前缀诊断" in html
    assert "M/W/D 语义差异证据" in html
    assert "暖机非单调差异（34）" in html
    assert "D.ma5" in html
    assert "W.state" in html
    assert "MA5 11" in html
    assert "交易门不变" in html
    assert "INSUFFICIENT_PREFIXES" in html
    assert "原生日线交易日覆盖" in html
    assert "结构化证据 0 个候选" in html
    assert "缺失未证明为停牌" not in html
    assert "候选时点的顶分型映射暴露" in html
    assert "去重后的活动顶分型事件" in html
    assert "稳定买卖点身份" in html
    assert "跨顶分型事件及对象去重" in html
    assert "当前工件只有历史计数或没有点位" in html
    assert "只解释冻结的一/二卖映射" in html
    assert "诊断性一/二买" in html
    assert "仅解释方向供给，不参与风险门、卖点映射或订单" in html
    assert "稳定诊断买点身份" in html
    assert "MAPPING_ELIGIBLE=false" in html
    assert "NOT_RECORDED_LEGACY" in html
    assert (
        "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP" in html
    )
    assert "2023-05-01T09:30:00+08:00" in html
    assert "480" in html
    assert "当前成交样本全部不是严格高级别门样本" in html
    assert "STRICT_GREEN_EMPTY_RESEARCH_AMBER_ONLY" in html
    assert "银行" in html
    assert "+21000.00" in html
    assert "NO_EXECUTABLE_TACTICAL_LOT" in html
    assert "风险候选 → 入场单" in html
    assert "候选—订单—成交—盈亏链已经证明" in html
    assert "全部入场成交均来自 AMBER 研究放宽" in html
    assert "高级别风险候选到入场订单、首次成交和终端战略周期" in html
    assert "STRICT_GREEN_EXECUTION_EMPTY_RESEARCH_AMBER_ONLY" in html
    assert "年化估算" in html
    assert "不足完整自然年，仅数学外推" in html
    assert "年化值不是完整自然年实测" in html
    assert "事件账本因此保持年化收益为 N/A" in html
    assert "RESEARCH_ONLY · LIVE_DISABLED" in html
    assert "<form" not in html.lower()
    data = api.get_json()["data"]
    assert data["source_kind"] == "current_research_variant"
    assert data["current_research"]["lifecycle"]["suppressed_retry_count"] == 3
    assert data["current_research"]["scheduler_causality_audit"][
        "forward_checkpoint_equivalent"
    ] is True
    assert data["current_research"]["causal_ablations"]["NO_TACTICAL"][
        "scheduler_causality_audit"
    ]["converged"] is True
    assert data["current_research"]["terminal_accounting_attribution"][
        "pnl_decomposition"
    ]["open_cycle_marked_net_pnl"] == "21000"
    assert data["current_research"]["replay_metrics"][
        "annualized_return"
    ] is None
    assert (
        "INSUFFICIENT_CALENDAR_SPAN_FOR_ANNUALIZATION"
        in data["current_research"]["replay_metrics"]["warnings"]
    )
    execution = data["current_research"][
        "higher_timeframe_execution_attribution"
    ]
    assert execution["causal_identity_status"] == "EXACT"
    assert execution["entry_order_count"] == 12
    assert execution["entry_filled_candidate_count"] == 2
    assert execution["all_filled_entries_are_research_amber_only"] is True


def test_identified_risk_point_builds_verified_causal_chart_lock(
    app: Flask,
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, point_id = _write_current_report_with_identified_risk_point(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    snapshot = build_research_audit_snapshot(audit_root)
    risk = snapshot["current_research"]["higher_timeframe_effectiveness_audit"]
    point = risk["subjects"]["market"]["globally_deduplicated_point_audit"][
        "points"
    ][0]

    lock = validate_risk_point_chart_lock(
        audit_root,
        point_id=point_id,
        source_sha256=risk["audit_sha256"],
        review_as_of=point["review_as_of_unix"],
    )
    assert lock["lock_kind"] == "RISK_POINT_AUDIT"
    assert lock["symbol"] == "SH.000001"
    assert lock["chart_interval"] == "1W"
    assert lock["focus_at"] == point["point_anchor_unix"]
    assert lock["review_as_of"] == point["review_as_of_unix"]
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200
    html = client.get("/decision-support/research-audit").get_data(as_text=True)
    assert "因果锁定并定位" in html
    assert f"review_candidate_id=sha256%3A{point_id[7:]}" in html
    assert "review_source_sha256=" in html
    assert f"review_as_of={point['review_as_of_unix']}" in html

    with pytest.raises(ValueError, match="stale audit"):
        validate_risk_point_chart_lock(
            audit_root,
            point_id=point_id,
            source_sha256="sha256:" + "0" * 64,
            review_as_of=point["review_as_of_unix"],
        )
    with pytest.raises(ValueError, match="not present"):
        validate_risk_point_chart_lock(
            audit_root,
            point_id=point_id,
            source_sha256=risk["audit_sha256"],
            review_as_of=point["review_as_of_unix"] + 1,
        )


def test_identified_diagnostic_buy_builds_separate_causal_chart_lock(
    app: Flask,
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, point_id = _write_current_report_with_identified_diagnostic_buy_point(
        audit_root
    )
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    snapshot = build_research_audit_snapshot(audit_root)
    risk = snapshot["current_research"]["higher_timeframe_effectiveness_audit"]
    market = risk["subjects"]["market"]
    point = market[
        "globally_deduplicated_diagnostic_buy_point_audit"
    ]["points"][0]

    lock = validate_risk_point_chart_lock(
        audit_root,
        point_id=point_id,
        source_sha256=risk["audit_sha256"],
        review_as_of=point["review_as_of_unix"],
    )
    assert lock["lock_kind"] == "RISK_POINT_AUDIT"
    assert lock["evidence_role"] == "DIAGNOSTIC_BUY_ONLY"
    assert lock["symbol"] == "SH.000001"
    assert lock["point_type"] == "1buy"
    assert point["diagnostic_only"] is True
    assert point["mapping_eligible"] is False
    assert market["globally_deduplicated_point_audit"][
        "distinct_point_id_count"
    ] == 0

    client = app.test_client()
    assert client.get("/_test/login").status_code == 200
    html = client.get("/decision-support/research-audit").get_data(as_text=True)
    assert "诊断一/二买明细（1）" in html
    assert f"review_candidate_id=sha256%3A{point_id[7:]}" in html


def test_non_monotonic_warmup_difference_builds_diagnostic_chart_lock(
    app: Flask,
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    snapshot = build_research_audit_snapshot(audit_root)
    risk = snapshot["current_research"]["higher_timeframe_effectiveness_audit"]
    points = risk["subjects"]["market"][
        "warmup_non_monotonic_point_audit"
    ]["points"]
    point = next(
        value
        for value in points
        if value["changed_paths"] == ["D.ma5"]
    )

    lock = validate_risk_point_chart_lock(
        audit_root,
        point_id=point["point_id"],
        source_sha256=risk["audit_sha256"],
        review_as_of=point["review_as_of_unix"],
    )

    assert lock["lock_kind"] == "RISK_POINT_AUDIT"
    assert lock["evidence_role"] == "WARMUP_NON_MONOTONIC_DIAGNOSTIC"
    assert lock["symbol"] == "SH.000001"
    assert lock["point_type"] == "WARMUP_DIFF"
    assert lock["chart_interval"] == "1D"
    assert lock["focus_at"] <= lock["review_as_of"]
    assert point["prefix_ma5"] == "11"
    assert point["reference_ma5"] == "10"

    client = app.test_client()
    assert client.get("/_test/login").status_code == 200
    html = client.get("/decision-support/research-audit").get_data(as_text=True)
    assert "暖机语义差异只解释" in html
    assert f"review_candidate_id=sha256%3A{point['point_id'][7:]}" in html


def test_warmup_mapping_supply_delta_is_visible_and_causally_chart_locked(
    app: Flask,
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_current_report_with_warmup_mapping_supply_delta(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    snapshot = build_research_audit_snapshot(audit_root)
    risk = snapshot["current_research"]["higher_timeframe_effectiveness_audit"]
    convergence = risk["subjects"]["market"]["warmup_convergence"]
    assert convergence["mapping_supply_diagnostic_status_counts"] == {
        "NON_MONOTONIC": 1,
        "NOT_RECORDED_LEGACY": 16,
    }
    for code in (
        "SUPPLY_CLASSIFICATION_CHANGED",
        "SELL12_DISAPPEARED_WITH_LONGER_HISTORY",
        "COMPLETED_IN_INTERVAL_SELL12_DISAPPEARED_WITH_LONGER_HISTORY",
        "HIGHEST_CANDIDATE_DISAPPEARED_WITH_LONGER_HISTORY",
        "POINT_EVIDENCE_LOST_WITH_LONGER_HISTORY",
        "POINT_EVIDENCE_GAINED_WITH_LONGER_HISTORY",
        "POINT_IDENTITY_SET_RESEGMENTED",
    ):
        assert convergence["mapping_supply_transition_code_counts"][code] == 1
    assert convergence["structure_lineage_diagnostic_status_counts"] == {
        "NON_MONOTONIC": 1,
        "NOT_RECORDED_LEGACY": 16,
    }
    for code in (
        "LOWER_LINE_COMMON_SUFFIX_IDENTICAL",
        "SHORTER_LINE_SEQUENCE_IS_REFERENCE_SUFFIX",
        "CENTER_PARTITION_CHANGED_WITH_IDENTICAL_COMMON_LINES",
        "CENTER_CORE_RETAINED_WITH_ONE_LINE_PHASE_SHIFT",
        "LOST_SELL_TRIGGER_LINE_ABSORBED_INTO_REFERENCE_CENTER",
        "POINT_TRIGGER_ROLE_CHANGED_WITH_LONGER_HISTORY",
    ):
        assert convergence["structure_lineage_transition_code_counts"][code] == 1

    point_audit = risk["subjects"]["market"][
        "warmup_mapping_supply_point_audit"
    ]
    assert point_audit["point_count"] == 2
    assert point_audit["distinct_structural_point_id_count"] == 2
    assert point_audit["delta_direction_counts"] == {
        "GAINED_IN_LONGEST": 1,
        "LOST_FROM_LONGEST": 1,
    }
    lost = next(
        value
        for value in point_audit["points"]
        if value["delta_direction"] == "LOST_FROM_LONGEST"
    )
    assert lost["point_type"] == "1sell"
    assert lost["highest_mapping_candidate"] is True
    assert lost["chart_focus_supported"] is True

    lineage_audit = risk["subjects"]["market"][
        "warmup_structure_lineage_point_audit"
    ]
    assert lineage_audit["point_count"] == 1
    assert lineage_audit["same_core_interval_count"] == 1
    assert lineage_audit["one_line_phase_shift_count"] == 1
    assert lineage_audit["sell_trigger_absorbed_count"] == 1
    lineage_point = lineage_audit["points"][0]
    assert lineage_point["point_id"] == lost["point_id"]
    assert lineage_point["structural_point_id"] == lost["structural_point_id"]
    assert lineage_point["point_type"] == "1sell"
    assert lineage_point["prefix_trigger_role"] == "AFTER_CENTER"
    assert lineage_point["reference_trigger_role"] == "CENTER_CONSTITUENT"
    assert lineage_point["line_sequences"][0]["common_suffix_line_count"] == 9
    assert lineage_point["same_core_interval"] is True
    assert lineage_point["one_line_phase_shift"] is True
    assert lineage_point["chart_focus_supported"] is True

    lock = validate_risk_point_chart_lock(
        audit_root,
        point_id=lost["point_id"],
        source_sha256=risk["audit_sha256"],
        review_as_of=lost["review_as_of_unix"],
    )
    assert lock["evidence_role"] == "WARMUP_MAPPING_SUPPLY_DELTA"
    assert lock["symbol"] == "SH.000001"
    assert lock["point_type"] == "1sell"
    assert lock["chart_interval"] == "1D"
    assert lock["focus_at"] <= int(
        datetime.fromisoformat(lock["point_available_at"]).timestamp()
    )
    assert datetime.fromisoformat(lock["point_available_at"]).timestamp() <= (
        lock["review_as_of"]
    )

    client = app.test_client()
    assert client.get("/_test/login").status_code == 200
    html = client.get("/decision-support/research-audit").get_data(as_text=True)
    assert "暖机映射供给逐点变化（2；稳定点 2）" in html
    assert "SELL12_DISAPPEARED_WITH_LONGER_HISTORY" in html
    assert "LOST_FROM_LONGEST" in html
    assert "1sell" in html
    assert "市场暖机结构谱系（1；同核心 1；一笔相位偏移 1；卖点触发线被吸收 1）" in html
    assert "AFTER_CENTER" in html
    assert "CENTER_CONSTITUENT" in html
    assert "LOST_SELL_TRIGGER_LINE_ABSORBED_INTO_REFERENCE_CENTER" in html
    assert f"review_candidate_id=sha256%3A{lost['point_id'][7:]}" in html


def test_current_result_rejects_rehashed_warmup_mapping_supply_delta(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_current_report_with_warmup_mapping_supply_delta(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    supply = payload["candidate_audit"][0][
        "market_risk_warmup_evidence"
    ]["warmup_mapping_supply_diagnostic"]
    supply["comparisons"][0]["delta"]["transition_codes"] = [
        "MAPPING_SUPPLY_UNCHANGED"
    ]
    stable_supply = dict(supply)
    stable_supply.pop("content_sha256")
    supply["content_sha256"] = sha256_json(stable_supply)
    payload.pop("content_sha256")
    payload["content_sha256"] = sha256_json(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable):
        build_research_audit_snapshot(audit_root)


def test_current_result_rejects_rehashed_warmup_structure_lineage_delta(
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_current_report_with_warmup_mapping_supply_delta(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    lineage = payload["candidate_audit"][0][
        "market_risk_warmup_evidence"
    ]["warmup_structure_lineage_diagnostic"]
    lineage["comparisons"][0]["delta"]["transition_codes"] = [
        "LOWER_LINE_COMMON_SUFFIX_IDENTICAL"
    ]
    stable_lineage = dict(lineage)
    stable_lineage.pop("content_sha256")
    lineage["content_sha256"] = sha256_json(stable_lineage)
    payload.pop("content_sha256")
    payload["content_sha256"] = sha256_json(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable):
        build_research_audit_snapshot(audit_root)


def test_current_page_shows_stale_source_error_without_old_report_fallback(
    app: Flask,
    audit_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_current_report(audit_root)
    monkeypatch.setattr(
        "cl_app.services.research_audit.decision_source_snapshot_matches_current",
        lambda value, root: False,
    )
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")
    html = response.get_data(as_text=True)

    assert response.status_code == 503
    assert "current_research_decision_source_stale" in html
    assert "当前回放结果不能安全展示" in html
    assert "0.02" not in html


def test_audit_page_leads_with_evidence_and_out_of_sample_verdict(
    app: Flask,
) -> None:
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    headings = (
        "是否达到实盘标准",
        "数据证据等级",
        "样本充分性",
        "固定策略收益、回撤与风险",
    )
    for heading in headings:
        assert heading in html
    assert [html.index(heading) for heading in headings] == sorted(
        html.index(heading) for heading in headings
    )
    for section in (
        "一、二、三类买点独立归因",
        "固定策略，不做历史调参",
        "过滤器消融与样本代价",
        "参数稳健性",
        "基线与市场参照",
        "集中度与限制",
        "算法源文件哈希",
    ):
        assert section in html
    assert "证据不足，结果仅可用于研究" in html
    assert 'data-live-ready="false"' in html


def test_page_fails_closed_when_new_artifact_is_missing(
    app: Flask,
    audit_root: Path,
) -> None:
    directory = audit_root / "audit/chanlun_trading_system_backtest"
    path = directory / "certified_report.json"
    path.unlink()
    (directory / "legacy_report.json").write_text(
        json.dumps(_report(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")

    assert response.status_code == 503
    assert "artifact_unavailable" in response.get_data(as_text=True)


def test_research_audit_styles_keep_dense_content_readable(app: Flask) -> None:
    client = app.test_client()

    response = client.get("/static/css/research_audit.css")
    css = response.get_data(as_text=True).replace("\r\n", "\n")

    assert response.status_code == 200
    assert "font-size: 16px;" in css
    assert ".ra-metric-card small {\n  font-size: 13px;" in css
    assert "th {\n  color:" in css
    assert "font-size: 13px;" in css
