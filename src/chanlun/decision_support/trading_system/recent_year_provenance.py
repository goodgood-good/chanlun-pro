"""Purpose-scoped source identity for recent-year sector-first research.

The repository-wide trading-system hash is useful for release provenance but
is too broad for long-running research artifacts: editing a human-review UI or
paper-notification formatter cannot change a historical sector composite or a
strict 30m/5m/1m scan.  This manifest binds exactly the price, structure,
selection and point-in-time metadata code that can affect those artifacts.

Any new dependency that participates in the computation must be added here.
The manifest itself is included, so changing its scope always changes the
resulting revision.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final


RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE: Final = (
    "chanlun-recent-year-sector-technical-research"
)

RECENT_YEAR_RESEARCH_ALGORITHM_PATHS: Final = (
    "src/chanlun/decision_support/fingerprints.py",
    "src/chanlun/tools/log_util.py",
    "src/chanlun/decision_support/trading_system/__init__.py",
    "src/chanlun/decision_support/trading_system/backtest/current_sector.py",
    "src/chanlun/decision_support/trading_system/backtest/fixed_year.py",
    "src/chanlun/decision_support/trading_system/backtest/models.py",
    "src/chanlun/decision_support/trading_system/backtest/pit_metadata.py",
    "src/chanlun/decision_support/trading_system/backtest/qmt_local_cache.py",
    "src/chanlun/decision_support/trading_system/conflicts.py",
    "src/chanlun/decision_support/trading_system/context.py",
    "src/chanlun/decision_support/trading_system/engine.py",
    "src/chanlun/decision_support/trading_system/execution_policy.py",
    "src/chanlun/decision_support/trading_system/lifecycle.py",
    "src/chanlun/decision_support/trading_system/models.py",
    "src/chanlun/decision_support/trading_system/provisional.py",
    "src/chanlun/decision_support/trading_system/qmt_causal_factor_adjustment.py",
    "src/chanlun/decision_support/trading_system/qmt_sector_same_base.py",
    "src/chanlun/decision_support/trading_system/runtime_config.py",
    "src/chanlun/decision_support/trading_system/sector_policy.py",
    "src/chanlun/decision_support/trading_system/sector_strength.py",
    "src/chanlun/decision_support/trading_system/structure_adapter.py",
    "src/chanlun/decision_support/trading_system/direct_recursive_structure.py",
    "src/chanlun/decision_support/trading_system/etf_proxy_facts.py",
    "src/chanlun/decision_support/trading_system/parameters.py",
    "src/chanlun/decision_support/trading_system/qmt_sector_ledger.py",
    "src/chanlun/decision_support/trading_system/recent_year_provenance.py",
    "src/chanlun/decision_support/trading_system/recent_year_research.py",
    "src/chanlun/decision_support/trading_system/sector_first_scope.py",
    "src/chanlun/decision_support/trading_system/sector_first_trigger_plan.py",
    "src/chanlun/decision_support/trading_system/sector_strength_replay.py",
    "src/chanlun/decision_support/trading_system/sector_trigger.py",
    "src/chanlun/decision_support/trading_system/selection.py",
    "src/chanlun/decision_support/trading_system/structure_adapter.py",
    "src/chanlun/exchange/kline_precision.py",
    "src/chanlun/exchange/price_basis.py",
    "tools/build_recent_year_current_sector_triggers.py",
)

RECENT_YEAR_RESEARCH_ALGORITHM_DIRECTORIES: Final = (
    "src/chanlun/core",
)


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def recent_year_research_algorithm_hashes(
    project_root: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    root = _default_project_root() if project_root is None else project_root.resolve()
    relative_paths = set(RECENT_YEAR_RESEARCH_ALGORITHM_PATHS)
    for relative_directory in RECENT_YEAR_RESEARCH_ALGORITHM_DIRECTORIES:
        directory = root / relative_directory
        if not directory.is_dir():
            raise FileNotFoundError(f"research algorithm directory is missing: {directory}")
        relative_paths.update(
            path.relative_to(root).as_posix() for path in directory.rglob("*.py")
        )
    output: list[tuple[str, str]] = []
    for relative in sorted(relative_paths):
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"research algorithm source is missing: {path}")
        output.append(
            (relative, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
        )
    return tuple(output)


def recent_year_research_algorithm_revision(
    hashes: tuple[tuple[str, str], ...],
) -> str:
    payload = {
        "scope": RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
        "hashes": hashes,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = (
    "RECENT_YEAR_RESEARCH_ALGORITHM_DIRECTORIES",
    "RECENT_YEAR_RESEARCH_ALGORITHM_PATHS",
    "RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE",
    "recent_year_research_algorithm_hashes",
    "recent_year_research_algorithm_revision",
)
