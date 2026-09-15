"""Read-only v7 adversarial checks; production calculation is not modified."""

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.base_profile import (
    STRICT_STROKE_MODE, strict_base_config, strict_base_config_revision,
)
from chanlun.core.types import Kline


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def bars(prices):
    return [Kline(i, BASE + timedelta(minutes=i), hi, lo, (hi + lo) / 2,
                  (hi + lo) / 2, 1) for i, (hi, lo) in enumerate(prices)]


def signature(calc):
    return [(b.start.k.index, b.end.k.index, b.start.k.k_index, b.end.k.k_index,
             b.start.val, b.end.val, b.forming, b.locked_at,
             b.fractal_visible_at, b.selected_at) for b in calc.bis]


def cold(raw):
    merged, calc = CL_Kline_Process(), BiCalculator()
    merged.process_cl_klines(raw)
    calc.calculate(merged.cl_klines)
    return merged, calc


def advance(merged, calc, raw):
    merged.process_cl_klines(raw)
    calc.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                   validated_incremental_prefix=True)


def compare(raw, merged, calc):
    other_merged, other = cold(raw)
    if [calc._kline_sig(k) for k in merged.cl_klines] != [
        calc._kline_sig(k) for k in other_merged.cl_klines
    ]:
        return "inclusion_replay"
    if signature(calc) != signature(other):
        return "stroke_replay"
    if calc.qualification_evidence != other.qualification_evidence:
        return "qualification_replay"
    if calc.completion_evidence != other.completion_evidence:
        return "completion_replay"
    return None


def fuzz(seeds, count, updates):
    failures, checks, prefixes = [], 0, 0
    for seed in range(seeds):
        rng, price = random.Random(seed + 1000), 10000
        prices = []
        for i in range(count):
            price += rng.randint(-20, 20)
            spread = 25 if i % 9 == 0 else 7
            prices.append((price + rng.randint(0, spread), price - rng.randint(0, spread)))
        raw, merged, calc = [], CL_Kline_Process(), BiCalculator()
        failure = None
        for i, final in enumerate(bars(prices)):
            raw.append(final)
            stages = (0.0, 0.5, 1.0) if updates else (1.0,)
            for stage in stages:
                middle = (final.h + final.l) / 2
                raw[-1] = Kline(i, final.date, middle + (final.h - middle) * stage,
                                middle - (middle - final.l) * stage, middle, middle, 1)
                try:
                    advance(merged, calc, raw)
                    prefixes += 1
                    if updates or i % 17 == 0 or i == count - 1:
                        checks += 1
                        failure = compare(raw, merged, calc)
                    if not failure and calc.audit_endpoint_ranges():
                        failure = "endpoint_range"
                    if not failure and calc.audit_endpoint_adjacency():
                        failure = "endpoint_adjacency"
                except Exception as exc:
                    failure = f"{type(exc).__name__}: {exc}"
                if failure:
                    _, other = cold(raw)
                    failures.append(dict(seed=seed + 1000, prefix=i + 1, stage=stage,
                                         failure=failure, prices=[(b.h, b.l) for b in raw],
                                         live=signature(calc), cold=signature(other)))
                    break
            if failure:
                break
        if seed % 25 == 24:
            print(f"seeds={seed + 1} prefixes={prefixes} failures={len(failures)}", flush=True)
    return dict(seeds=seeds, prefixes=prefixes, cold_checks=checks, failures=failures)


def market(path):
    frame = pd.read_parquet(path)
    raw = [Kline(i, row.date, row.high, row.low, row.open, row.close, row.volume)
           for i, row in enumerate(frame.itertuples(index=False))]
    merged, calc = CL_Kline_Process(), BiCalculator()
    previous, events, blocked, max_rollback = [], [], [], None
    for end in range(1, len(raw) + 1):
        advance(merged, calc, raw[:end])
        now = signature(calc)
        old_locked = {tuple(x[:6]): x for x in previous if x[7] is not None}
        now_locked = {tuple(x[:6]): x for x in now if x[7] is not None}
        removed = [x for key, x in old_locked.items() if key not in now_locked]
        moved = [dict(before=x, after=now_locked[key]) for key, x in old_locked.items()
                 if key in now_locked and x[7] != now_locked[key][7]]
        if removed or moved:
            event = dict(raw_prefix=end, observed_at=raw[end - 1].date,
                         before_count=len(previous), after_count=len(now),
                         removed_confirmed=removed, moved_confirmation=moved,
                         before_tail=previous[-6:], after_tail=now[-6:],
                         decision=asdict(calc._resolver.last_decision))
            events.append(event)
            if max_rollback is None or len(removed) > len(max_rollback["removed_confirmed"]):
                max_rollback = event
        if calc.continuation_blocked_at is not None:
            blocked.append(end)
        previous = now
    result = dict(input=str(path), raw_bars=len(raw), final_strokes=len(calc.bis),
                  confirmed_revision_events=len(events), events=events,
                  max_rollback=max_rollback, blocked_prefixes=blocked,
                  range_violations=[asdict(x) for x in calc.audit_endpoint_ranges()],
                  adjacency_violations=[asdict(x) for x in calc.audit_endpoint_adjacency()])
    print(json.dumps({k: result[k] for k in ("input", "raw_bars", "final_strokes",
                                           "confirmed_revision_events")}), flush=True)
    return result


def downstream(path, limit):
    frame = pd.read_parquet(path).head(limit)
    code, frequency = path.stem.rsplit("_", 1)
    config = dict(strict_base_config(), structure_price_quantum="0.01",
                  price_basis_revision="probe-raw", strict_config_revision="probe-v7")
    cd = CL(code, frequency, config, market="us" if code.endswith("US") else "a")
    previous_bis, previous_xds, previous_centers, previous_signature = {}, {}, {}, None
    events, errors, cold_checks = [], [], 0
    for end, row in enumerate(frame.itertuples(index=False), 1):
        cd.process_kline_values(row.date, row.open, row.high, row.low, row.close, row.volume)
        current_signature = signature(cd.bi_calculator)
        if current_signature == previous_signature and end != len(frame):
            continue
        previous_signature = current_signature
        bis = {tuple(b[:6]): b[7] for b in current_signature if b[7] is not None}
        xds = {(x.start.k.k_index, x.end.k.k_index, x.start.val, x.end.val):
               (x.formed_at, x.locked_at) for x in cd.get_xds() if x.is_done()}
        removed_bis = [k for k in previous_bis if k not in bis]
        removed_xds = [k for k in previous_xds if k not in xds]
        changed_xds = [dict(key=k, before=v, after=xds[k]) for k, v in previous_xds.items()
                       if k in xds and v != xds[k]]
        try:
            result = cd.get_native_centers()
            centers = {x.center_id: (x.confirmed_at if hasattr(x, "confirmed_at") else None)
                       for x in result.centers}
            removed_centers = [k for k in previous_centers if k not in centers]
            if removed_bis or removed_xds or changed_xds or removed_centers:
                event = dict(raw_prefix=end, observed_at=row.date,
                             removed_confirmed_bis=removed_bis,
                             removed_confirmed_xds=removed_xds,
                             changed_xd_confirmation=changed_xds,
                             removed_formal_centers=removed_centers)
                events.append(event)
            if (removed_bis and (removed_xds or removed_centers)) or end == len(frame):
                other = CL(code, frequency, config, market=cd.market)
                other.process_klines(frame.head(end))
                cold_checks += 1
                if cd.get_native_centers() != other.get_native_centers():
                    errors.append(dict(raw_prefix=end, error="center_replay_mismatch"))
            previous_centers = centers
        except Exception as exc:
            errors.append(dict(raw_prefix=end, error=f"{type(exc).__name__}: {exc}"))
            # Save the first exception, keep the run bounded and reviewable.
            break
        previous_bis, previous_xds = bis, xds
    result = dict(input=str(path), raw_bars=len(frame), evaluated_to=end,
                  events=events, errors=errors, cold_checks=cold_checks)
    print(json.dumps({k: result[k] for k in ("input", "raw_bars", "evaluated_to", "errors")}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=150)
    parser.add_argument("--bars", type=int, default=100)
    parser.add_argument("--updates", action="store_true")
    parser.add_argument("--market", type=Path, nargs="*", default=[])
    parser.add_argument("--downstream", type=Path, nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = dict(stroke_profile=STRICT_STROKE_MODE, profile_revision=strict_base_config_revision(),
                  fuzz=fuzz(args.seeds, args.bars, args.updates),
                  markets=[market(path) for path in args.market],
                  downstream=[downstream(path, args.limit) for path in args.downstream])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
