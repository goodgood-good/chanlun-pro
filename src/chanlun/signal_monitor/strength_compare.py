# -*- coding: utf-8 -*-
"""
中枢-free 力度对比内核。

把「力度数学」与「引擎数据访问」解耦：

- :func:`compare_strength_raw` —— 纯函数，输入两段力度字典 + 是否创新极值，
  无任何 ``cd`` 依赖，可纯单测，不受缠论笔划分对浮点噪声敏感的影响。
- :func:`compare_strength` —— 薄封装，从 ``LINE`` + ``cd`` 取力度后调 raw。
- :func:`manual_strength_compare` —— 手动图上对比：人选参考段，机器算力度。

仅依赖 分型 / 笔 / 线段 + MACD，**不碰中枢**。自动监控与手动图上对比共用本内核。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from chanlun.core.cl_interface import ICL, LINE, compare_ld_beichi


@dataclass
class StrengthCompareResult:
    """两段同向走势的力度对比结论。"""

    is_beichi: bool          # 是否背驰：创新高/低 且 力度衰减
    made_new_extreme: bool   # 当前段是否创新高(up)/新低(down)
    direction: str           # up / down
    macd_area_ratio: float   # 当前段同向柱子面积 / 参考段；参考段为 0 → inf
    macd_peak_ratio: float   # 峰值比（DIF 同向极值的绝对值之比）
    strength_score: int      # 0-100，越衰减越高（越像背驰）
    detail: dict = field(default_factory=dict)


def _hist_sum(ld: dict, direction: str) -> float:
    """取同向 MACD 柱子面积之和（up→红柱 up_sum，down→绿柱 down_sum）。"""
    key = "up_sum" if direction == "up" else "down_sum"
    try:
        return float(ld["macd"]["hist"][key])
    except (KeyError, TypeError):
        return 0.0


def _peak(ld: dict, direction: str) -> float:
    """取同向 DIF 极值的绝对值（up→max，down→min 的绝对值）。"""
    try:
        if direction == "up":
            return abs(float(ld["macd"]["dif"]["max"]))
        return abs(float(ld["macd"]["dif"]["min"]))
    except (KeyError, TypeError):
        return 0.0


def _ratio(cur: float, ref: float) -> float:
    """当前 / 参考。参考为 0 时：当前 > 0 → inf，否则 → 1.0。"""
    if ref == 0:
        return float("inf") if cur > 0 else 1.0
    return cur / ref


def compare_strength_raw(
    ref_ld: dict,
    cur_ld: dict,
    direction: str,
    made_new_extreme: bool,
) -> StrengthCompareResult:
    """纯力度对比。

    :param ref_ld: 参考段力度字典（``LINE.get_ld`` 的返回结构）
    :param cur_ld: 当前段力度字典
    :param direction: ``"up"`` / ``"down"``
    :param made_new_extreme: 当前段是否创新高/新低（由调用方按几何条件判定）
    :raises ValueError: direction 非法
    """
    if direction not in ("up", "down"):
        raise ValueError(f"direction 必须是 up/down，got {direction!r}")

    ref_area = _hist_sum(ref_ld, direction)
    cur_area = _hist_sum(cur_ld, direction)
    ref_peak = _peak(ref_ld, direction)
    cur_peak = _peak(cur_ld, direction)

    area_ratio = _ratio(cur_area, ref_area)
    peak_ratio = _ratio(cur_peak, ref_peak)

    # 力度衰减：复用引擎 compare_ld_beichi 的口径（同向柱子面积后段 < 前段）
    area_weaker = compare_ld_beichi(ref_ld, cur_ld, direction)
    is_beichi = bool(made_new_extreme and area_weaker)

    # 强度分：面积衰减越多分越高；未创新极值则无背驰意义，记 0。
    if made_new_extreme and area_ratio != float("inf"):
        decay = max(0.0, 1.0 - min(area_ratio, 1.0))
        strength_score = int(round(decay * 100))
    else:
        strength_score = 0

    return StrengthCompareResult(
        is_beichi=is_beichi,
        made_new_extreme=bool(made_new_extreme),
        direction=direction,
        macd_area_ratio=area_ratio,
        macd_peak_ratio=peak_ratio,
        strength_score=strength_score,
        detail={
            "ref_area": ref_area,
            "cur_area": cur_area,
            "ref_peak": ref_peak,
            "cur_peak": cur_peak,
            "area_weaker": area_weaker,
        },
    )


def compare_strength(ref_line: LINE, cur_line: LINE, cd: ICL) -> StrengthCompareResult:
    """从 ``LINE`` + ``cd`` 取力度后做对比。

    :param ref_line: 参考段（笔或线段）
    :param cur_line: 当前段（笔或线段），需与 ref_line 同向
    :param cd: 缠论数据对象（用于 ``LINE.get_ld`` 取 MACD 力度）
    :raises ValueError: 两段方向不一致
    """
    if ref_line.type != cur_line.type:
        raise ValueError(
            f"力度对比要求两段同向：ref={ref_line.type} cur={cur_line.type}"
        )
    direction = cur_line.type
    if direction == "up":
        made_new_extreme = cur_line.high > ref_line.high
    else:
        made_new_extreme = cur_line.low < ref_line.low

    return compare_strength_raw(
        ref_line.get_ld(cd),
        cur_line.get_ld(cd),
        direction,
        made_new_extreme,
    )


# ----------------------------------------------------------------------
# 手动图上对比 —— 与自动监控共用本内核，区别只是「谁来选对比的两段」
# ----------------------------------------------------------------------
def _fmt_dt(d) -> str:
    """统一日期格式（丢时区，两侧一致即可比较）。"""
    return d.strftime("%Y-%m-%d %H:%M:%S") if hasattr(d, "strftime") else str(d)


def _json_num(x):
    """把 inf / nan 转成 None，保证结果可 JSON 序列化。"""
    try:
        if x is None or math.isinf(x) or math.isnan(x):
            return None
    except TypeError:
        return x
    return x


def find_line_by_locator(
    cd: ICL, start_dt, end_dt, line_kind: str = "xd"
) -> "LINE | None":
    """按起止 K 线时间在 cd 的笔/线段里定位一根线；找不到返回 None。

    :param line_kind: ``"xd"`` 找线段，``"bi"`` 找笔
    """
    lines = cd.get_xds() if line_kind == "xd" else cd.get_bis()
    sd, ed = str(start_dt), str(end_dt)
    for ln in lines:
        s = getattr(getattr(ln.start, "k", None), "date", None)
        e = getattr(getattr(ln.end, "k", None), "date", None)
        if _fmt_dt(s) == sd and _fmt_dt(e) == ed:
            return ln
    return None


def manual_strength_compare(
    cd: ICL, ref_start_dt, ref_end_dt, line_kind: str = "xd"
) -> dict:
    """手动力度对比：参考段（按起止时间定位）vs 当前在走的最新同类段。

    返回可直接 JSON 化的 dict，供 web 手动对比接口使用。这是「人做取舍、
    机器算力度」—— 用户在图上点选参考段，本函数负责对比。
    """
    lines = cd.get_xds() if line_kind == "xd" else cd.get_bis()
    if not lines:
        return {"ok": False, "error": f"无可用 {line_kind} 数据"}

    ref = find_line_by_locator(cd, ref_start_dt, ref_end_dt, line_kind)
    if ref is None:
        return {"ok": False, "error": "参考线段未找到（图形可能已变化）"}

    cur = lines[-1]
    if (ref.start.k.k_index == cur.start.k.k_index
            and ref.end.k.k_index == cur.end.k.k_index):
        return {"ok": False, "error": "参考线段就是当前线段，无法对比"}

    if ref.type != cur.type:
        return {
            "ok": False,
            "error": "参考线段与当前线段方向不一致，力度对比需同向",
            "ref_type": ref.type,
            "cur_type": cur.type,
        }

    res = compare_strength(ref, cur, cd)
    return {
        "ok": True,
        "line_kind": line_kind,
        "direction": res.direction,
        "is_beichi": res.is_beichi,
        "made_new_extreme": res.made_new_extreme,
        "macd_area_ratio": _json_num(res.macd_area_ratio),
        "macd_peak_ratio": _json_num(res.macd_peak_ratio),
        "strength_score": res.strength_score,
        "ref": {
            "start": _fmt_dt(ref.start.k.date), "end": _fmt_dt(ref.end.k.date),
            "high": ref.high, "low": ref.low,
        },
        "cur": {
            "start": _fmt_dt(cur.start.k.date), "end": _fmt_dt(cur.end.k.date),
            "high": cur.high, "low": cur.low, "done": bool(cur.is_done()),
        },
        "verdict": (
            "背驰：当前段创新极值但力度衰减"
            if res.is_beichi
            else "未背驰：当前段力度未衰减或未创新极值"
        ),
    }


def list_compare_lines(cd: ICL, line_kind: str = "xd", limit: int = 30) -> list:
    """列出可作参考段的笔/线段（时间正序，最多最近 ``limit`` 个）。

    供 web 手动对比面板的下拉框使用：前端拿这个列表选参考段，
    再调 :func:`manual_strength_compare` 对比。最后一项即「当前在走段」。
    """
    lines = cd.get_xds() if line_kind == "xd" else cd.get_bis()
    out = []
    for ln in lines[-limit:]:
        out.append({
            "start": _fmt_dt(ln.start.k.date),
            "end": _fmt_dt(ln.end.k.date),
            "type": ln.type,
            "high": ln.high,
            "low": ln.low,
            "done": bool(ln.is_done()),
        })
    return out
