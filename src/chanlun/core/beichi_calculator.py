"""beichi_calculator.py — 级别无关的缠论背驰内核(子项目②)。

全部为纯函数：力度(MACD)经注入的 ld_provider 回调获取，内核不依赖 CL。
走势段用鸭子类型——BI(笔)/XD(线段)/ZSLX(走势类型) 均为 LINE 子类，
共享 .type/.start/.end/.high/.low 接口，故同一套内核适用于任意级别。

原文依据见 docs/chanlun_core_redesign_2_beichi_design.md §2。
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from chanlun.core.cl_interface import BI, Config, FX, LINE, ZS

# 力度 provider 类型：给定走势段的起止分型，返回 query_macd_ld 风格的 ld 字典
LdProvider = Callable[[FX, FX], dict]


def _use_huangbai(seg: LINE) -> bool:
    """力度口径是否包含黄白线。

    原文(第三章·第二十五节)细则1：1分钟以下级别只比柱子面积；1分钟级别
    及以上同时考虑黄白线。本引擎中笔≈1分钟以下波动 → 仅柱子；线段/走势
    类型≈1分钟级别走势类型及以上 → 柱子 + 黄白线。
    """
    return not isinstance(seg, BI)


def _xingao_xindi(seg_a: LINE, seg_b: LINE) -> bool:
    """创新高/新低前提（原文细则2：「背驰如果没有创新高，是不存在的」）。

    seg_a 在前、seg_b 在后、同向。up 段要求 seg_b 高点高于 seg_a；
    down 段要求 seg_b 低点低于 seg_a。
    """
    if seg_b.type == "up":
        return seg_b.high > seg_a.high
    return seg_b.low < seg_a.low


def _ld_area(ld: dict, direction: str) -> float:
    """柱子同向面积：up 看红柱和 up_sum，down 看绿柱和 down_sum。"""
    return ld["hist"]["up_sum"] if direction == "up" else ld["hist"]["down_sum"]


def _ld_huangbai(ld: dict, direction: str) -> float:
    """黄白线高度：up 看 DIF 最大值（离 0 轴最远），down 看 DIF 最小值。"""
    return ld["dif"]["max"] if direction == "up" else ld["dif"]["min"]


def _ld_decays(seg_a: LINE, seg_b: LINE, ld_provider: LdProvider) -> bool:
    """力度是否衰竭（seg_b 在前者 seg_a 之后、同向）。

    步骤2 柱子面积衰竭：seg_b 同向柱子面积 < seg_a。
    步骤3 黄白线衰竭（仅线段及以上）：up 段 DIF 高点更低、down 段 DIF 低点更高。
    """
    direction = seg_b.type
    ld_a = ld_provider(seg_a.start, seg_a.end)
    ld_b = ld_provider(seg_b.start, seg_b.end)

    # 步骤2：柱子面积衰竭
    if not (_ld_area(ld_b, direction) < _ld_area(ld_a, direction)):
        return False

    # 步骤3：黄白线衰竭——仅线段及以上（笔跳过）
    if _use_huangbai(seg_b):
        hb_a = _ld_huangbai(ld_a, direction)
        hb_b = _ld_huangbai(ld_b, direction)
        if direction == "up":
            if not (hb_b < hb_a):
                return False
        else:
            if not (hb_b > hb_a):
                return False
    return True


def is_beichi(seg_a: LINE, seg_b: LINE, ld_provider: LdProvider) -> bool:
    """背驰原语：seg_b（在后）相对 seg_a（在前、同向）是否背驰。

    原文(第二章·第四节)：背驰 = 力度衰竭。判定两步——
      1. 创新高/新低前提（原文细则2）；不满足直接非背驰。
      2. 力度衰竭（柱子面积 + 级别相关黄白线）。
    """
    if not _xingao_xindi(seg_a, seg_b):
        return False
    return _ld_decays(seg_a, seg_b, ld_provider)


def is_qs(one_zs: ZS, two_zs: ZS, wzgx_config: str) -> Optional[str]:
    """趋势关系判定：相邻两中枢是否「依次同向」构成趋势。

    原文：趋势至少两个依次同向的走势中枢。「依次同向」的松紧由 wzgx_config
    给定——原文「一个中枢整体在另一个之上/下」对应严格档 GD；默认档 ZGGDD
    较宽，此口径是缠论本身的模糊点，沿用代码库现有默认配置。
    返回 'up' / 'down' / None。
    """
    if wzgx_config == Config.ZS_WZGX_ZGD.value:
        # 宽松：zg 与 zd
        if one_zs.zg < two_zs.zd:
            return "up"
        if one_zs.zd > two_zs.zg:
            return "down"
    elif wzgx_config == Config.ZS_WZGX_ZGGDD.value:
        # 较宽松：zg 与 dd、zd 与 gg
        if one_zs.zg < two_zs.dd:
            return "up"
        if one_zs.zd > two_zs.gg:
            return "down"
    elif wzgx_config == Config.ZS_WZGX_GD.value:
        # 严格：gg 与 dd
        if one_zs.gg < two_zs.dd:
            return "up"
        if one_zs.dd > two_zs.gg:
            return "down"
    return None
