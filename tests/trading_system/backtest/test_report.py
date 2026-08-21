from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast

from chanlun.decision_support.trading_system.backtest.data_audit import DataEvidence
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    BacktestTrade,
    EquityPoint,
)
from chanlun.decision_support.trading_system.backtest.report import (
    REQUIRED_ABLATION_IDS,
    REQUIRED_BENCHMARK_IDS,
    AblationResult,
    BacktestEvaluationResult,
    BenchmarkResult,
    WalkForwardWindowResult,
    build_report,
    verify_report_hash,
)
from chanlun.decision_support.trading_system.backtest.metrics import calculate_metrics
from chanlun.decision_support.trading_system.models import PointType
from tests.trading_system.backtest.helpers import CN


GENERATED_AT = datetime(2026, 7, 20, 18, 0, tzinfo=CN)


def evidence(grade: str = "certified") -> DataEvidence:
    failures = () if grade == "certified" else ("historical_membership_missing",)
    return DataEvidence(
        grade=grade,  # type: ignore[arg-type]
        failures=failures,
        warnings=(),
        coverage=(("bar_status_coverage", Decimal("1")),),
    )


def trade(index: int, *, concentrated: bool = False) -> BacktestTrade:
    point_type = "1buy" if index < 50 else "2buy" if index < 100 else "3buy"
    code_index = 0 if concentrated else index % 10
    sector_index = 0 if concentrated else index % 10
    entered = GENERATED_AT - timedelta(days=365 - index)
    return BacktestTrade(
        code=f"SZ.{code_index:06d}",
        sector_id=f"qmt-gics3:test-sector-{sector_index}",
        point_type=cast(PointType, point_type),
        entry_at=entered,
        exit_at=entered + timedelta(hours=1),
        shares=100,
        entry_price=Decimal("10"),
        exit_price=Decimal("10.10"),
        exit_trigger_price=Decimal("10.05"),
        exit_reason="signal_exit_full",
        net_pnl=Decimal("8"),
        net_return=Decimal("0.008"),
        total_cost=Decimal("2"),
    )


def run_with_trades(count: int, *, concentrated: bool = False) -> BacktestRun:
    start = GENERATED_AT - timedelta(days=365)
    points = (
        EquityPoint(start, Decimal("100"), Decimal("0"), Decimal("100"), Decimal("0")),
        EquityPoint(
            start + timedelta(days=180),
            Decimal("120"),
            Decimal("0"),
            Decimal("120"),
            Decimal("0"),
        ),
        EquityPoint(
            start + timedelta(days=270),
            Decimal("114"),
            Decimal("0"),
            Decimal("114"),
            Decimal("0"),
        ),
        EquityPoint(
            GENERATED_AT,
            Decimal("130"),
            Decimal("0"),
            Decimal("130"),
            Decimal("0"),
        ),
    )
    return BacktestRun(
        fills=(),
        trades=tuple(trade(index, concentrated=concentrated) for index in range(count)),
        equity_curve=points,
        open_positions=(),
        pending_exits=(),
    )


def ablations() -> tuple[AblationResult, ...]:
    return tuple(
        AblationResult(
            ablation_id=ablation_id,
            label=ablation_id,
            trade_count=200 - index * 5,
            sample_reduction=Decimal(index) / Decimal("100"),
            net_return=Decimal("0.20") + Decimal(index) / Decimal("100"),
            max_drawdown=Decimal("0.08") - Decimal(index) / Decimal("1000"),
            calmar=Decimal("1.5") + Decimal(index) / Decimal("10"),
            quality_change=Decimal(index) / Decimal("100"),
        )
        for index, ablation_id in enumerate(REQUIRED_ABLATION_IDS)
    )


def benchmarks() -> tuple[BenchmarkResult, ...]:
    return tuple(
        BenchmarkResult(
            benchmark_id=benchmark_id,
            label=benchmark_id,
            net_return=Decimal("0.10"),
            max_drawdown=Decimal("0.15"),
            data_grade="certified",
        )
        for benchmark_id in REQUIRED_BENCHMARK_IDS
    )


def result(count: int, *, concentrated: bool = False) -> BacktestEvaluationResult:
    return BacktestEvaluationResult(
        aggregate_run=run_with_trades(count, concentrated=concentrated),
        walk_forward_windows=(),
        parameter_robustness=(("stable_neighbor_fraction", Decimal("0.75")),),
        liquidity_metrics=(("filled_order_ratio", Decimal("0.80")),),
        bootstrap_repetitions=40,
        bootstrap_seed=20260720,
    )


def result_with_window(count: int) -> BacktestEvaluationResult:
    base = result(count)
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
            ("max_portfolio_heat", "0.015"),
            ("first_buy_risk_multiplier", "0.25"),
        ),
        test_metrics=calculate_metrics(base.aggregate_run),
        closed_trade_count=count,
    )
    return replace(base, walk_forward_windows=(window,))


def test_report_cannot_claim_pass_with_one_trade_or_research_data() -> None:
    report = build_report(
        evidence=evidence("research_only"),
        result=result(1),
        ablations=(),
        benchmarks=(),
        generated_at=GENERATED_AT,
    )

    assert report["verdict"]["live_ready"] is False
    assert report["verdict"]["status"] == "evidence_insufficient"
    assert report["sample_adequacy"]["passed"] is False


def test_all_acceptance_conditions_can_pass_only_with_complete_analysis() -> None:
    report = build_report(
        evidence=evidence(),
        result=result_with_window(200),
        ablations=ablations(),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
    )

    assert report["verdict"] == {
        "live_ready": True,
        "status": "live_ready",
        "failed_conditions": [],
    }
    assert report["aggregate_out_of_sample"]["net_return"] == "0.3"
    assert report["aggregate_out_of_sample"]["max_drawdown"] == "0.05"
    assert Decimal(report["aggregate_out_of_sample"]["calmar"]) >= 1


def test_aggregate_without_walk_forward_windows_cannot_be_live_ready() -> None:
    report = build_report(
        evidence=evidence(),
        result=result(200),
        ablations=ablations(),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
    )

    assert report["verdict"]["live_ready"] is False
    assert report["verdict"]["status"] == "methodology_incomplete"
    assert "walk_forward_evidence" in report["verdict"]["failed_conditions"]


def test_report_hash_is_canonical_and_detects_mutation() -> None:
    first = build_report(
        evidence=evidence(),
        result=result_with_window(200),
        ablations=ablations(),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
        algorithm_hashes=(("engine", "sha256:" + "1" * 64),),
    )
    second = build_report(
        evidence=evidence(),
        result=result_with_window(200),
        ablations=tuple(reversed(ablations())),
        benchmarks=tuple(reversed(benchmarks())),
        generated_at=GENERATED_AT,
        algorithm_hashes=(("engine", "sha256:" + "1" * 64),),
    )

    assert first["content_sha256"] == second["content_sha256"]
    assert verify_report_hash(first) is True
    first["verdict"]["live_ready"] = False
    assert verify_report_hash(first) is False


def test_required_ablation_and_benchmark_fields_are_present() -> None:
    report = build_report(
        evidence=evidence(),
        result=result(200),
        ablations=ablations(),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
    )

    assert tuple(row["ablation_id"] for row in report["ablations"]) == (
        REQUIRED_ABLATION_IDS
    )
    assert all("quality_change" in row and "sample_reduction" in row for row in report["ablations"])
    assert tuple(row["benchmark_id"] for row in report["benchmarks"]) == (
        REQUIRED_BENCHMARK_IDS
    )


def test_concentration_above_twenty_percent_blocks_acceptance() -> None:
    report = build_report(
        evidence=evidence(),
        result=result(200, concentrated=True),
        ablations=ablations(),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
    )

    assert report["concentration"]["passed"] is False
    assert report["verdict"]["live_ready"] is False
    assert "concentration" in report["verdict"]["failed_conditions"]


def test_short_span_does_not_publish_annualized_headlines() -> None:
    short_run = run_with_trades(1)
    first = short_run.equity_curve[0]
    short_run = BacktestRun(
        fills=(),
        trades=short_run.trades,
        equity_curve=(
            first,
            EquityPoint(
                first.closed_at + timedelta(days=30),
                Decimal("101"),
                Decimal("0"),
                Decimal("101"),
                Decimal("0"),
            ),
        ),
        open_positions=(),
        pending_exits=(),
    )
    short_result = BacktestEvaluationResult(
        aggregate_run=short_run,
        bootstrap_repetitions=20,
    )

    report = build_report(
        evidence=evidence(),
        result=short_result,
        ablations=(),
        benchmarks=(),
        generated_at=GENERATED_AT,
    )

    assert report["aggregate_out_of_sample"]["annualized_return"] is None
    assert report["aggregate_out_of_sample"]["sharpe"] is None


def test_invalid_market_evidence_is_reported_as_data_invalid() -> None:
    report = build_report(
        evidence=evidence("invalid"),
        result=result(1),
        ablations=(),
        benchmarks=(),
        generated_at=GENERATED_AT,
    )

    assert report["verdict"]["live_ready"] is False
    assert report["verdict"]["status"] == "data_invalid"


def test_invalid_required_benchmark_blocks_analysis_acceptance() -> None:
    rows = list(benchmarks())
    rows[1] = BenchmarkResult(
        benchmark_id=rows[1].benchmark_id,
        label=rows[1].label,
        net_return=None,
        max_drawdown=None,
        data_grade="invalid",
    )

    report = build_report(
        evidence=evidence(),
        result=result(200),
        ablations=ablations(),
        benchmarks=tuple(rows),
        generated_at=GENERATED_AT,
    )

    assert report["verdict"]["live_ready"] is False
    assert "benchmark_evidence" in report["verdict"]["failed_conditions"]


def test_unavailable_required_ablation_blocks_analysis_acceptance() -> None:
    rows = list(ablations())
    rows[0] = AblationResult(
        ablation_id=rows[0].ablation_id,
        label=rows[0].label,
        trade_count=0,
        sample_reduction=Decimal("0"),
        net_return=Decimal("0"),
        max_drawdown=Decimal("0"),
        calmar=None,
        quality_change=Decimal("0"),
        completed=False,
        data_grade="invalid",
        failure_codes=("walk_forward_unavailable",),
    )

    report = build_report(
        evidence=evidence(),
        result=result(200),
        ablations=tuple(rows),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
    )

    assert report["verdict"]["live_ready"] is False
    assert "ablation_evidence" in report["verdict"]["failed_conditions"]
    assert report["ablations"][0]["completed"] is False


def test_report_records_requested_range_and_data_source_hashes() -> None:
    report = build_report(
        evidence=evidence(),
        result=result(200),
        ablations=ablations(),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
        requested_range=(date(2020, 1, 1), date(2026, 7, 17)),
        data_source_hashes=(("bars", "sha256:" + "2" * 64),),
    )

    assert report["requested_range"] == {
        "start": "2020-01-01",
        "end": "2026-07-17",
    }
    assert report["data_source_hashes"] == {
        "bars": "sha256:" + "2" * 64,
    }
    assert verify_report_hash(report) is True


def test_independent_walk_forward_window_liquidation_blocks_live_ready() -> None:
    report = build_report(
        evidence=evidence(),
        result=result(200),
        ablations=ablations(),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
        limitations=("walk_forward_windows_evaluated_independently",),
    )

    assert report["verdict"]["live_ready"] is False
    assert report["verdict"]["status"] == "methodology_incomplete"
    assert "execution_continuity" in report["verdict"]["failed_conditions"]


def test_execution_contract_discloses_unified_buy_point_execution() -> None:
    report = build_report(
        evidence=evidence(),
        result=result_with_window(200),
        ablations=ablations(),
        benchmarks=benchmarks(),
        generated_at=GENERATED_AT,
    )

    contract = report["execution_contract"]
    assert contract["point_classes_analyzed_independently"] is True
    assert contract["buy_point_classes_share_execution_logic"] is True
    assert contract["trade_frequency"] == "5m"
    assert contract["segment_difference_frequency"] == "1m"
    assert contract["segment_difference_required_for_trade_signal"] is False
    assert contract["execution_observation_frequency"] == "1m"
    assert "trigger_frequency" not in contract
    assert contract["max_five_minute_setup_age_seconds"] == 345600
    assert contract["formal_selection_required"] is False
