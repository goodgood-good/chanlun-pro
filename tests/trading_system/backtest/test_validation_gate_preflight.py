from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from chanlun.decision_support.fingerprints import canonical_json
from chanlun.decision_support.trading_system.backtest.causality_gate_contract import (
    CAUSALITY_GATE_PROVEN_CONTROLS,
    CAUSALITY_GATE_SCHEMA,
)
from chanlun.decision_support.trading_system.backtest.report import SCHEMA
from tools.verify_qmt_validation_gate import (
    _algorithm_revision,
    validate_validation_gate,
)


HASHES = (("src/example.py", "sha256:" + "a" * 64),)


def _write_validation(directory: Path, *, revision: str | None = None) -> None:
    directory.mkdir(parents=True)
    report_path = directory / "report.json"
    report = {
        "schema": SCHEMA,
        "universe": {
            "selected_symbol_count": 12,
            "causal_evaluation_count": 713,
        },
        "algorithm_hashes": [
            {"source": source, "sha256": digest} for source, digest in HASHES
        ],
    }
    report["content_sha256"] = (
        "sha256:"
        + hashlib.sha256(canonical_json(report).encode("utf-8")).hexdigest()
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")
    gate = {
        "schema": CAUSALITY_GATE_SCHEMA,
        "status": "passed",
        "pnl_generated": True,
        "algorithm_revision": revision or _algorithm_revision(HASHES),
        "validated_symbol_fact_count": 12,
        "validated_decision_count": 713,
        "proven_controls": list(CAUSALITY_GATE_PROVEN_CONTROLS),
        "failures": [],
        "report": str(report_path.resolve()),
    }
    (directory / "causality_gate.json").write_text(
        json.dumps(gate),
        encoding="utf-8",
    )


def test_current_validation_gate_allows_large_scope_preflight(tmp_path: Path) -> None:
    directory = tmp_path / "validation12"
    _write_validation(directory)

    result = validate_validation_gate(
        directory,
        expected_symbol_count=12,
        current_algorithm_hashes=HASHES,
    )

    assert result["status"] == "passed"
    assert result["validated_decision_count"] == 713


def test_stale_validation_gate_blocks_large_scope_preflight(tmp_path: Path) -> None:
    directory = tmp_path / "validation12"
    _write_validation(directory, revision="sha256:" + "b" * 64)

    with pytest.raises(ValueError, match="not eligible"):
        validate_validation_gate(
            directory,
            expected_symbol_count=12,
            current_algorithm_hashes=HASHES,
        )
