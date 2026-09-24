"""One attributable review record for every frozen displayed segment.

These are pen-geometry checks. Saved render records have no pen locking times;
monotone integer tokens only let the calculator propagate saved closed flags.
They are never reported as market confirmation times. OHLC reconstruction must
close that separate evidence gap.
"""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from chanlun.core.xd_calculator import XdCalculator, _CandidateExtension, _GapConfirmationInvalidated
from chanlun.core.segment_evidence import PendingBoundary
from script.audit_all_segment_results import OUT, read_json, write_json, implementation
from script.check_segment_model import pen, check_evidence, check_first_reference, check_first_sequence, check_second_sequence, check_reverse_segment, check_return_segment, check_first_pen_continuation


def ticks(value, quantum):
    q = Decimal(str(value)) / Decimal(quantum)
    rounded = q.to_integral_value()
    if abs(q - rounded) > Decimal("0.00001"):
        raise ValueError(f"Displayed price is off its declared price grid: {value}/{quantum}")
    return int(rounded)


def geometry(row, quantum):
    a, b = row["points"]
    return (a["time"], ticks(a["price"], quantum), b["time"], ticks(b["price"], quantum),
            bool(row.get("locked")) and not row.get("selection_pending", False))


def rebuilt_geometry(line):
    return (int(line.start.k.date.timestamp()), line.start.val, int(line.end.k.date.timestamp()), line.end.val, line.done)


class TraceCalculator(XdCalculator):
    """Record declared search decisions without modifying their behavior."""
    def __init__(self):
        super().__init__()
        self.decisions = []

    def trace(self, result, start, check, mode):
        if isinstance(result, (_CandidateExtension, _GapConfirmationInvalidated, PendingBoundary)):
            self.decisions.append({"start": start, "check": check, "mode": mode,
                                   "kind": type(result).__name__, **asdict(result)})
        return result

    def _try_end(self, *args):
        return self.trace(super()._try_end(*args), args[1], args[6], "first-sequence")

    def _try_end_r34(self, *args):
        return self.trace(super()._try_end_r34(*args), args[1], args[5], "local-break")


def standard_append(out, item, up):
    # Separate tuple implementation, using all original feature elements.
    if out:
        old = out[-1]
        if (old[0] <= item[0] and item[1] <= old[1]) or (item[0] <= old[0] and old[1] <= item[1]):
            rising = up if len(out) == 1 else old[1] > out[-2][1]
            pick = max if rising else min
            low, high = pick(old[0], item[0]), pick(old[1], item[1])
            out[-1] = (low, high, old[2] + item[2], old[3] if low == old[0] else item[3],
                       old[4] if high == old[1] else item[4])
            return False
    out.append(item)
    return True


def interior_candidates(values, start, stop, direction):
    """Literal full-standard gapless fractals, not an alternative final partition.

    L079/L081 and an unresolved prior hypothesis can change qualification.
    A returned interior fractal is therefore a question requiring context,
    never an automatic finding that production must split at that point.
    """
    standard, found = [], []
    up = direction == "up"
    for i in range(start + 1, stop + 1, 2):
        b = values[i]
        if not standard_append(standard, (b.low, b.high, (i,), i, i), up) or len(standard) < 3:
            continue
        a, m, c = standard[-3:]
        turn = (m[0] > max(a[0], c[0]) and m[1] > max(a[1], c[1]) if up else
                m[0] < min(a[0], c[0]) and m[1] < min(a[1], c[1]))
        if not turn or max(a[0], m[0]) > min(a[1], m[1]):
            continue
        end = m[4 if up else 3] - 1
        if end >= start + 2 and end < stop:
            own = values[start:end:2]
            record = m[1] > max(b.high for b in own) if up else m[0] < min(b.low for b in own)
            found.append({"candidate_end": end, "witness": i, "first_sequence": [a, m, c], "new_record": record})
    return found


def checked(callback):
    try:
        callback()
        return {"passed": True}
    except (AssertionError, ValueError, IndexError) as exc:
        return {"passed": False, "reason": str(exc)}


def review_one(dataset, output, hashes):
    started = perf_counter()
    record = {**dataset, "implementation_sha256": hashes, "rows": [], "errors": [], "components": []}
    try:
        saved = json.loads(gzip.decompress(Path(dataset["file"]).read_bytes()))
        quantum = str(saved["snapshot"]["structure_price_quantum"])
        native = {(s["start_time"], s["start_tick"], s["end_time"], s["end_tick"]): s
                  for s in saved["snapshot"].get("screening_segments", [])}
        pen_groups, segment_groups = defaultdict(list), defaultdict(list)
        for b in saved["chart"]["bis"]:
            pen_groups[b.get("component_index", 0)].append(b)
        for i, s in enumerate(saved["chart"]["xds"], 1):
            segment_groups[s.get("component_index", 0)].append((i, s))
        for component, originals in pen_groups.items():
            values, points = [], []
            for i, b in enumerate(originals):
                a, z = b["points"]
                v = pen(i, ticks(a["price"], quantum), ticks(z["price"], quantum))
                v.start.k = SimpleNamespace(date=datetime.fromtimestamp(a["time"], timezone.utc))
                v.end.k = SimpleNamespace(date=datetime.fromtimestamp(z["time"], timezone.utc))
                v.locked_at = i + 1 if b.get("locked") else None
                v.selection_pending = b.get("selection_pending", False)
                values.append(v)
                points.append({"index": i, "start_time": a["time"], "end_time": z["time"],
                               "start_tick": v.start.val, "end_tick": v.end.val,
                               "locked": bool(b.get("locked")), "selection_pending": v.selection_pending})
                if i and (values[i - 1].end.val != v.start.val or values[i - 1].end.k.date != v.start.k.date):
                    record["errors"].append({"component": component, "pen": i, "reason": "saved_pen_chain_disconnected"})
            calc = TraceCalculator()
            calc.calculate(values)
            global_check = checked(lambda: check_evidence(calc, values))
            if not global_check["passed"]:
                record["errors"].append({"component": component, "reason": "declared_model_evidence", "detail": global_check})
            lines = {rebuilt_geometry(x): x for x in calc.xds}
            expected = segment_groups.get(component, [])
            if len(lines) != len(expected):
                record["errors"].append({"component": component, "reason": "saved_segment_count_difference", "saved": len(expected), "rebuilt": len(lines)})
            comp = {"component": component, "pens": points, "decisions": calc.decisions,
                    "tail_state": asdict(calc.tail_state), "model_evidence_check": global_check}
            record["components"].append(comp)
            for number, shown in expected:
                identity = geometry(shown, quantum)
                line = lines.get(identity)
                row = {"segment_id": f"{dataset['id']}:S{number:04}", "number": number, "component": component,
                       "start_time": identity[0], "start_tick": identity[1], "end_time": identity[2], "end_tick": identity[3],
                       "confirmed": identity[4], "price_quantum": quantum, "checks": {}, "context_flags": [],
                       "original_source_refs": ["R65-three-pens", "R67", "R78-direction"],
                       "clock_check": "requires_original_klines_and_pen_lock_times"}
                unit = native.get(identity[:4], {})
                row["saved_confirmed_at"] = unit.get("confirmed_at")
                row["checks"]["saved_partition_rebuilt"] = line is not None
                if line is None:
                    row["verdict"] = "partition_mismatch"
                    record["rows"].append(row)
                    continue
                a, b = line.start_line.index, line.end_line.index
                p = line.construction_evidence
                row.update(start_pen=a, end_pen=b, pen_count=b - a + 1, direction=line.type,
                           proof=asdict(p) if p else None, body_low_tick=line.low, body_high_tick=line.high)
                row["checks"].update(minimum_odd_pen_span=b - a >= 2 and (b - a) % 2 == 0,
                                     initial_three_overlap=max(values[a].low, values[a + 2].low) <= min(values[a].high, values[a + 2].high),
                                     direction_consistent=(line.end.val > line.start.val if line.type == "up" else line.end.val < line.start.val),
                                     confirmed_has_proof=not line.done or p is not None)
                if number == 1:
                    row["context_flags"].append("observation_window_origin")
                if line.start.val != (line.low if line.type == "up" else line.high):
                    row["context_flags"].append("start_not_body_extreme")
                if line.end.val != (line.high if line.type == "up" else line.low):
                    row["context_flags"].append("end_not_body_extreme")
                if p:
                    if p.first_pen_continuation is not None:
                        row["checks"]["physical_first_pen_continuation"] = checked(
                            lambda: check_first_pen_continuation(calc.evidence, calc.evidence.index(p), values)
                        )["passed"]
                        row["context_flags"].append("physical_first_pen_continuation")
                        row["original_source_refs"].append("R71-physical-first-completion")
                    elif p.return_segment is not None:
                        row["checks"]["completed_return_segment_proof"] = checked(
                            lambda: check_return_segment(calc.evidence, calc.evidence.index(p), values)
                        )["passed"]
                        row["context_flags"].append("completed_return_segment")
                        row["original_source_refs"].append("R78-return-before-origin")
                    elif p.reverse_segment is not None:
                        row["checks"]["complete_reverse_segment_chain"] = checked(
                            lambda: check_reverse_segment(calc.evidence, calc.evidence.index(p), values)
                        )["passed"]
                        row["context_flags"].append("completed_reverse_segment")
                        row["original_source_refs"].append("R78-backward-transmission")
                    else:
                        row["checks"]["declared_reference_reconstructed"] = checked(lambda: check_first_reference(p, values))["passed"]
                        row["checks"]["post_boundary_sequence_reconstructed"] = checked(lambda: check_first_sequence(p, values))["passed"]
                    if p.second_sequence:
                        row["checks"]["second_sequence_reconstructed"] = checked(lambda: check_second_sequence(p, values))["passed"]
                        row["context_flags"].append("second_feature_confirmation")
                    if p.parent_key:
                        row["context_flags"].append("inherited_second_feature")
                    if p.origin_evidence:
                        row["context_flags"].append("origin_recovery")
                    if p.reference_mode == "standard-local":
                        row["context_flags"].append("standard_local_reference")
                    if p.pivot_stem:
                        row["context_flags"].append("contained_pivot_stem")
                    if p.first_sequence:
                        l, m, r = p.first_sequence
                        strict = (m.low > max(l.low, r.low) and m.high > max(l.high, r.high) if line.type == "up" else
                                  m.low < min(l.low, r.low) and m.high < min(l.high, r.high))
                        if not strict:
                            row["context_flags"].append("boundary_containment_requires_protection")
                    gap = p.gap_context
                    if gap and gap.raw_gap != gap.standard_gap:
                        row["context_flags"].append("raw_standard_gap_disagreement")
                    first = values[b + 1]
                    moved = any(x.low < first.low if line.type == "up" else x.high > first.high
                                for x in values[b + 2:p.witness_index + 1])
                    if not moved and not p.parent_key and not p.second_sequence:
                        row["context_flags"].append("first_reversal_extreme_not_extended")
                    row["original_source_refs"] += ["R71-boundary", "R75-equality", "R78-second", "R79", "R81"]
                candidates = interior_candidates(values, a, b, line.type)
                if candidates:
                    row["interior_standard_fractals"] = candidates
                    row["context_flags"].append("interior_full_standard_fractal")
                row["verdict"] = "check_failed" if not all(row["checks"].values()) else (
                    "pending_tail" if p is None else "context_review" if row["context_flags"] else "local_conditions_supported")
                record["rows"].append(row)
        record["saved_segments"] = dataset["segments"]
        record["reviewed_segments"] = len(record["rows"])
        if record["reviewed_segments"] != dataset["segments"]:
            record["errors"].append({"reason": "segment_coverage_incomplete"})
    except Exception as exc:
        record["errors"].append({"reason": "dataset_exception", "error": repr(exc)})
    record["seconds"] = perf_counter() - started
    record["counts"] = dict(Counter(s["verdict"] for s in record["rows"]))
    record["flag_counts"] = dict(Counter(f for s in record["rows"] for f in s["context_flags"]))
    target = Path(output) / "geometry" / (dataset["id"] + ".json.gz")
    write_json(target, record)
    return {k: v for k, v in record.items() if k not in {"rows", "components"}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=OUT)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int)
    args = p.parse_args()
    inventory = read_json(args.output / "inventory.json")
    jobs = inventory["datasets"][:args.limit]
    hashes, results = implementation(), []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(review_one, d, args.output, hashes) for d in jobs]
        for future in as_completed(futures):
            results.append(future.result())
            if len(results) % 100 == 0 or len(results) == len(jobs):
                print(json.dumps({"done": len(results), "total": len(jobs), "segments": sum(r.get("reviewed_segments", 0) for r in results),
                                  "datasets_with_errors": sum(bool(r["errors"]) for r in results)}), flush=True)
    assert hashes == implementation(), "Production code changed during the review"
    report = {"implementation_sha256": hashes, "datasets": results, "completed": len(results),
              "segments": sum(r.get("reviewed_segments", 0) for r in results),
              "datasets_with_errors": sum(bool(r["errors"]) for r in results),
              "verdict_counts": dict(sum((Counter(r["counts"]) for r in results), Counter())),
              "flag_counts": dict(sum((Counter(r["flag_counts"]) for r in results), Counter())),
              "audit_type": "per-segment geometry and declared interpretation; raw time proof is separate"}
    write_json(args.output / ("geometry_pilot.json" if args.limit else "geometry_results.json"), report)
    print(json.dumps({k: v for k, v in report.items() if k != "datasets"}), flush=True)


if __name__ == "__main__":
    main()
