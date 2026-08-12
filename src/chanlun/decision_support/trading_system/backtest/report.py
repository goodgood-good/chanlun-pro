from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import re
from typing import Literal

from chanlun.decision_support.fingerprints import canonical_json, normalize_datetime
from chanlun.decision_support.trading_system.backtest.data_audit import DataEvidence
from chanlun.decision_support.trading_system.backtest.metrics import (
    BootstrapIntervals,
    PerformanceMetrics,
    TradeGroupMetrics,
    calculate_metrics,
    clustered_bootstrap,
    sample_adequacy,
)
from chanlun.decision_support.trading_system.backtest.portfolio import BacktestRun
from chanlun.decision_support.trading_system.models import (
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    PointType,
)
from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
)


SCHEMA = "chanlun-low-drawdown-backtest"
STRATEGY_ID = STRICT_STRATEGY_ID
REQUIRED_ABLATION_IDS = (
    "original_definitions_only",
    "plus_sector_ranking",
    "plus_30m_context",
    "plus_1m_trigger",
    "plus_unified_buy_point_execution",
    "plus_portfolio_risk",
)
REQUIRED_BENCHMARK_IDS = (
    "csi_300",
    "csi_500",
    "eligible_universe_equal_weight",
)
LIVE_READY_BLOCKING_LIMITATIONS = frozenset(
    {"walk_forward_windows_evaluated_independently"}
)
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AblationResult:
    ablation_id: str
    label: str
    trade_count: int
    sample_reduction: Decimal
    net_return: Decimal
    max_drawdown: Decimal
    calmar: Decimal | None
    quality_change: Decimal
    completed: bool = True
    data_grade: Literal["certified", "research_only", "invalid"] = "certified"
    failure_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.ablation_id or not self.label or self.trade_count < 0:
            raise ValueError("invalid ablation identity")
        if not Decimal("0") <= self.sample_reduction <= Decimal("1"):
            raise ValueError("sample_reduction must be in [0, 1]")
        if self.max_drawdown < 0:
            raise ValueError("max_drawdown cannot be negative")
        if not self.completed and not self.failure_codes:
            raise ValueError("incomplete ablation requires failure codes")
        if len(self.failure_codes) != len(set(self.failure_codes)):
            raise ValueError("ablation failure codes must be unique")


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    benchmark_id: str
    label: str
    net_return: Decimal | None
    max_drawdown: Decimal | None
    data_grade: Literal["certified", "research_only", "invalid"]

    def __post_init__(self) -> None:
        if not self.benchmark_id or not self.label:
            raise ValueError("invalid benchmark identity")
        if self.max_drawdown is not None and self.max_drawdown < 0:
            raise ValueError("benchmark drawdown cannot be negative")


@dataclass(frozen=True, slots=True)
class WalkForwardWindowResult:
    window_id: str
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    selected_parameters: tuple[tuple[str, str | bool], ...]
    test_metrics: PerformanceMetrics
    closed_trade_count: int

    def __post_init__(self) -> None:
        if not self.window_id or self.closed_trade_count < 0:
            raise ValueError("invalid walk-forward result")
        if not (
            self.train_start
            <= self.train_end
            < self.validation_start
            <= self.validation_end
            < self.test_start
            <= self.test_end
        ):
            raise ValueError("walk-forward result periods overlap")


@dataclass(frozen=True, slots=True)
class BacktestEvaluationResult:
    aggregate_run: BacktestRun
    walk_forward_windows: tuple[WalkForwardWindowResult, ...] = ()
    parameter_robustness: tuple[tuple[str, Decimal], ...] = ()
    liquidity_metrics: tuple[tuple[str, Decimal], ...] = ()
    bootstrap_repetitions: int = 2000
    bootstrap_seed: int = 20260720
    enabled_buy_classes: tuple[PointType, ...] = (
        "1buy",
        "2buy",
        "3buy",
    )

    def __post_init__(self) -> None:
        if self.bootstrap_repetitions <= 0:
            raise ValueError("bootstrap_repetitions must be positive")
        for values, label in (
            (self.parameter_robustness, "parameter robustness"),
            (self.liquidity_metrics, "liquidity metric"),
        ):
            names = tuple(name for name, _value in values)
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate {label} name")


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("report decimal must be finite")
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else _decimal(value)


def _group_document(summary: TradeGroupMetrics) -> dict[str, object]:
    return {
        "trade_count": summary.trade_count,
        "net_pnl": _decimal(summary.net_pnl),
        "net_return": _decimal(summary.net_return),
        "win_rate": _optional_decimal(summary.win_rate),
        "expectancy": _optional_decimal(summary.expectancy),
    }


def _metrics_document(metrics: PerformanceMetrics) -> dict[str, object]:
    return {
        "net_return": _decimal(metrics.net_return),
        "max_drawdown": _decimal(metrics.max_drawdown),
        "max_drawdown_duration_bars": metrics.max_drawdown_duration_bars,
        "calmar": _optional_decimal(metrics.calmar),
        "ulcer_index": _decimal(metrics.ulcer_index),
        "worst_trade": _optional_decimal(metrics.worst_trade),
        "worst_day": _optional_decimal(metrics.worst_day),
        "worst_week": _optional_decimal(metrics.worst_week),
        "worst_month": _optional_decimal(metrics.worst_month),
        "value_at_risk_95": _optional_decimal(metrics.value_at_risk_95),
        "expected_shortfall_95": _optional_decimal(metrics.expected_shortfall_95),
        "win_rate": _optional_decimal(metrics.win_rate),
        "payoff_ratio": _optional_decimal(metrics.payoff_ratio),
        "profit_factor": _optional_decimal(metrics.profit_factor),
        "expectancy": _optional_decimal(metrics.expectancy),
        "exposure_ratio": _decimal(metrics.exposure_ratio),
        "turnover": _decimal(metrics.turnover),
        "total_cost": _decimal(metrics.total_cost),
        "cost_to_gross_profit": _optional_decimal(metrics.cost_to_gross_profit),
        "annualized_return": _optional_decimal(metrics.annualized_return),
        "sharpe": _optional_decimal(metrics.sharpe),
        "sortino": _optional_decimal(metrics.sortino),
        "warnings": list(metrics.warnings),
    }


def _confidence_document(intervals: BootstrapIntervals | None) -> object:
    if intervals is None:
        return None

    def interval(value) -> dict[str, str]:
        return {
            "lower": _decimal(value.lower),
            "median": _decimal(value.median),
            "upper": _decimal(value.upper),
        }

    return {
        "expectancy": interval(intervals.expectancy),
        "net_return": interval(intervals.net_return),
        "max_drawdown": interval(intervals.max_drawdown),
        "repetitions": intervals.repetitions,
        "seed": intervals.seed,
        "cluster_count": intervals.cluster_count,
    }


def _window_document(window: WalkForwardWindowResult) -> dict[str, object]:
    return {
        "window_id": window.window_id,
        "train": [window.train_start.isoformat(), window.train_end.isoformat()],
        "validation": [
            window.validation_start.isoformat(),
            window.validation_end.isoformat(),
        ],
        "test": [window.test_start.isoformat(), window.test_end.isoformat()],
        "selected_parameters": dict(window.selected_parameters),
        "closed_trade_count": window.closed_trade_count,
        "test_metrics": _metrics_document(window.test_metrics),
    }


def _ablation_document(row: AblationResult) -> dict[str, object]:
    return {
        "ablation_id": row.ablation_id,
        "label": row.label,
        "trade_count": row.trade_count,
        "sample_reduction": _decimal(row.sample_reduction),
        "net_return": _decimal(row.net_return),
        "max_drawdown": _decimal(row.max_drawdown),
        "calmar": _optional_decimal(row.calmar),
        "quality_change": _decimal(row.quality_change),
        "completed": row.completed,
        "data_grade": row.data_grade,
        "failure_codes": list(row.failure_codes),
    }


def _benchmark_document(row: BenchmarkResult) -> dict[str, object]:
    return {
        "benchmark_id": row.benchmark_id,
        "label": row.label,
        "net_return": _optional_decimal(row.net_return),
        "max_drawdown": _optional_decimal(row.max_drawdown),
        "data_grade": row.data_grade,
    }


def _content_sha256(report: dict[str, object]) -> str:
    without_hash = {
        key: value for key, value in report.items() if key != "content_sha256"
    }
    digest = hashlib.sha256(canonical_json(without_hash).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def verify_report_hash(report: dict[str, object]) -> bool:
    expected = report.get("content_sha256")
    return bool(
        isinstance(expected, str)
        and _HASH_RE.fullmatch(expected) is not None
        and expected == _content_sha256(report)
    )


def build_report(
    *,
    evidence: DataEvidence,
    result: BacktestEvaluationResult,
    ablations: tuple[AblationResult, ...],
    benchmarks: tuple[BenchmarkResult, ...],
    generated_at: datetime,
    algorithm_hashes: tuple[tuple[str, str], ...] = (),
    limitations: tuple[str, ...] = (),
    requested_range: tuple[date, date] | None = None,
    effective_range: tuple[date, date] | None = None,
    evaluation_mode: str = "walk_forward",
    sector_price_source: str = "qmt_gics3_component_composite",
    universe_summary: dict[str, object] | None = None,
    data_source_hashes: tuple[tuple[str, str], ...] = (),
) -> dict[str, object]:
    generated = normalize_datetime(generated_at, "generated_at")
    hash_names = tuple(name for name, _digest in algorithm_hashes)
    if len(hash_names) != len(set(hash_names)) or any(
        _HASH_RE.fullmatch(digest) is None for _name, digest in algorithm_hashes
    ):
        raise ValueError("algorithm hashes must be unique sha256 values")
    data_hash_names = tuple(name for name, _digest in data_source_hashes)
    if len(data_hash_names) != len(set(data_hash_names)) or any(
        _HASH_RE.fullmatch(digest) is None for _name, digest in data_source_hashes
    ):
        raise ValueError("data source hashes must be unique sha256 values")
    if requested_range is not None and requested_range[0] > requested_range[1]:
        raise ValueError("requested range start cannot follow end")
    if effective_range is not None and effective_range[0] > effective_range[1]:
        raise ValueError("effective range start cannot follow end")
    if evaluation_mode not in {"walk_forward", "fixed_policy_one_year"}:
        raise ValueError("unsupported evaluation mode")
    if not isinstance(sector_price_source, str) or not sector_price_source.strip():
        raise ValueError("sector price source is required")
    metrics = calculate_metrics(result.aggregate_run)
    adequacy = sample_adequacy(
        result.aggregate_run,
        enabled_buy_classes=result.enabled_buy_classes,
    )
    bootstrap = (
        None
        if not result.aggregate_run.trades
        else clustered_bootstrap(
            result.aggregate_run.trades,
            repetitions=result.bootstrap_repetitions,
            seed=result.bootstrap_seed,
        )
    )
    ablation_by_id = {row.ablation_id: row for row in ablations}
    benchmark_by_id = {row.benchmark_id: row for row in benchmarks}
    if len(ablation_by_id) != len(ablations):
        raise ValueError("duplicate ablation id")
    if len(benchmark_by_id) != len(benchmarks):
        raise ValueError("duplicate benchmark id")
    ordered_ablations = tuple(
        ablation_by_id[row_id]
        for row_id in REQUIRED_ABLATION_IDS
        if row_id in ablation_by_id
    )
    ordered_benchmarks = tuple(
        benchmark_by_id[row_id]
        for row_id in REQUIRED_BENCHMARK_IDS
        if row_id in benchmark_by_id
    )
    analyses_complete = (
        tuple(row.ablation_id for row in ordered_ablations) == REQUIRED_ABLATION_IDS
        and tuple(row.benchmark_id for row in ordered_benchmarks)
        == REQUIRED_BENCHMARK_IDS
    )
    ablation_evidence_available = all(
        row.completed and row.data_grade != "invalid" for row in ordered_ablations
    )
    benchmark_evidence_available = all(
        row.data_grade != "invalid" for row in ordered_benchmarks
    )
    blocking_limitations = tuple(
        limitation
        for limitation in limitations
        if limitation in LIVE_READY_BLOCKING_LIMITATIONS
    )
    concentration_passed = metrics.max_symbol_trade_concentration <= Decimal(
        "0.20"
    ) and metrics.max_sector_trade_concentration <= Decimal("0.20")
    failed_conditions: list[str] = []
    if evidence.grade != "certified":
        failed_conditions.append("data_evidence")
    if not adequacy.adequate:
        failed_conditions.append("sample_adequacy")
    if metrics.net_return <= 0:
        failed_conditions.append("net_return")
    if metrics.max_drawdown > Decimal("0.10"):
        failed_conditions.append("max_drawdown")
    if metrics.calmar is None or metrics.calmar < Decimal("1"):
        failed_conditions.append("calmar")
    if not concentration_passed:
        failed_conditions.append("concentration")
    if not analyses_complete:
        failed_conditions.append("analysis_completeness")
    if analyses_complete and not ablation_evidence_available:
        failed_conditions.append("ablation_evidence")
    if analyses_complete and not benchmark_evidence_available:
        failed_conditions.append("benchmark_evidence")
    if evaluation_mode == "walk_forward" and not result.walk_forward_windows:
        failed_conditions.append("walk_forward_evidence")
    if blocking_limitations:
        failed_conditions.append("execution_continuity")
    if evidence.grade == "invalid":
        status = "data_invalid"
    elif "data_evidence" in failed_conditions:
        status = "evidence_insufficient"
    elif "sample_adequacy" in failed_conditions:
        status = "sample_inadequate"
    elif any(
        condition in failed_conditions
        for condition in (
            "analysis_completeness",
            "ablation_evidence",
            "benchmark_evidence",
        )
    ):
        status = "analysis_incomplete"
    elif any(
        condition in failed_conditions
        for condition in (
            "walk_forward_evidence",
            "execution_continuity",
        )
    ):
        status = "methodology_incomplete"
    elif "concentration" in failed_conditions:
        status = "concentration_failed"
    elif failed_conditions:
        status = "performance_gate_failed"
    else:
        status = "live_ready"
    report_limitations = list(limitations)
    report_limitations.extend(evidence.failures)
    if not analyses_complete:
        report_limitations.append("required_ablations_or_benchmarks_missing")
    if analyses_complete and not ablation_evidence_available:
        report_limitations.append("required_ablation_evidence_invalid")
    if analyses_complete and not benchmark_evidence_available:
        report_limitations.append("required_benchmark_evidence_invalid")
    report_limitations.append("research_output_not_an_order_instruction")
    report: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": generated.isoformat(),
        "strategy_id": STRATEGY_ID,
        "strategy_label": "缠论原文定义 · 低回撤执行体系",
        "active_strategy_count": 1,
        "read_only": True,
        "historical": True,
        "no_order_execution": True,
        "evaluation_mode": evaluation_mode,
        "algorithm_hashes": [
            {"source": name, "sha256": digest}
            for name, digest in sorted(algorithm_hashes)
        ],
        "requested_range": (
            None
            if requested_range is None
            else {
                "start": requested_range[0].isoformat(),
                "end": requested_range[1].isoformat(),
            }
        ),
        "effective_range": (
            None
            if effective_range is None
            else {
                "start": effective_range[0].isoformat(),
                "end": effective_range[1].isoformat(),
            }
        ),
        "universe": dict(universe_summary or {}),
        "data_source_hashes": dict(sorted(data_source_hashes)),
        "data_evidence": {
            "grade": evidence.grade,
            "failures": list(evidence.failures),
            "warnings": list(evidence.warnings),
            "coverage": {name: _decimal(value) for name, value in evidence.coverage},
        },
        "execution_contract": {
            "context_frequency": "30m",
            "setup_frequency": "5m",
            "trigger_frequency": "1m",
            "point_classes_analyzed_independently": True,
            "buy_point_classes_share_execution_logic": True,
            "max_five_minute_setup_age_seconds": (MAX_FIVE_MINUTE_SETUP_AGE_SECONDS),
            "sector_price_source": sector_price_source,
            "sector_price_change_gate": False,
            "next_tradable_minute_fill": True,
            "entry_risk_ttl_seconds": 300,
            "entry_liquidity_resize": "one_shot_to_10pct_minute_volume",
            "exit_liquidity_execution": (
                "partial_up_to_10pct_minute_volume_until_complete"
            ),
            "t_plus_one": True,
            "intraday_structural_stop": True,
        },
        "walk_forward_windows": [
            _window_document(window)
            for window in sorted(
                result.walk_forward_windows,
                key=lambda row: row.window_id,
            )
        ],
        "aggregate_out_of_sample": _metrics_document(metrics),
        "point_type_metrics": {
            point_type: _group_document(summary)
            for point_type, summary in metrics.per_point_type
        },
        "sector_year_liquidity_metrics": {
            "sectors": {
                sector: _group_document(summary)
                for sector, summary in metrics.per_sector
            },
            "years": {
                str(year): _group_document(summary)
                for year, summary in metrics.per_year
            },
            "liquidity": {
                name: _decimal(value) for name, value in result.liquidity_metrics
            },
        },
        "bootstrap_intervals": _confidence_document(bootstrap),
        "ablations": [_ablation_document(row) for row in ordered_ablations],
        "parameter_robustness": {
            name: _decimal(value) for name, value in result.parameter_robustness
        },
        "benchmarks": [_benchmark_document(row) for row in ordered_benchmarks],
        "concentration": {
            "max_symbol_trade_fraction": _decimal(
                metrics.max_symbol_trade_concentration
            ),
            "max_sector_trade_fraction": _decimal(
                metrics.max_sector_trade_concentration
            ),
            "limit": "0.2",
            "passed": concentration_passed,
        },
        "sample_adequacy": {
            "passed": adequacy.adequate,
            "closed_trade_count": adequacy.closed_trade_count,
            "required_closed_trade_count": 200,
            "point_counts": dict(adequacy.point_counts),
            "required_per_enabled_buy_class": 50,
            "enabled_buy_classes": list(adequacy.enabled_buy_classes),
            "failures": list(adequacy.failures),
        },
        "verdict": {
            "live_ready": not failed_conditions,
            "status": status,
            "failed_conditions": failed_conditions,
        },
        "limitations": list(dict.fromkeys(report_limitations)),
    }
    report["content_sha256"] = _content_sha256(report)
    return report


__all__ = [
    "AblationResult",
    "BacktestEvaluationResult",
    "BenchmarkResult",
    "REQUIRED_ABLATION_IDS",
    "REQUIRED_BENCHMARK_IDS",
    "SCHEMA",
    "STRATEGY_ID",
    "WalkForwardWindowResult",
    "build_report",
    "verify_report_hash",
]
