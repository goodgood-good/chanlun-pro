#!/usr/bin/env python3
"""Fail closed unless the current 12-symbol replay gate matches current code."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    rendered = str(value)
    if rendered not in sys.path:
        sys.path.insert(0, rendered)

from chanlun.decision_support.trading_system.backtest.causality_gate_contract import (  # noqa: E402
    CAUSALITY_GATE_PROVEN_CONTROLS,
    CAUSALITY_GATE_SCHEMA,
    causality_gate_state_is_consistent,
)
from chanlun.decision_support.trading_system.backtest.report import (  # noqa: E402
    SCHEMA,
    verify_report_hash,
)
from tools import qmt_research_contract  # noqa: E402


def _algorithm_revision(hashes: Sequence[tuple[str, str]]) -> str:
    encoded = json.dumps(
        tuple(hashes),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _document(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"validation artifact is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"validation artifact is not a JSON object: {path}")
    return value


def validate_validation_gate(
    directory: Path,
    *,
    expected_symbol_count: int,
    current_algorithm_hashes: Sequence[tuple[str, str]] | None = None,
) -> dict[str, object]:
    root = directory.resolve(strict=True)
    report_path = (root / "report.json").resolve(strict=True)
    gate_path = (root / "causality_gate.json").resolve(strict=True)
    report = _document(report_path)
    gate = _document(gate_path)
    current_hashes = tuple(
        current_algorithm_hashes
        if current_algorithm_hashes is not None
        else qmt_research_contract.algorithm_hashes()
    )
    current_revision = _algorithm_revision(current_hashes)
    failures = gate.get("failures")
    controls = gate.get("proven_controls")
    recorded_report = gate.get("report")
    if (
        gate.get("schema") != CAUSALITY_GATE_SCHEMA
        or gate.get("status") != "passed"
        or not isinstance(failures, list)
        or not isinstance(controls, list)
        or tuple(controls) != CAUSALITY_GATE_PROVEN_CONTROLS
        or not causality_gate_state_is_consistent(
            status=gate.get("status"),
            pnl_generated=gate.get("pnl_generated"),
            failures=failures,
            report=recorded_report,
        )
        or not isinstance(recorded_report, str)
        or Path(recorded_report).resolve(strict=True) != report_path
        or gate.get("algorithm_revision") != current_revision
        or gate.get("validated_symbol_fact_count") != expected_symbol_count
        or not isinstance(gate.get("validated_decision_count"), int)
        or int(gate["validated_decision_count"]) <= 0
    ):
        raise ValueError("current small-scope causality gate is not eligible")
    universe = report.get("universe")
    report_hashes = report.get("algorithm_hashes")
    if (
        report.get("schema") != SCHEMA
        or not verify_report_hash(report)
        or not isinstance(universe, Mapping)
        or universe.get("selected_symbol_count") != expected_symbol_count
        or universe.get("causal_evaluation_count")
        != gate["validated_decision_count"]
        or not isinstance(report_hashes, list)
    ):
        raise ValueError("current small-scope report is not eligible")
    try:
        frozen_hashes = tuple(
            (str(row["source"]), str(row["sha256"]))
            for row in report_hashes
            if isinstance(row, Mapping)
        )
    except KeyError as exc:
        raise ValueError("current small-scope report hashes are malformed") from exc
    if len(frozen_hashes) != len(report_hashes):
        raise ValueError("current small-scope report hashes are malformed")
    if _algorithm_revision(frozen_hashes) != current_revision:
        raise ValueError("current small-scope report code revision is stale")
    return {
        "status": "passed",
        "validated_symbol_fact_count": expected_symbol_count,
        "validated_decision_count": gate["validated_decision_count"],
        "algorithm_revision": current_revision,
        "directory": str(root),
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--directory", type=Path, required=True)
    value.add_argument("--expected-symbol-count", type=int, default=12)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.expected_symbol_count <= 0:
        raise ValueError("expected symbol count must be positive")
    result = validate_validation_gate(
        args.directory,
        expected_symbol_count=args.expected_symbol_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
