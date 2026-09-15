"""Audit local OHLC files; no quotes, accounts, database, or network access.

Example:
    python script/audit_old_strokes.py tests/fixtures/QQQ.US_30m.parquet --output output/old-strokes.json

The optional range-gate comparison is a deliberately incomplete experiment,
not a replacement endpoint-selection algorithm. See docs/old_stroke_audit.md.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE, strict_base_config_revision
from chanlun.core.types import Kline
from chanlun.core.stroke_rules import old_pair_valid


class _RangeGateOnly(BiCalculator):
    """Historical greedy geometry plus the range gate, for comparison only.

    Confirmation is intentionally omitted: this experiment only measures how
    far the old single-endpoint selector can advance after adding a range gate.
    """

    def _check_stroke_validity(self, start_fx, end_fx):
        return old_pair_valid(start_fx, end_fx, self._ranges.query(start_fx.k.index, end_fx.k.index))

    def _rebuild_from_fxs(self, fxs, incremental=False, changed_from=None):
        endpoints = []
        for fx in fxs:
            if not endpoints:
                endpoints.append(fx)
            elif fx.type == endpoints[-1].type:
                if self._is_more_extreme(fx, endpoints[-1]) and (
                    len(endpoints) == 1 or self._check_stroke_validity(endpoints[-2], fx)
                ):
                    endpoints[-1] = fx
            elif self._check_stroke_validity(endpoints[-1], fx):
                endpoints.append(fx)
        self.bis = [self._create_bi(a, b, i) for i, (a, b) in enumerate(zip(endpoints, endpoints[1:]))]


def audit_file(path: Path, *, compare_range_gate: bool = False) -> dict:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    else:
        raise ValueError(f"expected a CSV or Parquet OHLC file: {path}")
    columns = ["date", "high", "low", "open", "close", "volume"]
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {sorted(missing)}")
    frame = frame[columns].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame["date"].isna().any() or frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise ValueError("dates must be present, unique and ordered")
    frame[columns[1:]] = frame[columns[1:]].astype(float)
    if not np.isfinite(frame[columns[1:]].to_numpy()).all():
        raise ValueError("OHLC values must be finite")
    if (frame["high"] < frame["low"]).any():
        raise ValueError("high must be at least low")
    raw = [
        Kline(i, row.date, row.high, row.low, row.open, row.close, row.volume)
        for i, row in enumerate(frame.itertuples(index=False))
    ]
    processor = CL_Kline_Process()
    processor.process_cl_klines(raw)
    calc = BiCalculator()
    calc.calculate(processor.cl_klines)
    issues = calc.audit_endpoint_ranges()
    adjacency_issues = calc.audit_endpoint_adjacency()
    result = {
        "input": str(path.resolve()),
        "raw_bars": len(raw),
        "merged_bars": len(processor.cl_klines),
        "strokes": len(calc.bis),
        "confirmed_strokes": len(calc.confirmed_bis),
        "pending_strokes": len(calc.pending_bis),
        "completion_status": calc.completion_status,
        "qualification_evidence": [asdict(item) for item in calc.qualification_evidence],
        "completion_evidence": [asdict(item) for item in calc.completion_evidence],
        "continuation_evidence": [asdict(item) for item in calc.continuation_evidence],
        "construction_state": calc.construction_state(),
        "selection_revisions": [asdict(item) for item in calc.selection_revisions],
        "last_endpoint_cl_index": calc.bis[-1].end.k.index if calc.bis else None,
        "last_confirmed_endpoint_cl_index": calc.confirmed_bis[-1].end.k.index if calc.confirmed_bis else None,
        "continuation_blocked_at": calc.continuation_blocked_at.isoformat() if calc.continuation_blocked_at else None,
        "old_spacing_violations": sum(b.end.k.index - b.start.k.index < 4 for b in calc.bis),
        "range_violations": len(issues),
        "confirmed_range_violations": sum(issue.done for issue in issues),
        "adjacency_violations": len(adjacency_issues),
        "issues": [asdict(issue) for issue in issues],
        "adjacency_issues": [asdict(issue) for issue in adjacency_issues],
        "range_and_adjacency_diagnostics": "historical-full-span-model; not active path-B vetoes",
    }
    if compare_range_gate:
        experiment = _RangeGateOnly()
        experiment.calculate(processor.cl_klines)
        result["range_gate_only_experiment"] = {
            "strokes": len(experiment.bis),
            "last_endpoint_cl_index": experiment.bis[-1].end.k.index if experiment.bis else None,
            "range_violations": len(experiment.audit_endpoint_ranges()),
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare-range-gate", action="store_true")
    args = parser.parse_args(argv)
    report = {
        "stroke_profile": STRICT_STROKE_MODE,
        "profile_revision": strict_base_config_revision(),
        "scope": "user path-B selection, third-edge closed-witness completion and source-discrepancy diagnostics",
        "price_basis": "OHLC values as stored in input; no CL price normalization",
        "files": [audit_file(path, compare_range_gate=args.compare_range_gate) for path in args.inputs],
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False,
                         default=lambda value: value.isoformat())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return report


if __name__ == "__main__":
    main()
