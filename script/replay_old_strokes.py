"""Replay local old-stroke selection and continuation evidence against cold calculation.

No permanent-completion claim is made. Use audit_old_strokes.py to inspect
individual range/adjacency violations and selection-revision records.
"""

import argparse
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE, strict_base_config_revision
from chanlun.core.types import Kline


def _signature(calc):
    return tuple((bi.start.k.index, bi.end.k.index, bi.start.k.k_index, bi.end.k.k_index,
                  bi.start.val, bi.end.val, bi.forming, bi.locked_at,
                  bi.fractal_visible_at, bi.selected_at) for bi in calc.bis)


def replay(name, raw, cold_every):
    merged, live = CL_Kline_Process(), BiCalculator()
    prefix, cold_checks = [], 0
    confirmed = {}
    for end, bar in enumerate(raw, 1):
        prefix.append(bar)
        merged.process_cl_klines(prefix)
        live.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                       validated_incremental_prefix=True)
        assert len(live.confirmed_bis) == len(live.completion_evidence) == max(0, len(live.bis) - 1)
        now = {(b.start.k.index, b.end.k.index): (b.start.val, b.end.val, b.locked_at)
               for b in live.confirmed_bis}
        if any(now.get(key) != record for key, record in confirmed.items()):
            assert any(r.revises_confirmed_selection and r.observed_at == bar.date
                       for r in live.selection_revisions), (name, end, "unrecorded revision")
        confirmed = now
        assert all(b.selected_at <= b.locked_at <= bar.date for b in live.confirmed_bis)
        assert len(live.qualification_evidence) == len(live.bis)
        assert all(e.fractal_visible_at <= e.selected_at <= bar.date for e in live.qualification_evidence)
        if end % cold_every == 0 or end == len(raw):
            cold_merged, cold = CL_Kline_Process(), BiCalculator()
            cold_merged.process_cl_klines(prefix)
            cold.calculate(cold_merged.cl_klines)
            assert [live._kline_sig(k) for k in merged.cl_klines] == [
                cold._kline_sig(k) for k in cold_merged.cl_klines
            ], (name, end, "inclusion replay")
            assert _signature(live) == _signature(cold), (name, end, "selection replay")
            assert live.qualification_evidence == cold.qualification_evidence, (name, end, "qualification replay")
            assert live.completion_evidence == cold.completion_evidence, (name, end, "completion replay")
            assert live.selection_revisions == cold.selection_revisions, (name, end, "revision replay")
            assert not live.audit_endpoint_ranges(), (name, end, "range violation")
            assert not live.audit_endpoint_adjacency(), (name, end, "adjacency violation")
            assert all(fx.done for fx in live.fxs), (name, end, "physical fractal state")
            cold_checks += 1
    result = dict(input=name, raw_bars=len(raw), strokes=len(live.bis),
                  confirmed_strokes=len(live.confirmed_bis), pending_strokes=len(live.pending_bis),
                  cold_checks=cold_checks, revisions=len(live.selection_revisions),
                  confirmed_selection_revisions=sum(r.revises_confirmed_selection for r in live.selection_revisions),
                  blocked=live.continuation_blocked_at is not None,
                  completion_status=live.completion_status)
    print(json.dumps(result), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="*")
    parser.add_argument("--cold-every", type=int, default=509)
    parser.add_argument("--random-seeds", type=int, default=30)
    parser.add_argument("--random-bars", type=int, default=250)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cold_every < 1 or args.random_seeds < 0 or args.random_bars < 1:
        parser.error("cold-every/random-bars must be positive; random-seeds must be nonnegative")
    results = []
    for path in args.inputs:
        frame = pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        raw = [Kline(i, row.date, row.high, row.low, row.open, row.close, row.volume)
               for i, row in enumerate(frame.itertuples(index=False))]
        results.append(replay(str(path), raw, args.cold_every))
    base = pd.Timestamp("2026-01-01", tz="UTC")
    for seed in range(args.random_seeds):
        rng, raw, price = random.Random(seed), [], 100
        for i in range(args.random_bars):
            price += rng.randint(-8, 8)
            high, low = price + rng.randint(0, 5), price - rng.randint(0, 5)
            raw.append(Kline(i, base + pd.Timedelta(minutes=i), high, low, low, high, 1))
        results.append(replay(f"random-{seed}", raw, 1))
    report = dict(stroke_profile=STRICT_STROKE_MODE,
                  profile_revision=strict_base_config_revision(), results=results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    main()
