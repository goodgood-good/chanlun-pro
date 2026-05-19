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


def _drill_chain(top_zslx: ZSLX, top_level: int):
    """从顶层走势类型沿 zss[-1].lines[-1] 逐级下钻。

    返回 [(level, cur_zslx, beichi_seg), ...]——cur_zslx 是该级走势类型，
    beichi_seg 是其离开末中枢的背驰段。背驰段为更低级走势类型(ZSLX) → 继续
    下钻；为线段(XD) → 即 L0 重，记完停止。
    """
    chain = []
    cur = top_zslx
    level = top_level
    while isinstance(cur, ZSLX):
        if not cur.zss or not cur.zss[-1].lines:
            break
        seg = cur.zss[-1].lines[-1]
        chain.append((level, cur, seg))
        if isinstance(seg, ZSLX):
            cur = seg
            level -= 1
        else:
            break
    return chain


def _zslx_is_beichi(
    zslx: ZSLX, units: List[LINE], ld_provider: LdProvider, wzgx_config: str
) -> bool:
    """② 复核走势类型是否背驰：趋势走 beichi_qs、盘整走 beichi_pz。

    背驰段 = 走势类型末中枢的离开段（zss[-1].lines[-1]）。
    """
    if not zslx.zss or not zslx.zss[-1].lines:
        return False
    leave_seg = zslx.zss[-1].lines[-1]
    if zslx.zslx_type in ("上涨", "下跌"):
        is_bc, _ = beichi_qs(units, zslx.zss, leave_seg, ld_provider, wzgx_config)
    else:
        is_bc, _ = beichi_pz(zslx.zss[-1], leave_seg, ld_provider)
    return is_bc


def calculate_interval_nest(
    levels: List[LevelResult],
    xds: List[XD],
    ld_provider: LdProvider,
    wzgx_config: str,
) -> Optional[IntervalNest]:
    """计算「当下」区间套：最高级别趋势背驰的末走势类型 → 逐级下钻到 L0。

    无任何级别的末走势类型为趋势背驰 → 返回 None。
    """
    if not levels:
        return None

    # 第 1 步：找起点——最高的「末走势类型是趋势且趋势背驰」的级别
    start = None
    for k in range(len(levels) - 1, -1, -1):
        zslxs = levels[k].zslxs
        if not zslxs:
            continue
        wt = zslxs[-1]
        if wt.zslx_type not in ("上涨", "下跌"):
            continue
        units = _units_at_level(levels, xds, k)
        if _zslx_is_beichi(wt, units, ld_provider, wzgx_config):
            start = (k, wt)
            break
    if start is None:
        return None

    # 第 2 步：逐级下钻
    chain = _drill_chain(start[1], start[0])
    if not chain:
        return None

    # 第 3 步：每重 ② 复核背驰、组装
    links: List[NestLink] = []
    for level, cur, seg in chain:
        units = _units_at_level(levels, xds, level)
        links.append(NestLink(
            level=level,
            beichi_seg=seg,
            is_beichi=_zslx_is_beichi(cur, units, ld_provider, wzgx_config),
        ))
    last_seg = links[-1].beichi_seg
    return IntervalNest(
        links=links, turning_point=last_seg.end, direction=last_seg.type
    )
