#!/usr/bin/env python
"""Run the causal Chanlun low-drawdown research backtest."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Sequence
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
MAX_NATIVE_SECTOR_PAGES = 80
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.backtest.data_audit import (
    DataEvidence,
    audit_dataset,
)
from chanlun.decision_support.trading_system.backtest.benchmarks import (
    build_required_benchmarks,
)
from chanlun.decision_support.trading_system.backtest.data_source import (
    BacktestDataConfig,
    load_point_in_time_dataset,
    load_tdx_native_sector_bars,
)
from chanlun.decision_support.trading_system.backtest.models import BacktestDataset
from chanlun.decision_support.trading_system.backtest.report import (
    REQUIRED_ABLATION_IDS,
    REQUIRED_BENCHMARK_IDS,
    AblationResult,
    BacktestEvaluationResult,
    BenchmarkResult,
    build_report,
)
from chanlun.decision_support.trading_system.backtest.runner import (
    WalkForwardResearch,
    build_causal_period_runner,
    empty_evaluation,
    run_walk_forward_evaluation,
    run_required_ablations,
)
from chanlun.decision_support.trading_system.backtest.walk_forward import (
    build_walk_forward_windows,
)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD") from exc


def _positive_decimal(value: str) -> Decimal:
    try:
        converted = Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError("金额必须是十进制数") from exc
    if not converted.is_finite() or converted <= 0:
        raise argparse.ArgumentTypeError("金额必须大于 0")
    return converted


def _positive_int(value: str) -> int:
    try:
        converted = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if converted <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return converted


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--start", type=_parse_date, required=True)
    result.add_argument("--end", type=_parse_date, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument(
        "--initial-cash",
        type=_positive_decimal,
        default=Decimal("1000000"),
    )
    result.add_argument(
        "--bootstrap-repetitions",
        type=_positive_int,
        default=2000,
    )
    result.add_argument(
        "--dry-notifications",
        action="store_true",
        default=True,
        help="回测固定为仅记录；该选项不能关闭",
    )
    return result


def config_from(args: argparse.Namespace) -> BacktestDataConfig:
    if args.start > args.end:
        raise ValueError("start cannot follow end")
    if args.dry_notifications is not True:
        raise ValueError("backtest side effects must remain disabled")
    return BacktestDataConfig(start=args.start, end=args.end)


def _run_walk_forward(
    dataset: BacktestDataset,
    args: argparse.Namespace,
) -> WalkForwardResearch:
    schedule = build_walk_forward_windows(start=args.start, end=args.end)
    if not schedule:
        return run_walk_forward_evaluation(
            dataset,
            start=args.start,
            end=args.end,
            initial_cash=args.initial_cash,
            bootstrap_repetitions=args.bootstrap_repetitions,
        )
    data_grade = (
        "certified"
        if (
            dataset.membership_as_of_each_session
            and dataset.point_in_time_adjustment
            and dataset.security_status_as_of_each_session
        )
        else "research_only"
    )
    benchmarks = build_required_benchmarks(
        dataset,
        data_grade=data_grade,
        frozen_artifact_root=PROJECT_ROOT / "audit/early_screening_backtest",
    )
    sector_indices: dict[str, str] = {}
    for membership in dataset.memberships:
        index_code = membership.sector_id.rsplit(":", 1)[-1]
        if re.fullmatch(r"SH\.880\d{3}", index_code) is not None:
            sector_indices[membership.sector_id] = index_code
    max_pages = max(
        2,
        min(
            MAX_NATIVE_SECTOR_PAGES,
            ((args.end - args.start).days * 240) // 700 + 2,
        ),
    )
    native_sector_bars = load_tdx_native_sector_bars(
        sector_indices=sector_indices,
        start=args.start,
        end=args.end,
        max_pages=max_pages,
    )
    earliest_native = min(
        (bar.closed_at.date() for bar in native_sector_bars),
        default=None,
    )
    if earliest_native is None or earliest_native > schedule[0].train_start:
        observed_at = datetime.combine(
            args.start,
            datetime.min.time(),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        return WalkForwardResearch(
            evaluation=empty_evaluation(
                initial_cash=args.initial_cash,
                observed_at=observed_at,
                bootstrap_repetitions=args.bootstrap_repetitions,
            ),
            selected_parameters=(),
            limitations=(
                "native_sector_history_insufficient_for_walk_forward",
            ),
            benchmarks=benchmarks,
        )
    research = run_walk_forward_evaluation(
        dataset,
        start=args.start,
        end=args.end,
        initial_cash=args.initial_cash,
        bootstrap_repetitions=args.bootstrap_repetitions,
        period_runner=build_causal_period_runner(dataset, native_sector_bars),
    )
    ablations = run_required_ablations(
        dataset,
        start=args.start,
        end=args.end,
        initial_cash=args.initial_cash,
        selected_parameters=research.selected_parameters,
        period_runner_factory=lambda ablation_id: build_causal_period_runner(
            dataset,
            native_sector_bars,
            ablation_id=ablation_id,
        ),
        data_grade=data_grade,
    )
    return replace(research, ablations=ablations, benchmarks=benchmarks)


def _algorithm_hashes() -> tuple[tuple[str, str], ...]:
    package_root = SOURCE_ROOT / "chanlun/decision_support/trading_system"
    relative_paths = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in package_root.rglob("*.py")
    }
    relative_paths.update(
        {
            "src/chanlun/core/bs_branch.py",
            "src/chanlun/core/bs2_branch.py",
            "src/chanlun/core/cl.py",
            "tools/backtest_chanlun_trading_system.py",
        }
    )
    output: list[tuple[str, str]] = []
    for relative in sorted(relative_paths):
        path = PROJECT_ROOT / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        output.append((relative, "sha256:" + digest))
    return tuple(output)


def _verified_data_hashes(
    dataset: BacktestDataset,
) -> tuple[tuple[str, str], ...]:
    pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
    return tuple(
        (name, digest)
        for name, digest in dataset.source_hashes
        if pattern.fullmatch(digest) is not None
    )


def _unavailable_ablations(reason: str) -> tuple[AblationResult, ...]:
    return tuple(
        AblationResult(
            ablation_id=ablation_id,
            label=ablation_id,
            trade_count=0,
            sample_reduction=Decimal("0"),
            net_return=Decimal("0"),
            max_drawdown=Decimal("0"),
            calmar=None,
            quality_change=Decimal("0"),
            completed=False,
            data_grade="invalid",
            failure_codes=(reason,),
        )
        for ablation_id in REQUIRED_ABLATION_IDS
    )


def _unavailable_benchmarks() -> tuple[BenchmarkResult, ...]:
    return tuple(
        BenchmarkResult(
            benchmark_id=benchmark_id,
            label=benchmark_id,
            net_return=None,
            max_drawdown=None,
            data_grade="invalid",
        )
        for benchmark_id in REQUIRED_BENCHMARK_IDS
    )


def _build_report(
    *,
    dataset: BacktestDataset,
    evidence: DataEvidence,
    result: WalkForwardResearch | None,
    args: argparse.Namespace,
) -> dict[str, object]:
    if result is None:
        observed_at = datetime.combine(
            args.start,
            datetime.min.time(),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        evaluation: BacktestEvaluationResult = empty_evaluation(
            initial_cash=args.initial_cash,
            observed_at=observed_at,
            bootstrap_repetitions=args.bootstrap_repetitions,
        )
        limitations = ("evaluation_not_run_due_to_invalid_data",)
    else:
        evaluation = result.evaluation
        limitations = result.limitations
    unavailable_reason = (
        limitations[0] if limitations else "analysis_not_executed"
    )
    return build_report(
        evidence=evidence,
        result=evaluation,
        ablations=(
            result.ablations
            if result is not None and result.ablations
            else _unavailable_ablations(unavailable_reason)
        ),
        benchmarks=(
            result.benchmarks
            if result is not None and result.benchmarks
            else _unavailable_benchmarks()
        ),
        generated_at=datetime.now().astimezone(),
        algorithm_hashes=_algorithm_hashes(),
        limitations=limitations,
        requested_range=(args.start, args.end),
        data_source_hashes=_verified_data_hashes(dataset),
    )


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"unsupported report value: {type(value).__name__}")


def write_report_atomic(path: Path, report: object) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=_json_default,
    ) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _calendar_preflight() -> tuple[BacktestDataset, DataEvidence]:
    dataset = BacktestDataset(
        bars=(),
        statuses=(),
        memberships=(),
        corporate_actions=(),
        membership_as_of_each_session=False,
        point_in_time_adjustment=False,
        source_hashes=(),
        security_status_as_of_each_session=False,
    )
    evidence = DataEvidence(
        grade="research_only",
        failures=("insufficient_calendar_span_for_walk_forward",),
        warnings=("market_loading_skipped_by_calendar_preflight",),
        coverage=(
            ("bar_status_coverage", Decimal("0")),
            ("historical_membership", Decimal("0")),
            ("point_in_time_adjustment", Decimal("0")),
            ("historical_security_status", Decimal("0")),
        ),
    )
    return dataset, evidence


def _native_sector_capacity_preflight(
    args: argparse.Namespace,
) -> tuple[BacktestDataset, DataEvidence, WalkForwardResearch] | None:
    calendar_days = (args.end - args.start).days + 1
    estimated_sessions = max(1, (calendar_days * 5 + 6) // 7)
    required_pages = (estimated_sessions * 240 + 699) // 700
    if required_pages <= MAX_NATIVE_SECTOR_PAGES:
        return None
    dataset = BacktestDataset(
        bars=(),
        statuses=(),
        memberships=(),
        corporate_actions=(),
        membership_as_of_each_session=False,
        point_in_time_adjustment=False,
        source_hashes=(),
        security_status_as_of_each_session=False,
    )
    evidence = DataEvidence(
        grade="research_only",
        failures=("native_sector_history_capacity_insufficient",),
        warnings=("stock_loading_skipped_by_sector_evidence_preflight",),
        coverage=(
            (
                "native_sector_page_capacity",
                Decimal(MAX_NATIVE_SECTOR_PAGES) / Decimal(required_pages),
            ),
        ),
    )
    observed_at = datetime.combine(
        args.start,
        datetime.min.time(),
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    benchmarks = build_required_benchmarks(
        dataset,
        data_grade="invalid",
        frozen_artifact_root=PROJECT_ROOT / "audit/early_screening_backtest",
    )
    research = WalkForwardResearch(
        evaluation=empty_evaluation(
            initial_cash=args.initial_cash,
            observed_at=observed_at,
            bootstrap_repetitions=args.bootstrap_repetitions,
        ),
        selected_parameters=(),
        limitations=("native_sector_history_capacity_insufficient",),
        benchmarks=benchmarks,
    )
    return dataset, evidence, research


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = config_from(args)
        schedule = build_walk_forward_windows(start=args.start, end=args.end)
        if not schedule:
            dataset, evidence = _calendar_preflight()
            result = _run_walk_forward(dataset, args)
            report = _build_report(
                dataset=dataset,
                evidence=evidence,
                result=result,
                args=args,
            )
            write_report_atomic(args.output, report)
            return 2
        capacity_preflight = _native_sector_capacity_preflight(args)
        if capacity_preflight is not None:
            dataset, evidence, result = capacity_preflight
            report = _build_report(
                dataset=dataset,
                evidence=evidence,
                result=result,
                args=args,
            )
            write_report_atomic(args.output, report)
            return 2
        dataset = load_point_in_time_dataset(config)
        evidence = audit_dataset(dataset)
        if evidence.grade == "invalid":
            report = _build_report(
                dataset=dataset,
                evidence=evidence,
                result=None,
                args=args,
            )
            write_report_atomic(args.output, report)
            return 3
        result = _run_walk_forward(dataset, args)
        report = _build_report(
            dataset=dataset,
            evidence=evidence,
            result=result,
            args=args,
        )
        write_report_atomic(args.output, report)
        return 0 if evidence.grade == "certified" else 2
    except Exception as exc:
        print(
            f"backtest_runtime_error={type(exc).__name__}:{exc}",
            file=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
