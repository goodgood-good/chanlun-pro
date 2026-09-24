"""Reproduce the QQQ.US minute-chart review from frozen, attributable inputs.

This is an audit, not a replacement segment algorithm. Experimental alternatives
are explicitly labelled and checked against the existing source-figure cases.
All theoretical evidence is read from D:/缠论, never from repository commentary.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sys
from time import perf_counter
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import pandas as pd

from chanlun.core.cl import CL
from script.validate_segment_corpus import (
    check_structure, fixed_prefixes, fresh_matches, pen_state, physical,
)
from script.check_segment_candidates import SearchAuditCalculator


DEFAULT_OUTPUT = ROOT / "output/qqq_1m_audit_20260917"
SOURCE_ROOT = Path("D:/缠论/chanlun_lesson_corpus")
SOURCE_RANGES = {
    "R65-inclusion": (65, 34, 64),
    "R65-three-pens": (65, 220, 271),
    "R67": (67, 40, 118),
    "R71-invalidation": (71, 100, 124),
    "R71-boundary": (71, 172, 208),
    "R75-equality": (75, 412, 421),
    "R77": (77, 367, 418),
    "R78-direction": (78, 40, 82),
    "R78-complex": (78, 97, 157),
    "R78-second": (78, 208, 259),
    "R79": (79, 247, 295),
    "R81": (81, 946, 1030),
}


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_evidence():
    records = {}
    with (SOURCE_ROOT / "source_map.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("lesson_number") in {x[0] for x in SOURCE_RANGES.values()}:
                records[record["record_id"]] = record
    sources = {}
    for key, (lesson, first, last) in SOURCE_RANGES.items():
        path = next(SOURCE_ROOT.glob(f"L{lesson:03d}_*.md"))
        lines = path.read_text(encoding="utf-8").splitlines()
        items, current_id = [], None
        for number, line in enumerate(lines, 1):
            match = re.search(r'"record_id":"([^"]+)"', line)
            if match:
                current_id = match.group(1)
            elif first <= number <= last and line.strip() and current_id:
                origin = records[current_id]
                items.append({"line": number, "text": line, "record_id": current_id,
                              "role": origin["source_role"], "page": origin["page_number"]})
        sources[key] = {"path": str(path), "first_line": first, "last_line": last,
                        "sha256": digest(path), "items": items}
    return sources


def chart_frame(snapshot):
    data = snapshot["data"]
    assert data["bar_time_label"] == "end"
    frame = pd.DataFrame({"date": pd.to_datetime(data["t"], unit="s", utc=True).tz_convert("America/New_York"),
                          **{name: data[short] for name, short in
                             [("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v")]}})
    frame["code"] = "QQQ.US"
    frame.attrs = {**data["price_basis"], "bar_time_label": "end"}
    return frame


def full_frame(frame, opening):
    # The producing adapter declares start labels. The captured chart explicitly
    # uses end labels; compare OHLCV before adding any missing opening bars.
    opening = opening.copy()
    opening.date = opening.date.dt.tz_convert("America/New_York") + pd.Timedelta(minutes=1)
    joined = frame.merge(opening, on="date", suffixes=("_chart", "_provider"))
    assert len(joined) > 0
    assert all((joined[k + "_chart"] == joined[k + "_provider"]).all()
               for k in ["open", "high", "low", "close", "volume"])
    assert opening.attrs["price_basis_revision"] == frame.attrs["price_basis_revision"]
    extra = opening.loc[opening.date < frame.date.iloc[0], frame.columns]
    result = pd.concat([extra, frame], ignore_index=True)
    result.attrs = dict(frame.attrs)
    return result, {"added_opening_bars": len(extra), "overlap_bars": len(joined),
                    "ohlcv_overlap_exact": True, "provider_labels": "start", "chart_labels": "end"}


def make_cl(frame):
    config = {k: frame.attrs[k] for k in ("structure_price_quantum", "price_basis_revision") if k in frame.attrs}
    return CL("QQQ.US", "1m", config, market="us")


def ep(point):
    return {"time": int(point.k.date.timestamp()), "date": point.k.date.isoformat(), "price": point.val}


def line_signature(line):
    return tuple((int(p.k.date.timestamp()), p.val) for p in (line.start, line.end))


def normalize_features(pens, indices, initial):
    """Chronological interval folding, independent of production helpers.

    This deliberately applies L065/L067 without the production reference-pivot
    exclusion. A resulting local shape is diagnostic, not automatic authority
    to override L071/L079/L081 context.
    """
    result = []
    for index in indices:
        pen = pens[index]
        cur = {"low": pen.low, "high": pen.high, "sources": [index]}
        if result:
            old = result[-1]
            contained = ((old["low"] <= cur["low"] and old["high"] >= cur["high"])
                         or (cur["low"] <= old["low"] and cur["high"] >= old["high"]))
            if contained:
                direction = initial if len(result) == 1 else (
                    "up" if old["high"] > result[-2]["high"] else "down")
                choose = max if direction == "up" else min
                result[-1] = {"low": choose(old["low"], cur["low"]),
                              "high": choose(old["high"], cur["high"]),
                              "sources": old["sources"] + [index]}
                continue
        result.append(cur)
    return result


def overlap(a, b):
    return max(a["low"], b["low"]) <= min(a["high"], b["high"])


def segment_rows(cl, chart):
    pens, rows = cl.get_contiguous_bis(), []
    chart_keys = {tuple((p["time"], p["price"]) for p in x["points"]): i
                  for i, x in enumerate(chart["data"]["xds"])}
    for line in cl.get_xds():
        start, end = line.start_line.index, line.end_line.index
        proof = line.construction_evidence
        p = asdict(proof) if proof else None
        row = {"number": line.index + 1, "index": line.index,
               "chart_index": chart_keys.get(line_signature(line)),
               "start_bi": start, "end_bi": end, "pen_count": end - start + 1,
               "type": line.type, "start": ep(line.start), "end": ep(line.end),
               "high": line.high, "low": line.low, "done": line.done,
               "locked_at": line.locked_at.isoformat() if line.locked_at else None,
               "proof": p, "flags": [], "checks": {}}
        initial = pens[start:start + 3]
        row["first_three_overlap"] = [max(x.low for x in initial), min(x.high for x in initial)]
        row["checks"].update(
            odd_span=end - start >= 2 and (end - start) % 2 == 0,
            first_three_overlap=row["first_three_overlap"][0] <= row["first_three_overlap"][1],
            direction=(line.end.val > line.start.val) == (line.type == "up"),
            connected=(not rows or rows[-1]["end"] == row["start"]),
        )
        if p:
            left, middle, right = p["first_sequence"]
            row["standard_gap"] = not overlap(left, middle)
            row["raw_gap"] = not overlap(left, {"low": pens[end + 1].low, "high": pens[end + 1].high})
            row["witness"] = {"index": p["witness_index"], "end": ep(pens[p["witness_index"]].end)}
            row["dependencies_unlocked"] = [i for i in range(start, p["witness_index"] + 1)
                                              if pens[i].locked_at is None or pens[i].selection_pending]
            row["checks"]["middle_source_is_endpoint"] = (
                middle["high_source_index"] if line.type == "up" else middle["low_source_index"]) == end + 1
            row["checks"]["completed_dependencies"] = not line.done or not row["dependencies_unlocked"]
            strict_top = middle["high"] > max(left["high"], right["high"])
            strict_bottom = middle["low"] < min(left["low"], right["low"])
            row["checks"]["directional_fractal"] = strict_top if line.type == "up" else strict_bottom
            if p["parent_key"] is None and row["standard_gap"] != p["initial_gap"]:
                row["flags"].append("gap_classification_conflict")
            if p["rule"] == "first-pen-break":
                row["flags"].append("local_break_context")
            if p["second_sequence"] or p["parent_key"] is not None:
                row["flags"].append("second_sequence")
            raw_indices = list(range(start + 1, end + 1, 2))
            prefix = normalize_features(pens, raw_indices, line.type)
            row["chronological_prefix"] = prefix
            row["production_left"] = left
        if not line.done:
            row["flags"].append("pending")
        if (line.type == "down" and line.high > line.start.val) or (
                line.type == "up" and line.low < line.start.val):
            row["flags"].append("crosses_origin")
        directional_extreme = line.high if line.type == "up" else line.low
        row["endpoint_is_directional_extreme"] = line.end.val == directional_extreme
        assert all(row["checks"].values()), (row["number"], row["checks"])
        rows.append(row)
    return rows


def experiments(pens, production):
    from tests.core.test_segment_source_rules import CASES, strokes, geometry
    from tests.core.test_segment_reviewed_rules import REVIEWED

    code = (ROOT / "src/chanlun/core/xd_calculator.py").read_text(encoding="utf-8")
    for expression in ["has_gap = not _overlap(left, first)",
                       "has_gap = not _overlap(left, _bi_to_cs_elem(first))",
                       "if not advances:\n        return previous"]:
        assert code.count(expression) == 1, "production changed; review controlled edits before rerunning"
    variants = {
        "standard_gap": code.replace("has_gap = not _overlap(left, first)",
                                     "has_gap = not _overlap(elements[0], elements[1])").replace(
                                         "has_gap = not _overlap(left, _bi_to_cs_elem(first))",
                                         "has_gap = not _overlap(left, mid)"),
        "all_references": code.replace("if not advances:\n        return previous",
                                       "if not advances:\n        return following"),
        # Hypothesis: L078's protected strong first reversal and an ordinary
        # overlapping reversal need different treatment after inclusion. This
        # remains a deduction, not an author-specified Python condition.
        "strong_first_only": code.replace(
            "has_gap = not _overlap(left, _bi_to_cs_elem(first))",
            'has_gap = not _overlap(left, _bi_to_cs_elem(first) if '
            '(first.low < left["low"] if seg_type == "up" else first.high > left["high"]) else mid)',
        ).replace(
            "has_gap = not _overlap(left, first)",
            'has_gap = not _overlap(left, first if '
            '(first.low < left.low if seg_type == "up" else first.high > left.high) else elements[1])',
        ),
    }
    result = {}
    for name, source in variants.items():
        module = types.ModuleType("audit_" + name)
        sys.modules[module.__name__] = module
        exec(compile(source, module.__name__, "exec"), module.__dict__)
        calc = module.XdCalculator()
        calc.calculate(pens)
        differences = []
        for group, cases in [("source_figure_constraints", CASES), ("recorded_user_decisions", REVIEWED)]:
            for case, (points, expected) in cases.items():
                for mirror in (False, True):
                    got = geometry(module.XdCalculator().calculate(strokes(points, mirror)))
                    if got != expected:
                        differences.append({"group": group, "case": case, "mirror": mirror,
                                            "expected": expected, "actual": got})
        keys = {(x.start_line.index, x.end_line.index) for x in calc.xds}
        previous = {}
        replay_calc = module.XdCalculator()
        for stop in range(len(pens) + 1):
            replay_calc.calculate(pens[:stop])
            current = {(x.start_line.index, x.end_line.index, x.type): x.locked_at
                       for x in replay_calc.xds if x.done}
            assert all(current.get(k) == t for k, t in previous.items()), (name, stop)
            previous = current
        result[name] = {"approved": False, "implementation_sha256": hashlib.sha256(source.encode()).hexdigest(),
                        "fixed_pen_prefixes_stable": len(pens) + 1,
                        "segments": [{"start_bi": x.start_line.index, "end_bi": x.end_line.index,
                                      "start": ep(x.start), "end": ep(x.end), "done": x.done,
                                      "rule": x.construction_evidence.rule if x.construction_evidence else None}
                                     for x in calc.xds],
                        "changed_production_segments": [x.index + 1 for x in production
                                                         if (x.start_line.index, x.end_line.index) not in keys],
                        "constraint_disagreements": differences}
    return result


def replay(frame, expected, output):
    started = perf_counter()
    running, previous, prior_pens = make_cl(frame), {}, None
    result = {"mode": "each source bar observed; latest bar unclosed, previous closed by next observation",
              "bars": 0, "changed_pen_states": 0, "confirmed_boundary_revocations": 0,
              "late_confirmations": [], "status": "running"}
    try:
        for index, row in enumerate(frame.itertuples(index=False), 1):
            running.process_validated_incremental_klines(frame.iloc[index-1:index], last_bar_closed=False)
            current = physical(running.get_xds())
            assert all(current.get(key) == when for key, when in previous.items()), ("revoked", index)
            assert all(when == row.date for key, when in current.items() if key not in previous), ("backdated", index)
            pens = running.get_contiguous_bis()
            state = pen_state(pens)
            if state != prior_pens:
                check_structure(running.xd_calculator, pens)
                fresh_matches(running.xd_calculator, pens)
                result["changed_pen_states"] += 1
                prior_pens = state
            result["bars"] = index
            previous = current
            if index % 1000 == 0:
                result["seconds"] = round(perf_counter() - started, 2)
                dump(output / "replay.json", result)
                print("replay", index, result["changed_pen_states"], result["seconds"], flush=True)
        assert pen_state(running.get_contiguous_bis()) == pen_state(expected.get_contiguous_bis())
        assert [line_signature(x) for x in running.get_xds()] == [line_signature(x) for x in expected.get_xds()]
        assert running.xd_calculator.evidence == expected.xd_calculator.evidence
        assert physical(running.get_xds()) == physical(expected.get_xds())
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = repr(exc)
        raise
    finally:
        result["seconds"] = round(perf_counter() - started, 2)
        dump(output / "replay.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    snapshot = json.loads((output / "chart_snapshot.json").read_text(encoding="utf-8"))
    frame = chart_frame(snapshot)
    chart_cl = make_cl(frame)
    chart_cl.process_klines(frame, last_bar_closed=False)
    for kind, lines in [("bis", chart_cl.get_contiguous_bis()), ("xds", chart_cl.get_xds())]:
        assert [line_signature(x) for x in lines] == [tuple((p["time"], p["price"]) for p in x["points"])
                                                     for x in snapshot["data"][kind]]
    opening = pd.read_parquet(output / "QQQ.US_1m_aug18_open.parquet")
    frame, provenance = full_frame(frame, opening)
    assert frame.date.is_unique and frame.date.is_monotonic_increasing
    assert all((frame.low <= frame[k]).all() and (frame[k] <= frame.high).all() for k in ["open", "close"])
    frame.to_parquet(output / "QQQ.US_1m_full_aug18.parquet", index=False)
    cl = make_cl(frame)
    cl.process_klines(frame, last_bar_closed=False)
    pens = cl.get_contiguous_bis()
    check_structure(cl.xd_calculator, pens)
    fresh_matches(cl.xd_calculator, pens)
    fixed = fixed_prefixes(pens)
    counts = Counter()
    audit = SearchAuditCalculator(counts)
    audit.calculate(pens)
    rows = segment_rows(cl, snapshot)
    assert (len(frame), len(pens), len(rows)) == (8287, 510, 58), "frozen review scope changed"
    data = {
        "scope": {"symbol": "QQQ.US", "frequency": "1m", "start": frame.date.iloc[0].isoformat(),
                  "end": frame.date.iloc[-1].isoformat(), "bars": len(frame), "pens": len(pens),
                  "segments": len(rows), "confirmed": sum(x.done for x in cl.get_xds()),
                  "chart_segments": len(chart_cl.get_xds()), "time_label": "end",
                  "data_timezone": "America/New_York", "snapshot_validated_at": snapshot["validated_at"],
                  "closed_through": cl.get_stroke_construction_state()["closed_through"],
                  "price_basis": frame.attrs, **provenance},
        "checks": {"chart_geometry_matches": True, "structure_checker": "passed",
                   "fixed_pen_prefixes": fixed, "candidate_scan": dict(counts)},
        "hashes": {str(p.relative_to(ROOT)): digest(p) for p in [
            ROOT / "src/chanlun/core/xd_calculator.py", ROOT / "src/chanlun/core/bi_calculator.py",
            ROOT / "src/chanlun/core/strict_structure/base_profile.py", Path(__file__).resolve(),
            output / "chart_snapshot.pkl", output / "QQQ.US_1m_aug18_open.parquet",
            output / "QQQ.US_1m_full_aug18.parquet"]},
        "segments": rows,
        "pens": [{"index": b.index, "start": ep(b.start), "end": ep(b.end), "type": b.type,
                  "low": b.low, "high": b.high, "locked_at": b.locked_at.isoformat() if b.locked_at else None,
                  "selection_pending": b.selection_pending} for b in pens],
        "bars": [{"time": int(r.date.timestamp()), "open": r.open, "high": r.high,
                  "low": r.low, "close": r.close, "volume": r.volume} for r in frame.itertuples(index=False)],
        "sources": source_evidence(),
        "experiments": experiments(pens, cl.get_xds()),
        "tail": asdict(cl.xd_calculator.tail_state),
    }
    dump(output / "audit.json", data)
    print(json.dumps({"scope": data["scope"], "checks": data["checks"],
                      "flags": {r["number"]: r["flags"] for r in rows if r["flags"]}},
                     ensure_ascii=False, default=str), flush=True)
    if args.replay:
        replay(frame, cl, output)


if __name__ == "__main__":
    main()
