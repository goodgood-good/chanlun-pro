"""Reproduce endpoint replacement and track physical confirmation evidence.

The output records observations, without changing or accepting the production rules.
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from script.probe_old_stroke_cases import bars, cold
from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import (
    STRICT_STROKE_MODE, strict_base_config, strict_base_config_revision,
)
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.unit_adapter import adapt_lines


PROTECTED_REVERSAL = [
    (12, 8), (10, 6), (12, 8), (14, 10), (16, 12), (20, 16),
    (18, 14), (17, 13), (16, 12), (14, 10), (15, 11), (16, 12),
    (17, 13), (18, 14), (13, 9), (8, 4), (10, 6), (12, 8),
    (14, 10), (16, 12), (14, 10),
]

DISCONNECTED_CONTINUATION = [
    (19, 15), (20, 16), (18, 14), (16, 12), (14, 10), (12, 8),
    (14, 10), (16, 12), (18, 14), (22, 18), (15, 9), (10, 6),
    (16, 12), (24, 20), (18, 14), (14, 10), (10, 6), (8, 4), (10, 6),
]


def protected_reversal(mirror=False):
    prices = [(100 - low, 100 - high) for high, low in PROTECTED_REVERSAL] if mirror else PROTECTED_REVERSAL
    records, states = [], []
    for count in (15, 17, 21):
        _, calc = cold(bars(prices[:count]))
        records.append(dict(
            raw_prefix=count,
            strokes=[dict(start=b.start.k.index, end=b.end.k.index,
                          start_price=b.start.val, end_price=b.end.val,
                          done=b.is_done(), locked_at=b.locked_at) for b in calc.bis],
            decision=asdict(calc._resolver.last_decision),
            range_violations=[asdict(x) for x in calc.audit_endpoint_ranges()],
            adjacency_violations=[asdict(x) for x in calc.audit_endpoint_adjacency()],
        ))
        states.append({(b.start.k.index, b.end.k.index) for b in calc.bis})
    disappeared = (5, 9) in states[1] and (5, 9) not in states[2]
    static_clean = not records[-1]["range_violations"] and not records[-1]["adjacency_violations"]
    return dict(mirror=mirror, prices=prices, snapshots=records,
                protected_reversal_disappeared=disappeared,
                long_edge_selected=(5, 15) in states[2],
                static_checks_clean=static_clean,
                static_audits_missed=disappeared and static_clean)


def disconnected_continuation(mirror=False, without_gaps=False):
    """Enumerate local necessary conditions independently of the selector.

    The enumeration proves a limitation of the implemented conditions, not a
    new source rule or a proof that a locally admissible edge is a finished pen.
    """
    prices = list(DISCONNECTED_CONTINUATION)
    if without_gaps:
        prices[10], prices[12], prices[14] = (20, 9), (22, 8), (22, 14)
    if mirror:
        prices = [(100 - low, 100 - high) for high, low in prices]
    raw = bars(prices)
    gaps = [(i - 1, i) for i in range(1, len(raw))
            if max(raw[i - 1].l, raw[i].l) > min(raw[i - 1].h, raw[i].h)]
    if without_gaps:
        assert not gaps
    merged, _ = cold(raw)
    assert len(merged.cl_klines) == len(raw)
    points = []
    for index in range(1, len(raw) - 1):
        left, center, right = raw[index - 1:index + 2]
        if center.h > max(left.h, right.h) and center.l > max(left.l, right.l):
            points.append((index, "ding", center.h))
        elif center.h < min(left.h, right.h) and center.l < min(left.l, right.l):
            points.append((index, "di", center.l))
    edges, checks = [], []
    for offset, first in enumerate(points):
        for second in points[offset + 1:]:
            start, end = first[0], second[0]
            if first[1] == second[1]:
                continue
            top, bottom = (first, second) if first[1] == "ding" else (second, first)
            interval = raw[start:end + 1]
            conditions = dict(
                independent_bar=end - start >= 4,
                endpoint_order=(raw[top[0]].h > raw[bottom[0]].h
                                and raw[top[0]].l > raw[bottom[0]].l),
                endpoint_extremes=(max(k.h for k in interval) == top[2]
                                   and min(k.l for k in interval) == bottom[2]),
            )
            checks.append(dict(start=start, end=end, **conditions))
            if all(conditions.values()):
                edges.append((start, end))
    # Enumerating a six-fractal example needs no reachability information from
    # StrokeResolver and cannot inherit its choice of parent or origin.
    reachable = {points[0][0]}
    for start, end in edges:
        if start in reachable:
            reachable.add(end)
    snapshots = []
    for count in (11, 15, 19):
        _, calc = cold(raw[:count])
        snapshots.append(dict(
            raw_prefix=count,
            strokes=[dict(start=b.start.k.index, end=b.end.k.index,
                          done=b.is_done(), locked_at=b.locked_at) for b in calc.bis],
            decision=asdict(calc._resolver.last_decision),
        ))
    old_done = {(b["start"], b["end"]) for b in snapshots[0]["strokes"] if b["done"]}
    now = {(b["start"], b["end"]) for b in snapshots[-1]["strokes"]}
    return dict(
        mirror=mirror, without_gaps=without_gaps, input_gaps=gaps,
        raw_bars=len(raw), merged_bars=len(merged.cl_klines),
        prices=prices, fractals=points, local_edge_checks=checks,
        locally_admissible_edges=edges, reachable_from_first=sorted(reachable),
        last_fractal_reachable_from_first=points[-1][0] in reachable,
        removed_confirmed=sorted(old_done - now), snapshots=snapshots,
    )


def unit_identity():
    path = ROOT / "tests/fixtures/QQQ.US_30m.parquet"
    frame = pd.read_parquet(path)
    config = dict(strict_base_config(), structure_price_quantum="0.01",
                  price_basis_revision="probe-raw", strict_config_revision="probe-v7")
    live = CL("QQQ.US", "30m", config, market="us")
    snapshots = []
    for count in (281, 282):
        live.process_klines(frame.head(count))
        units = adapt_lines(live.get_xds(), 0, SourceKind.SEGMENT,
                            live._strict_price_quantum(), live._strict_as_of(),
                            live._strict_registry(), constituent_lines=live.get_bis())
        xd, unit = next((xd, unit) for xd, unit in zip(live.get_xds(), units)
                        if (xd.start.k.k_index, xd.end.k.k_index) == (14, 85))
        snapshots.append(dict(raw_prefix=count, unit_id=unit.unit_id,
                              start=xd.start.k.k_index, end=xd.end.k.k_index,
                              done=xd.is_done(), locked_at=xd.locked_at,
                              available_at=unit.available_at))
    batch = CL("QQQ.US", "30m", config, market="us")
    batch.process_klines(frame.head(282))
    batch_line = next(x for x in batch.get_xds()
                      if (x.start.k.k_index, x.end.k.k_index) == (14, 85))
    registry = live._strict_registry()
    return dict(input=str(path), snapshots=snapshots,
                identity_unchanged=snapshots[0]["unit_id"] == snapshots[1]["unit_id"],
                confirmation_changed=snapshots[0]["locked_at"] != snapshots[1]["locked_at"],
                both_confirmation_records_preserved=all(
                    registry._confirmed_at.get(item["unit_id"]) == item["locked_at"]
                    for item in snapshots
                ),
                later_time_matches_cold=snapshots[1]["locked_at"] == batch_line.locked_at)


def market_segment_revision():
    path = ROOT / "tests/fixtures/SH.600519_5m.parquet"
    frame = pd.read_parquet(path).head(8909)
    config = dict(strict_base_config(), structure_price_quantum="0.01",
                  price_basis_revision="probe-raw", strict_config_revision="probe-v7")
    live = CL("SH.600519", "5m", config, market="a")
    snapshots, confirmed, removals = [], [], []
    for count in range(8896, 8910):
        live.process_klines(frame.head(count))
        lines = [dict(start=x.start.k.k_index, end=x.end.k.k_index,
                      start_price=x.start.val, end_price=x.end.val,
                      done=x.is_done(), locked_at=x.locked_at)
                 for x in live.get_xds() if x.end.k.k_index >= 8300]
        snapshots.append(dict(raw_prefix=count, observed_at=frame.iloc[count - 1].date,
                              stroke_count=len(live.get_bis()), segments=lines))
        confirmed.append({(x["start"], x["end"]): x for x in lines if x["done"]})
        if len(confirmed) > 1:
            removed = [x for key, x in confirmed[-2].items() if key not in confirmed[-1]]
            if removed:
                removals.append(dict(raw_prefix=count, removed=removed))
    removed = [x for key, x in confirmed[0].items() if key not in confirmed[-1]]
    original_update_removed = [x for key, x in confirmed[0].items() if key not in confirmed[1]]
    batch = CL("SH.600519", "5m", config, market="a")
    batch.process_klines(frame)
    signature = lambda cd: [(x.start.k.k_index, x.end.k.k_index, x.locked_at)
                            for x in cd.get_xds()]
    return dict(input=str(path), snapshots=snapshots, removed_confirmed_segments=removed,
                removed_at_original_update=original_update_removed,
                removal_events=removals,
                later_output_matches_cold=signature(live) == signature(batch))


def draw_case(path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, sharey=True)
    for ax, count in zip(axes, (17, 21)):
        _, calc = cold(bars(PROTECTED_REVERSAL[:count]))
        for i, (hi, lo) in enumerate(PROTECTED_REVERSAL[:count]):
            ax.vlines(i, lo, hi, colors="#aaaaaa", linewidth=2)
            ax.hlines((hi, lo), i - 0.1, i + 0.1, colors="#aaaaaa", linewidth=1)
        if count == 21:
            ax.plot([1, 5, 9, 13], [6, 20, 10, 18], "--", color="#8c8c8c",
                    linewidth=2, label="Previously retained path")
        points = [calc.bis[0].start] + [b.end for b in calc.bis]
        ax.plot([p.k.index for p in points], [p.val for p in points], "o-",
                color="#2779ad" if count == 17 else "#b9382b", linewidth=2.2,
                label="Current output")
        for label, x, y in [("A1", 1, 6), ("B5", 5, 20), ("C9", 9, 10),
                            ("D13", 13, 18), ("E15", 15, 4), ("F19", 19, 16)]:
            if x < count:
                ax.annotate(label, (x, y), xytext=(0, 8 if x in (5, 13, 19) else -16),
                            textcoords="offset points", ha="center", fontsize=10)
        names = {1: "A", 5: "B", 9: "C", 13: "D", 15: "E", 19: "F"}
        selected_path = "-".join(f"{names.get(p.k.index, '')}{p.k.index}" for p in points)
        ax.set_title(f"{count} bars: selected path {selected_path}", loc="left")
        ax.set_ylabel("Price")
        ax.grid(axis="y", alpha=0.2)
        ax.legend(loc="upper right", fontsize=9)
    axes[-1].set_xlabel("Merged K-line center (zero-based; no inclusion in this example)")
    axes[-1].set_xticks(range(21))
    axes[-1].set_ylim(1, 23)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figure", type=Path)
    args = parser.parse_args()
    report = dict(stroke_profile=STRICT_STROKE_MODE,
                  profile_revision=strict_base_config_revision(),
                  protected_reversal=[protected_reversal(False), protected_reversal(True)],
                  disconnected_continuation=[disconnected_continuation(mirror, without_gaps)
                                             for without_gaps in (False, True)
                                             for mirror in (False, True)],
                  unit_identity=unit_identity(),
                  market_segment_revision=market_segment_revision())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    if args.figure:
        args.figure.parent.mkdir(parents=True, exist_ok=True)
        draw_case(args.figure)
    for item in report["protected_reversal"]:
        print(f"mirror={item['mirror']} protected_reversal_disappeared={item['protected_reversal_disappeared']} "
              f"static_audits_missed={item['static_audits_missed']}")
    print(f"identity_unchanged={report['unit_identity']['identity_unchanged']} "
          f"confirmation_changed={report['unit_identity']['confirmation_changed']}")
    print(f"removed_confirmed_market_segments="
          f"{len(report['market_segment_revision']['removed_confirmed_segments'])}")


if __name__ == "__main__":
    main()
