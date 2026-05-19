"""recursive_calculator.py — 缠论递归装配(子项目④)。

把 ①ZsCalculator(中枢) / ②beichi_calculator(背驰) / ③ZslxCalculator(走势类型)
交替递归，构建 中枢-L0 → 走势类型-L0 → 中枢-L1 → … 的多级层级树。

并存独立子系统：不动周期多级分析、不动 bs_point_calculator。
原文依据见 docs/chanlun_core_redesign_4_recursive_design.md §2。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from chanlun.core.beichi_calculator import LdProvider
from chanlun.core.cl_interface import LINE, XD, ZS, ZSLX
from chanlun.core.zs_calculator import ZsCalculator
from chanlun.core.zslx_calculator import ZslxCalculator

# 递归层数上限——防御性护栏。走势单元数逐级收缩，正常远不及此。
_MAX_LEVELS = 50


@dataclass
class LevelResult:
    """单个递归级别的结果。"""
    level: int            # 0 = L0
    zss: List[ZS]         # 本级中枢
    zslxs: List[ZSLX]     # 本级走势类型


def _as_units(zslxs: List[ZSLX]) -> List[ZSLX]:
    """把走势类型列表整备成下一级中枢扫描的走势单元（就地写字段后返回）。

    ZsCalculator 扫描靠构成段的 zs_high/zs_low 判重叠、靠 index 定位；ZSLX
    默认这两者为 0，喂回前必须写入：
    - zs_high/zs_low = 所含中枢的包络 [min(dd), max(gg)]（spec 决策 3）；
    - index 按 0,1,2… 重排（③ 的 _finalize 给所有 ZSLX 置 index=0）。
    """
    for i, zslx in enumerate(zslxs):
        zslx.zs_high = max(zs.gg for zs in zslx.zss)
        zslx.zs_low = min(zs.dd for zs in zslx.zss)
        zslx.index = i
    return zslxs


class RecursiveCalculator:
    """递归装配计算器。无状态，每次 calculate 全量重算。"""

    def calculate(
        self, xds: List[XD], ld_provider: LdProvider, wzgx_config: str
    ) -> List[LevelResult]:
        """把线段递归装配成多级中枢/走势类型层级树。"""
        if not xds:
            return []
        return []
