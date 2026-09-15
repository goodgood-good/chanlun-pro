"""Render the recorded short-reversal case, without modifying market inputs."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from review_stroke_real_markets_v12 import bars, cold, bi_record, save


def main():
    output = ROOT / "output/bi_code_review/v13_validation"
    frame = pd.read_parquet(ROOT / "tests/fixtures/stroke_v13/SH.600519_5m.parquet")
    old = json.loads(
        (
            ROOT
            / "output/bi_code_review/v12_real_market_audit/cases/recorded_SH.600519_5m_retraction.json"
        ).read_text(encoding="utf-8")
    )
    _, calc = cold(bars(frame))
    current = [bi_record(b) for b in calc.bis]
    save(
        output / "short_reversal_case.json",
        {
            "symbol": "SH.600519",
            "frequency": "5m",
            "raw_count": len(frame),
            "v12": old["after"],
            "v13": {"strokes": current, "construction": calc.construction_state()},
            "meaning": "Recorded market regression, not an original Chan lesson figure or a final whole-chart oracle.",
        },
    )
    plt.rcParams.update(
        {
            "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "font.size": 10,
        }
    )
    figure, axes = plt.subplots(
        2, 1, figsize=(13, 8), sharex=True, sharey=True, layout="constrained"
    )
    for axis, label, values in zip(
        axes,
        ["v12：间接前驱绕过检查，A、B 被回撤", "v13：保留 A、B，C→D 作为独立待定连接"],
        [old["after"]["strokes"], current],
    ):
        axis.set_title(label, loc="left", weight="bold")
        for index in range(75, len(frame)):
            row = frame.iloc[index]
            axis.vlines(index, row.low, row.high, color="#b7bdc7", linewidth=1)
            axis.plot(
                [index - 0.13, index + 0.13],
                [row.close] * 2,
                color="#7a8390",
                linewidth=1,
            )
        for bi in values:
            a, b = bi["start"], bi["end"]
            if b["raw_center"] < 75:
                continue
            pending = bi["selection_pending"] or not bi["is_done"]
            axis.plot(
                [a["raw_center"], b["raw_center"]],
                [a["value"], b["value"]],
                color="#d67825" if pending else "#1767a5",
                linestyle="--" if pending else "-",
                linewidth=2.4,
                marker="o",
                markersize=4,
            )
        for name, index, price in [
            ("P", 78, 1437.15),
            ("A", 85, 1446.41),
            ("B", 93, 1438.88),
            ("C", 96, 1450.67),
            ("D", 104, 1431.28),
        ]:
            axis.annotate(
                f"{name}  {price:.2f}",
                (index, price),
                xytext=(0, 11 if name in ("A", "C") else -19),
                textcoords="offset points",
                ha="center",
                fontsize=9,
            )
        axis.axvspan(93, 96, color="#dfe4eb", alpha=0.45)
        axis.set_xlim(75, 106)
        axis.set_ylim(1427, 1455)
        axis.grid(axis="y", alpha=0.15)
        axis.set_ylabel("价格")
    ticks = [75, 78, 85, 93, 96, 104]
    axes[-1].set_xticks(
        ticks, [frame.iloc[i].date.strftime("%m-%d\n%H:%M") for i in ticks]
    )
    figure.suptitle("贵州茅台 5 分钟 · 同一份 106 根历史行情快照", weight="bold")
    axes[-1].set_xlabel(
        "蓝色实线：已有合格后继的当前连接；橙色虚线：尾笔或连接待定。灰色区间没有补画一笔。"
    )
    for suffix in ("png", "svg"):
        figure.savefig(output / f"short_reversal_comparison.{suffix}", dpi=160)
    plt.close(figure)
    print(output / "short_reversal_comparison.png")


if __name__ == "__main__":
    main()
