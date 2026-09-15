"""Measure confirmation retraction after an arbitrarily long valid prefix.

This is a diagnostic for the current implementation, not an additional Chan
rule. The graph checks independently enumerate the current local necessary
conditions; they do not establish which strokes the original text completes.
An exit code of zero means the checks ran, not that completion is correct.
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from script.probe_old_stroke_cases import advance, bars, cold, compare
from script.reproduce_old_stroke_anomalies import DISCONNECTED_CONTINUATION
from chanlun.core.strict_structure.base_profile import (
    STRICT_STROKE_MODE, strict_base_config_revision,
)


CYCLE = [(19, 15), (20, 16), (18, 14), (16, 12),
         (14, 10), (12, 8), (14, 10), (16, 12)]


def family_prices(cycles, mirror=False):
    if cycles < 0:
        raise ValueError("cycles must be non-negative")
    tail = list(DISCONNECTED_CONTINUATION)
    tail[10], tail[12], tail[14] = (20, 9), (22, 8), (22, 14)
    prices = CYCLE * cycles + tail
    return [(100 - low, 100 - high) for high, low in prices] if mirror else prices


def independent_graph(prices, old_last_center):
    """Use only raw high/low pairs, never the production resolver or rules."""
    gaps, inclusions, points = [], [], []
    for index, (previous, current) in enumerate(zip(prices, prices[1:]), 1):
        ph, pl = previous
        ch, cl = current
        if max(pl, cl) > min(ph, ch):
            gaps.append((index - 1, index))
        if (ph >= ch and pl <= cl) or (ch >= ph and cl <= pl):
            inclusions.append((index - 1, index))
    if gaps or inclusions:
        raise ValueError("the diagnostic family must have no gaps or inclusion")
    for index in range(1, len(prices) - 1):
        lh, ll = prices[index - 1]
        high, low = prices[index]
        rh, rl = prices[index + 1]
        if high > max(lh, rh) and low > max(ll, rl):
            points.append((index, "ding", high))
        elif high < min(lh, rh) and low < min(ll, rl):
            points.append((index, "di", low))
    edges, suffix_checks = [], []
    checked = 0
    for offset, first in enumerate(points):
        for second in points[offset + 1:]:
            if first[1] == second[1]:
                continue
            start, end = first[0], second[0]
            top, bottom = (first, second) if first[1] == "ding" else (second, first)
            high, low = prices[top[0]], prices[bottom[0]]
            interval = prices[start:end + 1]
            conditions = dict(
                independent_bar=end - start >= 4,
                endpoint_order=high[0] > low[0] and high[1] > low[1],
                endpoint_extremes=(max(p[0] for p in interval) == top[2]
                                   and min(p[1] for p in interval) == bottom[2]),
            )
            checked += 1
            if end > old_last_center:
                suffix_checks.append(dict(start=start, end=end, **conditions))
            if all(conditions.values()):
                edges.append((start, end))
    reachable = {points[0][0]}
    for start, end in edges:
        if start in reachable:
            reachable.add(end)
    return dict(
        input_gaps=gaps, input_inclusions=inclusions, fractals=points,
        opposite_pairs_checked=checked, suffix_pair_checks=suffix_checks,
        locally_admissible_edges=edges, reachable_from_first=sorted(reachable),
        last_fractal_reachable_from_first=points[-1][0] in reachable,
    )


def snapshot(calc, count):
    return dict(
        raw_prefix=count,
        strokes=[dict(start=b.start.k.index, end=b.end.k.index,
                      start_price=b.start.val, end_price=b.end.val,
                      done=b.is_done(), locked_at=b.locked_at,
                      fractal_visible_at=b.fractal_visible_at,
                      selected_at=b.selected_at) for b in calc.bis],
        decision=asdict(calc._resolver.last_decision),
        range_violations=[asdict(x) for x in calc.audit_endpoint_ranges()],
        adjacency_violations=[asdict(x) for x in calc.audit_endpoint_adjacency()],
    )


def run_case(cycles, mirror):
    prices = family_prices(cycles, mirror)
    raw = bars(prices)
    before, middle, final = (8 * cycles + n for n in (11, 15, 19))
    graph = independent_graph(prices, before - 2)
    merged, calc = cold([])
    records, replay_errors, removal_events = [], [], []
    previous_confirmed = set()
    for count in range(1, len(raw) + 1):
        advance(merged, calc, raw[:count])
        current = {(b.start.k.index, b.end.k.index) for b in calc.bis}
        removed = previous_confirmed - current
        if removed:
            removal_events.append(dict(raw_prefix=count, removed_confirmed=sorted(removed)))
        previous_confirmed = {(b.start.k.index, b.end.k.index) for b in calc.confirmed_bis}
        if count >= before:
            error = compare(raw[:count], merged, calc)
            if error:
                replay_errors.append(dict(raw_prefix=count, failure=error))
        if count in (before, middle, final):
            records.append(snapshot(calc, count))
    old = records[0]["strokes"]
    old_done = {(b["start"], b["end"]) for b in old if b["done"]}
    now = {(b["start"], b["end"]) for b in records[-1]["strokes"]}
    return dict(
        cycles=cycles, mirror=mirror, prices=prices,
        raw_bars=len(raw), merged_bars=len(merged.cl_klines),
        initial_stroke_count=len(old), initial_confirmed_count=len(old_done),
        first_stroke_successors_before_extension=max(0, len(old) - 1),
        appended_bars=final - before,
        removed_initial_confirmed=sorted(old_done - now), removal_events=removal_events,
        replay_checks=final - before + 1, replay_errors=replay_errors,
        all_observed_static_checks_clean=all(
            not x["range_violations"] and not x["adjacency_violations"] for x in records
        ),
        independent_graph=graph, snapshots=records,
    )


def draw_case(path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    prices = family_prices(0)
    labels = [("P", 1, 20), ("A", 5, 8), ("B", 9, 22),
              ("C", 11, 6), ("D", 13, 24), ("E", 17, 4)]
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, sharey=True)
    for ax, count in zip(axes, (11, 19)):
        for index, (high, low) in enumerate(prices[:count]):
            ax.vlines(index, low, high, colors="#bdc5cc", linewidth=2)
            ax.hlines((low, high), index - .12, index + .12,
                      colors="#bdc5cc", linewidth=1)
        if count == 19:
            ax.plot([1, 5, 9], [20, 8, 22], "o--", color="#ba4639",
                    alpha=.7, label="此前输出，现已被撤回")
        _, calc = cold(bars(prices[:count]))
        for offset, bi in enumerate(calc.bis):
            ax.plot([bi.start.k.index, bi.end.k.index], [bi.start.val, bi.end.val],
                    "o-" if bi.is_done() else "o--", linewidth=2.7,
                    color="#146e5e" if bi.is_done() else "#276ba3",
                    label=("程序标记完成" if bi.is_done() else "当前待定笔"))
        for name, x, y in labels:
            if x + 1 < count:
                ax.annotate(f"{name}{x}", (x, y), ha="center",
                            xytext=(0, 9 if y >= 20 else -18), textcoords="offset points")
        ax.set_title(f"输入 {count} 根 K 线时的实际输出", loc="left")
        ax.set_ylabel("价格")
        ax.grid(axis="y", alpha=.18)
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1))
    axes[-1].set_xlabel("K 线序号（从 0 开始；无包含、无跳空）")
    axes[-1].set_xticks(range(len(prices)))
    axes[-1].set_ylim(0, 28)
    fig.suptitle("当前 v9 的完成状态反例：只追加 8 根 K 线，P→A 从已完成变为撤回")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, nargs="+", default=[0, 1, 2, 4, 8, 16, 32, 64])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figure", type=Path)
    args = parser.parse_args()
    if any(cycles < 0 for cycles in args.cycles):
        parser.error("--cycles must be non-negative")
    cases = [run_case(cycles, mirror) for cycles in args.cycles for mirror in (False, True)]
    report = dict(
        stroke_profile=STRICT_STROKE_MODE, profile_revision=strict_base_config_revision(),
        interpretation="Diagnostic of current predicates and completion state; not an original-rule oracle",
        incremental_prefixes=sum(x["raw_bars"] for x in cases),
        cold_comparisons=sum(x["replay_checks"] for x in cases),
        cases=cases,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    if args.figure:
        args.figure.parent.mkdir(parents=True, exist_ok=True)
        draw_case(args.figure)
    for item in cases:
        print(f"cycles={item['cycles']} mirror={item['mirror']} "
              f"successors={item['first_stroke_successors_before_extension']} "
              f"removed={len(item['removed_initial_confirmed'])} "
              f"replay_errors={len(item['replay_errors'])}")
    if any(x["replay_errors"] for x in cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
