from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
from typing import Literal

from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
)
from chanlun.decision_support.trading_system.backtest.report import (
    BenchmarkResult,
    REQUIRED_BENCHMARK_IDS,
)


EvidenceGrade = Literal["certified", "research_only", "invalid"]


def _decimal(value: object) -> Decimal:
    converted = Decimal(str(value))
    if not converted.is_finite():
        raise ValueError("benchmark decimal must be finite")
    return converted


def _frozen_old_artifact(root: Path) -> BenchmarkResult:
    candidates = sorted(
        root.glob("*.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not str(payload.get("schema_version", "")).startswith(
                "chanlun-early-screening-backtest/"
            ):
                continue
            metrics = payload["result"]["metrics"]
            return BenchmarkResult(
                benchmark_id="frozen_old_artifact",
                label="frozen_old_artifact",
                net_return=_decimal(metrics["total_return"]),
                max_drawdown=_decimal(metrics["max_drawdown"]),
                data_grade="research_only",
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            continue
    return BenchmarkResult(
        benchmark_id="frozen_old_artifact",
        label="frozen_old_artifact",
        net_return=None,
        max_drawdown=None,
        data_grade="invalid",
    )


def _eligible_equal_weight(
    dataset: BacktestDataset,
    data_grade: EvidenceGrade,
) -> BenchmarkResult:
    membership = {
        (row.session, row.code) for row in dataset.memberships
    }
    closes: dict[tuple[object, str], tuple[object, Decimal]] = {}
    for bar in sorted(dataset.bars, key=lambda row: row.closed_at):
        key = (bar.closed_at.date(), bar.code)
        closes[key] = (bar.closed_at, bar.raw_close)
    sessions = sorted({session for session, _code in closes})
    if len(sessions) < 2 or not membership:
        return BenchmarkResult(
            benchmark_id="eligible_universe_equal_weight",
            label="eligible_universe_equal_weight",
            net_return=None,
            max_drawdown=None,
            data_grade="invalid",
        )

    by_session: dict[object, dict[str, Decimal]] = defaultdict(dict)
    for (session, code), (_closed_at, close) in closes.items():
        if (session, code) in membership:
            by_session[session][code] = close
    equity = Decimal("1")
    peak = equity
    maximum_drawdown = Decimal("0")
    previous = by_session[sessions[0]]
    for session in sessions[1:]:
        current = by_session[session]
        comparable = sorted(set(previous) & set(current))
        if comparable:
            period_return = sum(
                (current[code] / previous[code] - Decimal("1") for code in comparable),
                Decimal("0"),
            ) / Decimal(len(comparable))
            equity *= Decimal("1") + period_return
            peak = max(peak, equity)
            maximum_drawdown = max(
                maximum_drawdown,
                (peak - equity) / peak,
            )
        previous = current
    return BenchmarkResult(
        benchmark_id="eligible_universe_equal_weight",
        label="eligible_universe_equal_weight",
        net_return=equity - Decimal("1"),
        max_drawdown=maximum_drawdown,
        data_grade=data_grade,
    )


def build_required_benchmarks(
    dataset: BacktestDataset,
    *,
    data_grade: EvidenceGrade,
    frozen_artifact_root: Path,
) -> tuple[BenchmarkResult, ...]:
    rows = {
        "frozen_old_artifact": _frozen_old_artifact(frozen_artifact_root),
        "csi_300": BenchmarkResult(
            benchmark_id="csi_300",
            label="csi_300",
            net_return=None,
            max_drawdown=None,
            data_grade="invalid",
        ),
        "csi_500": BenchmarkResult(
            benchmark_id="csi_500",
            label="csi_500",
            net_return=None,
            max_drawdown=None,
            data_grade="invalid",
        ),
        "eligible_universe_equal_weight": _eligible_equal_weight(
            dataset,
            data_grade,
        ),
    }
    return tuple(rows[benchmark_id] for benchmark_id in REQUIRED_BENCHMARK_IDS)


__all__ = ["build_required_benchmarks"]
