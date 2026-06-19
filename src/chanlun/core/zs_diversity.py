"""走势分解多样性精炼层（方案A）。纯函数，零副作用，不改 ZsCalculator/zs_branch。

原文锚:
  L054:47  选最有意义的走势分解
  L038:215 三类点=延伸结束信号
  L033:17  九段升级条件
  L043:57  巨型中枢非法判定
"""
from __future__ import annotations

from typing import List, Optional

from chanlun.core.types import LINE


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
