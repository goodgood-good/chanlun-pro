"""Shared immutable-artifact helpers for the active QMT research pipeline."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path

from chanlun.decision_support.trading_system.backtest.report import (
    REQUIRED_ABLATION_IDS,
    REQUIRED_BENCHMARK_IDS,
    AblationResult,
    BenchmarkResult,
)
from chanlun.decision_support.trading_system.selection import (
    SelectionResearchSnapshot,
    selection_research_by_symbol,
    selection_research_ledger_from_document,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"


def _local_module_path(module: str) -> Path | None:
    if module != "chanlun" and not module.startswith("chanlun."):
        return None
    base = SOURCE_ROOT.joinpath(*module.split("."))
    module_path = base.with_suffix(".py")
    if module_path.is_file():
        return module_path
    package_path = base / "__init__.py"
    return package_path if package_path.is_file() else None


def _package_initializers(path: Path) -> tuple[Path, ...]:
    try:
        relative = path.relative_to(SOURCE_ROOT)
    except ValueError:
        return ()
    output: list[Path] = []
    current = SOURCE_ROOT
    for part in relative.parts[:-1]:
        current /= part
        initializer = current / "__init__.py"
        if initializer.is_file():
            output.append(initializer)
    return tuple(output)


@lru_cache(maxsize=1)
def _fact_algorithm_relative_paths() -> tuple[str, ...]:
    """Return the transitive local-code boundary for per-symbol fact files."""

    class _ModuleImportCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.modules: list[str] = []

        def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
            self.modules.extend(alias.name for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
            if node.level == 0 and node.module:
                self.modules.append(node.module)
                self.modules.extend(
                    f"{node.module}.{alias.name}" for alias in node.names
                )

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            return

        def visit_AsyncFunctionDef(  # noqa: N802
            self, node: ast.AsyncFunctionDef
        ) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            return

    pending = [
        SOURCE_ROOT
        / "chanlun/decision_support/trading_system/backtest/fixed_year.py",
        PROJECT_ROOT / "tools/backtest_qmt_fixed_year.py",
    ]
    discovered: set[Path] = set()
    while pending:
        path = pending.pop().resolve()
        if path in discovered:
            continue
        if not path.is_file():
            raise FileNotFoundError(f"fact algorithm dependency is missing: {path}")
        discovered.add(path)
        pending.extend(
            initializer
            for initializer in _package_initializers(path)
            if initializer.resolve() not in discovered
        )
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        collector = _ModuleImportCollector()
        collector.visit(tree)
        for module in collector.modules:
            dependency = _local_module_path(module)
            if dependency is not None and dependency.resolve() not in discovered:
                pending.append(dependency)
    return tuple(
        sorted(path.relative_to(PROJECT_ROOT).as_posix() for path in discovered)
    )


def _hash_relative_paths(
    relative_paths: Iterable[str],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            relative,
            "sha256:"
            + hashlib.sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest(),
        )
        for relative in sorted(set(relative_paths))
    )


def fact_algorithm_hashes() -> tuple[tuple[str, str], ...]:
    """Hash only code that can alter an expensive per-symbol fact checkpoint."""

    return _hash_relative_paths(_fact_algorithm_relative_paths())


def algorithm_hashes() -> tuple[tuple[str, str], ...]:
    """Hash the complete extraction, audit and final-report implementation."""

    strategy_root = SOURCE_ROOT / "chanlun/decision_support/trading_system"
    core_root = SOURCE_ROOT / "chanlun/core"
    relative_paths = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for package_root in (strategy_root, core_root)
        for path in package_root.rglob("*.py")
    }
    relative_paths.update(
        {
            "src/chanlun/decision_support/fingerprints.py",
            "src/chanlun/exchange/kline_precision.py",
            "src/chanlun/exchange/price_basis.py",
            "src/chanlun/exchange/qmt_screening_sector_source.py",
            "tools/qmt_research_contract.py",
            "tools/backtest_qmt_fixed_year.py",
            "tools/audit_qmt_prefix_invariance.py",
            "tools/finalize_qmt_fixed_year.py",
            "tools/finalize_qmt_pit_fixed_year.py",
            "tools/snapshot_qmt_pit_metadata.py",
        }
    )
    relative_paths.update(_fact_algorithm_relative_paths())
    return _hash_relative_paths(relative_paths)


def unavailable_ablations(reason: str) -> tuple[AblationResult, ...]:
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


def unavailable_benchmarks() -> tuple[BenchmarkResult, ...]:
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
    payload = (
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=_json_default,
        )
        + "\n"
    )
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def load_selection_research_ledger(
    path: Path,
    *,
    replay_symbols: set[str],
) -> tuple[
    tuple[SelectionResearchSnapshot, ...],
    dict[str, tuple[SelectionResearchSnapshot, ...]],
]:
    """读取正式研究账本，并验证其标的范围属于本次回放。"""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("正式研究账本缺失或不可读") from exc
    snapshots = selection_research_ledger_from_document(raw)
    if not snapshots:
        raise ValueError("正式研究账本不能为空")
    by_symbol = selection_research_by_symbol(snapshots)
    unknown = set(by_symbol).difference(replay_symbols)
    if unknown:
        raise ValueError("正式研究账本包含回放范围外标的")
    return snapshots, by_symbol


__all__ = (
    "algorithm_hashes",
    "fact_algorithm_hashes",
    "load_selection_research_ledger",
    "unavailable_ablations",
    "unavailable_benchmarks",
    "write_report_atomic",
)
