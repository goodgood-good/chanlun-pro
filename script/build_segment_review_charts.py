"""Build offline review charts from local primary text and controlled examples.

This only produces research artifacts. It does not change production rules.
All price chains are synthetic; source excerpts and images retain their origin.
"""

from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from chanlun.core.xd_calculator import XdCalculator
from tests.core.test_segment_source_rules import geometry, strokes

SOURCE = Path("D:/缠论/chanlun_lesson_corpus")
DEST = Path("D:/缠论/研究输出/线段规则争议图解")


def compute(points, calculator=XdCalculator):
    current = calculator()
    segments = geometry(current.calculate(strokes(points)))
    mirrored = calculator()
    assert geometry(mirrored.calculate(strokes(points, True))) == segments
    return {"segments": segments, "reason": current.tail_state.reason,
            "confirmed": sum(bool(s[2]) for s in segments)}


def source_block(lesson, spans, title):
    path = next(SOURCE.glob(f"L{lesson:03}_*.md"))
    lines = path.read_text(encoding="utf-8").splitlines()
    selected = []
    for start, end in spans:
        for n in range(start, end + 1):
            text = lines[n - 1].strip()
            if text and not text.startswith(("<!--", "![", "（图", "(图")):
                selected.append({"line": n, "text": text})
    return {"lesson": lesson, "title": title, "path": str(path),
            "uri": path.as_uri(), "spans": spans, "lines": selected,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def build_data():
    before = json.loads((ROOT / "docs/segment_review_before_20260917.json").read_text(encoding="utf-8"))
    data = {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "scope": "合成笔链；排除行情跳空；保留特征序列缺口", "cases": before["cases"],
            "before_implementation_sha256": before["implementation_sha256"]}
    data["review_decisions"] = {
        "date": "2026-09-17", "source": "用户在对话中对原图页的裁定，不是新增原文引语",
        "r1": "采用 B：按原始首笔与有效左参考判类。",
        "r2": "到 P11 采用 A；到 P14 划为 P0—P3、P3—P6、P6—P11、P11—P14。",
        "r3": "P5=13、15 时保留一致结果；P5=14 时为 P0—P5 未完成段。",
        "r4": "参考与候选资格没有问题，保持现有规则。",
        "r5": "包含方向没有问题，保持现有规则。",
    }
    for key in ("r1", "r2", "r3", "r4"):
        for entry in data["cases"][key].values():
            entry["approved"] = compute(entry["points"])
            if key == "r4":
                assert [list(s) for s in entry["approved"]["segments"]] == entry["a"]["segments"]
    assert data["cases"]["r1"]["11_0"]["approved"]["segments"] == [(0, 3, True), (3, 8, False)]
    assert data["cases"]["r2"]["11"]["approved"]["segments"] == [(0, 3, True), (3, 6, True), (6, 11, False)]
    assert data["cases"]["r2"]["14"]["approved"]["segments"] == [(0, 3, True), (3, 6, True), (6, 11, True), (11, 14, False)]
    assert data["cases"]["r3"]["14"]["approved"]["segments"] == [(0, 5, False)]
    sources = {
        "standard": source_block(67, [(49, 76), (85, 118)], "标准元素、两种结束方式"),
        "first": source_block(71, [(100, 124), (184, 208)], "首笔破坏、判类与边界包含"),
        "equal": source_block(75, [(418, 421)], "作者答复：一端相同仍包含"),
        "pen": source_block(77, [(247, 259), (271, 283)], "这里的“最先一个”属于笔的划分"),
        "three": source_block(80, [(1708, 1708)], "作者答复：分型至少需要三个元素"),
        "reference": source_block(81, [(958, 964), (988, 991), (1018, 1024)], "作者答复：选 1，不选较低的 3"),
        "fig79": source_block(79, [(247, 274), (286, 295)], "作者正文：34、56 合为 36；上图与下图"),
        "inclusion": source_block(65, [(34, 64), (82, 100)], "顺序包含、上下方向与前项"),
    }
    data["sources"] = sources
    data["images"] = {}
    for key, name in {
        "upper79": "1ef1c665acca7500a3f7432868e0436059731f24110bd97170fac3af97ac7b30.jpg",
        "lower79": "784c099fdfc87cc5d30e0a036caf5c112c37ff934aae563dabca026cfc8fa6f9.jpg",
    }.items():
        path = SOURCE / "images" / name
        data["images"][key] = {"path": str(path), "uri": path.as_uri(),
            "data": "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}
    names = ["src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
             "src/chanlun/core/strict_structure/base_profile.py", "tests/core/test_segment_reviewed_rules.py"]
    data["implementation_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    return data


def make_print_charts(data):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    font = FontProperties(fname="C:/Windows/Fonts/msyh.ttc")
    matplotlib.rcParams.update({"font.family": font.get_name(), "axes.unicode_minus": False,
                               "font.size": 11, "savefig.facecolor": "#ffffff"})
    blue, amber, ink, gray, green = "#225bd8", "#b66b0e", "#23334a", "#9da8b6", "#176654"

    def source_footer(fig, keys):
        lines = []
        for key in keys:
            item = data["sources"][key]
            spans = "、".join(str(a) if a == b else f"{a}—{b}" for a, b in item["spans"])
            lines.append(f"{item['path']}  ·  第 {spans} 行")
        lines.append("逐行原文、原图与可切换变体：同目录《线段规则争议图解.html》。本图数值为合成示例。")
        fig.text(.055, .092 if len(keys) == 3 else .067, "\n".join(lines),
                 fontsize=9, color="#64748b", linespacing=1.5)

    def plot(ax, points, segments=(), color=blue, marks=(), title="", note=""):
        ax.set_title(title, loc="left", fontsize=13, fontweight="bold", pad=21, color=ink)
        ax.plot(range(len(points)), points, color=gray, linewidth=1.6, marker="o", markersize=3, zorder=2)
        spread = max(points) - min(points)
        ax.set_ylim(min(points) - spread * .24, max(points) + spread * .31)
        ax.set_xlim(-.55, len(points) - .45)
        for start, end, done in segments:
            ax.plot([start, end], [points[start], points[end]], color=color,
                    linewidth=3, linestyle="-" if done else (0, (4, 3)), zorder=3)
        for i, value in enumerate(points):
            peak = (i == 0 and value > points[1]) or (i > 0 and value > points[i - 1])
            ax.annotate(f"P{i}\n{value:g}", (i, value), textcoords="offset points",
                        xytext=(0, 10 if peak else -11), va="bottom" if peak else "top",
                        ha="center", fontsize=9, color=ink,
                        bbox={"facecolor": "white", "edgecolor": "none", "pad": .7, "alpha": .9})
        for i in marks:
            ax.scatter(i, points[i], s=165, facecolor="none", edgecolor=color, linewidth=2, zorder=5)
        ax.set_xticks([])
        ax.grid(axis="y", alpha=.16)
        ax.spines[["top", "right", "bottom"]].set_visible(False)
        ax.spines["left"].set_color("#ccd4df")
        ax.tick_params(axis="y", labelsize=9, colors="#64748b")
        if note:
            ax.text(.01, -.12, note, transform=ax.transAxes, color=color, fontsize=11, va="top")

    def bars(ax, values, names, colors, title, highlight=None):
        ax.set_title(title, loc="left", fontsize=13, fontweight="bold", pad=21, color=ink)
        lo, hi = min(a for a, _ in values), max(b for _, b in values)
        span = hi - lo
        for i, ((low, high), name, col) in enumerate(zip(values, names, colors)):
            ax.plot([i, i], [low, high], color=col, linewidth=13, alpha=.25, solid_capstyle="butt")
            ax.plot([i, i], [low, high], color=col, linewidth=3)
            ax.plot([i - .10, i + .10], [low, low], color=col, linewidth=2)
            ax.plot([i - .10, i + .10], [high, high], color=col, linewidth=2)
            ax.annotate(f"{high:g}", (i, high), xytext=(0, 8), textcoords="offset points", ha="center", color=col)
            ax.annotate(f"{low:g}", (i, low), xytext=(0, -16), textcoords="offset points", ha="center", color=col)
        if highlight:
            low, high = highlight
            ax.axhspan(low, high, color=amber, alpha=.13)
            ax.text(len(values) - .45, (low + high) / 2, "特征缺口", ha="right", va="center", fontsize=10, color=amber)
        ax.set_xticks(range(len(names)), names, fontsize=10)
        ax.set_ylim(lo - .24 * span, hi + .28 * span)
        ax.set_xlim(-.55, len(names) - .45)
        ax.grid(axis="y", alpha=.16)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color("#ccd4df")
        ax.tick_params(axis="y", labelsize=9, colors="#64748b")

    titles = ["01  首笔已破坏，包含后却产生特征缺口", "02  同价极值：段界选较早还是较晚？",
              "03  恰好回到首笔起点，再破终点", "04  合法参考与内部极值：原文已有的约束",
              "05  包含方向：作者指定的 36 不能变成 54"]
    r1, r2, r3 = data["cases"]["r1"]["11_0"], data["cases"]["r2"]["14"], data["cases"]["r3"]["14"]
    specs = [
        (r1, [[6, 10], [4, 14], [11, 14], [3, 13]], ["左参考", "原始首笔", "包含后的中", "独立右元素"],
         "修改前：标准元素判类", "已裁定：采用 B，原始首笔判类", "0—3 待定，等待第二分型", "0—3 已确认；3—8 形成中", [3, 6], (10, 11)),
        (r2, [[13, 16], [11, 14], [11, 13], [12, 14]], ["第二序列左", "笔 6", "笔 6+8 合并", "第二序列右"],
         "修改前：保留 P6，后续漏划", "已裁定：保留 P6，第三段结束于 P11", "0—3、3—6 已确认；6—13 待定", "0—3、3—6、6—11 已确认；11—14 待定", [6, 8, 11], None),
        (r3, [[6, 10], [4, 14], [3, 14], [4, 14]], ["左参考", "原始首笔", "第三笔", "两笔合并"],
         "修改前：未完成段显示至 P3", "已裁定：P0—P5 未完成", "只有左元素和合并元素，尚无第一分型", "同价末端显示至 P5，仍不提前确认", [3, 5], None),
    ]
    output = []
    for k, (entry, ranges, names, a_title, b_title, a_note, b_note, marks, gap) in enumerate(specs):
        fig, axes = plt.subplots(2, 2, figsize=(16, 9.6))
        fig.subplots_adjust(top=.84, bottom=.16, left=.055, right=.97, hspace=.63, wspace=.18)
        fig.suptitle(titles[k], x=.055, y=.963, ha="left", fontsize=22, fontweight="bold", color=ink)
        fig.text(.055, .913, "数值均为合成示例  ·  灰线＝原始笔  ·  彩色实线＝确认段  ·  彩色虚线＝待定段  ·  两侧使用同一价格尺度", color="#64748b", fontsize=11)
        plot(axes[0, 0], entry["points"], marks=marks, title="原始笔链与关键点")
        bars(axes[0, 1], ranges, names, [gray, blue, amber, gray], "特征元素的价格区间（不是行情跳空）", gap)
        plot(axes[1, 0], entry["points"], entry["a"]["segments"], blue, marks, a_title, a_note)
        plot(axes[1, 1], entry["points"], entry["approved"]["segments"], green, marks, b_title, b_note)
        source_footer(fig, [["standard", "first"], ["equal", "pen"], ["first", "equal", "three"]][k])
        name = f"0{k+1}_" + ["包含后新增缺口", "同价端点", "等值返回首笔起点"][k]
        for suffix in ("png", "svg"):
            path = DEST / f"{name}.{suffix}"
            fig.savefig(path, dpi=160)
            output.append(str(path))
        plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(16, 9.6))
    fig.subplots_adjust(top=.82, bottom=.16, left=.055, right=.97, hspace=.68, wspace=.20)
    fig.suptitle(titles[3], x=.055, y=.96, ha="left", fontsize=22, fontweight="bold", color=ink)
    fig.text(.055, .91, "合成价格实现原图关系；两例已有作者答复。灰线＝笔，实线＝确认段，虚线＝待定段。", color="#64748b")
    upper = data["cases"]["r4"]["upper"]
    ref = data["cases"]["r4"]["reference"]
    plot(axes[0, 0], upper["points"], upper["a"]["segments"], blue, [4, 8], "第79课上图：第二段结束于 P8", "内部 P4=32 高于段端 P8=30")
    bars(axes[0, 1], [[20, 29], [12, 26], [18, 30]], ["12", "36＝34+56", "78"], [gray, blue, gray], "作者指定的 12、36、78")
    plot(axes[1, 0], ref["points"], ref["a"]["segments"], blue, [1, 3, 5], "第81课答复：选 1，不选较低的 3", "参考 12、56、78；0—5 确认")
    bars(axes[1, 1], [[3, 8], [2, 6], [7, 12], [4, 11]], ["12：作者所选", "34：本例排除", "56", "78"], [blue, amber, blue, blue], "错误取 34，会人为得到 6—7 的缺口")
    source_footer(fig, ["fig79", "reference"])
    for suffix in ("png", "svg"):
        path = DEST / f"04_参考资格与内部极值.{suffix}"
        fig.savefig(path, dpi=160)
        output.append(str(path))
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(16, 7.0))
    fig.subplots_adjust(top=.76, bottom=.25, left=.055, right=.97, wspace=.20)
    fig.suptitle(titles[4], x=.055, y=.955, ha="left", fontsize=22, fontweight="bold", color=ink)
    fig.text(.055, .875, "第79课合成值：P3=12、P4=32、P5=14、P6=26。作者明确说 34、56 合为 36。", color="#64748b")
    bars(axes[0], [[12, 32], [14, 26], [12, 26]], ["34 原始区间", "56 原始区间", "向下包含：36"], [gray, gray, blue], "A  两边取小 → [12,26]，保留原候选 P3=12")
    bars(axes[1], [[12, 32], [14, 26], [14, 32]], ["34 原始区间", "56 原始区间", "向上包含：54"], [gray, gray, amber], "B  两边取大 → [14,32]，不是作者所说的 36")
    fig.text(.055, .135, "这个图已经排除随意反转方向；其余无前项语境、同价包含的初始化仍需另行论证。", color=ink, fontsize=12)
    source_footer(fig, ["inclusion", "fig79"])
    for suffix in ("png", "svg"):
        path = DEST / f"05_包含方向约束.{suffix}"
        fig.savefig(path, dpi=160)
        output.append(str(path))
    plt.close(fig)

    fig, axes = plt.subplots(3, 2, figsize=(16, 16))
    fig.subplots_adjust(top=.91, bottom=.085, left=.055, right=.97, hspace=.60, wspace=.16)
    fig.suptitle("三项规则已裁定 · 修改前与当前结果", x=.055, y=.975, ha="left", fontsize=24, fontweight="bold", color=ink)
    fig.text(.055, .94, "用户裁定：2026-09-17。合成示例；蓝色为修改前，绿色为当前；实线为确认段，虚线为待定段。", color="#64748b", fontsize=11)
    for k, spec in enumerate(specs):
        entry, _, _, a_title, b_title, a_note, b_note, marks, _ = spec
        plot(axes[k, 0], entry["points"], entry["a"]["segments"], blue, marks, f"{k+1} · {a_title}", a_note)
        plot(axes[k, 1], entry["points"], entry["approved"]["segments"], green, marks, f"{k+1} · {b_title}", b_note)
    fig.text(.055, .022, "完整原文、特征元素包含过程、上下镜像、数值变体及判断记录，见同目录《线段规则争议图解.html》。\n研究依据仅为 D:\\缠论；排除实际行情跳空。", color="#64748b", fontsize=11, linespacing=1.6)
    path = DEST / "三项争议对照总览.png"
    fig.savefig(path, dpi=160)
    output.append(str(path))
    plt.close(fig)
    return output


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    DEST.mkdir(parents=True, exist_ok=True)
    data = build_data()
    data_text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    template = (ROOT / "docs/segment_rule_review.template.html").read_text(encoding="utf-8")
    assert template.count("__REVIEW_DATA__") == 1
    page = DEST / "线段规则争议图解.html"
    page.write_text(template.replace("__REVIEW_DATA__", data_text), encoding="utf-8")
    outputs = make_print_charts(data)
    manifest = {"html": str(page), "images": outputs, "implementation_sha256": data["implementation_sha256"],
                "generator_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                                      ("script/build_segment_review_charts.py", "docs/segment_rule_review.template.html", "docs/segment_review_before_20260917.json")},
                "sources": {k: {x: v[x] for x in ("path", "spans", "sha256")} for k, v in data["sources"].items()},
                 "generated_at": data["generated_at"], "generator_changes_production_rules": False,
                 "review_decisions_applied": data["review_decisions"]}
    (DEST / "图表生成记录.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (DEST / "用户裁定_2026-09-17.json").write_text(json.dumps(data["review_decisions"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"html": str(page), "static_files": len(outputs), "size_bytes": page.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
