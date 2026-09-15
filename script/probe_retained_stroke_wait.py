"""Find v11 selected-origin waits hiding four or more later candidate edges.

This inspects an implementation branch, not a universal source-rule oracle.
The four-edge threshold only reduces audit noise; it is not a pen definition.
No production files, databases, or running-service caches are modified.
"""

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE, strict_base_config_revision
from chanlun.core.types import Kline


def check_candidate(merged, calc, path):
    result = []
    for start, end in zip(path, path[1:]):
        first, second = (calc._resolver.nodes[i].fx for i in (start, end))
        top, bottom = (first, second) if first.type == "ding" else (second, first)
        span = merged.cl_klines[first.k.index:second.k.index + 1]
        physical = []
        for fx in (first, second):
            left, middle, right = merged.cl_klines[fx.k.index - 1:fx.k.index + 2]
            physical.append((middle.h > max(left.h, right.h) and middle.l > max(left.l, right.l))
                            if fx.type == "ding" else
                            (middle.h < min(left.h, right.h) and middle.l < min(left.l, right.l)))
        result.append(dict(raw_start=first.k.k_index, raw_end=second.k.k_index,
                           independent_source_conditions=bool(
                               all(physical) and first.type != second.type and second.k.index - first.k.index >= 4
                               and top.k.h > bottom.k.h and top.k.l > bottom.k.l
                               and max(k.h for k in span) == top.val and min(k.l for k in span) == bottom.val)))
    return result


def inspect(path):
    frame = pd.read_parquet(path)
    merged, calc = CL_Kline_Process(), BiCalculator()
    raw, events = [], []
    previous_decision = None
    for index, row in enumerate(frame.itertuples(index=False)):
        raw.append(Kline(index, row.date, row.high, row.low, row.open, row.close, row.volume))
        merged.process_cl_klines(raw)
        calc.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                       validated_incremental_prefix=True)
        resolver = calc._resolver
        decision = resolver.last_decision
        if decision is None or decision is previous_decision:
            continue
        previous_decision = decision
        if decision.action != "await_qualified_opposite" or resolver.selected < 0:
            continue
        candidate = resolver.nodes[decision.fractal]
        selected = resolver.path()
        if candidate.parent < 0 or candidate.origin == resolver.nodes[resolver.selected].origin:
            continue
        live = resolver.sequence.live_starts()
        retained_live = live.intersection(selected)
        if not retained_live or selected[-1] in live:
            continue
        pending_path = resolver.path(decision.fractal)
        if len(pending_path) < 5 or pending_path[0] <= selected[-1]:
            continue
        source_checks = check_candidate(merged, calc, pending_path)
        assert all(check["independent_source_conditions"] for check in source_checks)
        cold_merged, cold_calc = CL_Kline_Process(), BiCalculator()
        cold_merged.process_cl_klines(raw)
        cold_calc.calculate(cold_merged.cl_klines)
        signature = lambda engine: [(b.start.k.k_index, b.end.k.k_index, b.start.val, b.end.val,
                                     b.selected_at, b.continuation_at, b.completion_status) for b in engine.bis]
        assert signature(calc) == signature(cold_calc)
        events.append(dict(
            raw_prefix=index + 1, observed_at=row.date,
            selected_stroke_count=len(calc.bis),
            selected_tail_raw_center=resolver.nodes[selected[-1]].fx.k.k_index,
            live_retained_raw_centers=[resolver.nodes[i].fx.k.k_index for i in sorted(retained_live)],
            later_candidate_edges=len(pending_path) - 1,
            later_candidate_raw_centers=[resolver.nodes[i].fx.k.k_index for i in pending_path],
            reported_blocked_at=calc.continuation_blocked_at,
            unresolved_equal_choices=[asdict(d) for d in calc.unresolved_endpoint_choices],
            source_condition_checks=source_checks, cold_replay_matches=True,
            decision=asdict(decision),
        ))
    return dict(input=str(path.resolve()), input_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                raw_bars=len(raw), events=events)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = dict(profile=STRICT_STROKE_MODE, profile_revision=strict_base_config_revision(),
                  selection_rule="unrelated qualified candidate, live earlier retained point, dead selected tail",
                  minimum_candidate_edges=4,
                  audit_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), markets=[])
    for path in args.market:
        result = inspect(path)
        report["markets"].append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(dict(input=str(path), raw_bars=result["raw_bars"], events=len(result["events"]),
                              maximum_hidden_candidate_edges=max((e["later_candidate_edges"] for e in result["events"]), default=0))),
              flush=True)


if __name__ == "__main__":
    main()
