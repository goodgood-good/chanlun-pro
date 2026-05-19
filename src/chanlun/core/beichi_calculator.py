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
