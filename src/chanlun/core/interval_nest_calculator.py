"""interval_nest_calculator.py — 缠论区间套（基于 ④ 递归层级树）。

原文第三章·第六节《区间套》：根据背驰段从高级别向低级别逐级寻找背驰点，
逐重收缩范围、精确定位转折点。本模块从 ④ 的递归层级树出发，找最高级别趋势
背驰的末走势类型，沿 zss[-1].lines[-1] 下钻到 L0。

并存独立子系统：不动周期分析、不动 bs_point_calculator。
设计见 docs/chanlun_core_redesign_5_interval_nest_design.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from chanlun.core.beichi_calculator import LdProvider, beichi_pz, beichi_qs
from chanlun.core.cl_interface import FX, LINE, XD, ZSLX
from chanlun.core.recursive_calculator import LevelResult


@dataclass
class NestLink:
    """区间套的一重：某级别的背驰段。"""
    level: int          # 该重背驰段所在的递归级别
    beichi_seg: LINE    # 该级别的背驰段（离开末中枢的走势段）
    is_beichi: bool     # ② 复核该段所属走势类型是否确为背驰


@dataclass
class IntervalNest:
    """区间套：从最高背驰级别逐重下钻到 L0 的背驰段链。"""
    links: List[NestLink]   # index 0 = 最高级别
    turning_point: FX       # L0 背驰段的终分型 = 精确转折点
    direction: str          # L0 背驰段方向：'up'→顶背驰(卖点)、'down'→底背驰(买点)


def _units_at_level(
    levels: List[LevelResult], xds: List[XD], k: int
) -> List[LINE]:
    """级别 k 的走势单元列表：k==0 为线段；k>=1 为第 k-1 级走势类型。"""
    return list(xds) if k == 0 else list(levels[k - 1].zslxs)
