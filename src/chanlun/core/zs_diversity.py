"""走势分解多样性精炼层（方案A）。纯函数，零副作用，不改 ZsCalculator/zs_branch。

原文锚:
  L054:47  选最有意义的走势分解
  L038:215 三类点=延伸结束信号
  L033:17  九段升级条件
  L043:57  巨型中枢非法判定
"""
from __future__ import annotations

import copy
from typing import List, Optional

from chanlun.core.types import LINE, ZS
from chanlun.core.zs_calculator import ZsCalculator


def _first_third_class(
    segs: List[LINE], zd: float, zg: float, core: int = 3
) -> Optional[int]:
    """返回 segs 中首个三类买卖点的离开段下标（核心区后）。

    对齐 bs_branch._third_class 口径：
      离开上 end > zg  且 回试 end >= zg  → 三买
      离开下 end < zd  且 回试 end <= zd  → 三卖

    参数
    ----
    segs : 线段列表（含核心区段 + 延伸段）
    zd   : 中枢下沿 ZD
    zg   : 中枢上沿 ZG
    core : 核心区最少段数，检测从下标 core 开始（默认 3）

    返回
    ----
    首个满足条件的离开段下标 i（i >= core），无则 None。
    """
    for i in range(core, len(segs) - 1):
        leave = segs[i]
        retest = segs[i + 1]
        if leave._type == "up" and leave.end.val > zg and retest.end.val >= zg:
            return i
        if leave._type == "down" and leave.end.val < zd and retest.end.val <= zd:
            return i
    return None


def _zs_segs(z: ZS) -> List[LINE]:
    """中枢覆盖的完整段序列（本体 lines + 离开段 end，去重）。"""
    segs = list(z.lines)
    if z.end is not None and (not segs or z.end is not segs[-1]):
        segs.append(z.end)
    return segs


def _truncate(z: ZS, i: int) -> ZS:
    """把中枢收口到下标 i 处：本体 = segs[:i]，离开段 = segs[i]。"""
    segs = _zs_segs(z)
    z2 = copy.copy(z)
    z2.lines = list(segs[:i])
    z2.end = segs[i]
    z2.done = True
    z2._bounds_dirty = True
    if hasattr(z2, "update_boundaries"):
        z2.update_boundaries()
    return z2


def _refine_r4(zss: List[ZS], units: List[LINE], min_lines: int) -> List[ZS]:
    """R4 精炼：把每个跨三类点的贪婪中枢在三类点处收口，余段复用 ZsCalculator 重扫。

    原文锚: L038:215 三类点=延伸结束信号

    参数
    ----
    zss       : 待精炼中枢列表（通常来自 ZsCalculator.calculate）
    units     : 与 zss 对应的基础段列表（目前仅透传给递归调用，供未来使用）
    min_lines : 传给 ZsCalculator(min_zs_lines) 的最小构成段数

    返回
    ----
    精炼后的中枢列表（保持时序，可能比输入更多）。
    """
    out: List[ZS] = []
    for z in zss:
        segs = _zs_segs(z)
        i = _first_third_class(segs, z.zd, z.zg, core=3)
        if i is None or i < min_lines:
            # 无中间三类点，或切后本体不足 min_lines → 原样保留
            out.append(z)
            continue
        # 收口：中枢截止到下标 i
        out.append(_truncate(z, i))
        # 余段（三类点之后）交给引擎重扫，递归精炼
        tail = segs[i + 1 :]
        if len(tail) >= min_lines:
            rescanned = ZsCalculator(min_zs_lines=min_lines).calculate(tail)
            out.extend(_refine_r4(rescanned, tail, min_lines))
    return out
