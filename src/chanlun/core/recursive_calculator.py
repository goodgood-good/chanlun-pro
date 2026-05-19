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


def _split_one(zs: ZS) -> List[ZS]:
    """把一个 ≥9 段中枢按 (123)(456)(789)… 拆成 ⌊N/3⌋ 个三段子中枢。

    N%3≠0 时余 1~2 段并入最后一组（保证每组 ≥3）。子中枢边界口径与
    ZsCalculator 初始三段一致：zg/zd 取前三段、gg/dd 由 update_boundaries 全量。
    """
    lines = zs.lines
    n = len(lines)
    groups_count = n // 3
    subs: List[ZS] = []
    idx = 0
    for g in range(groups_count):
        size = 3 if g < groups_count - 1 else (n - idx)  # 末组吸收余数
        group = lines[idx:idx + size]
        idx += size
        sub = ZS(zs_type=zs.zs_type, start=None)
        sub.lines = group
        sub._bounds_dirty = True
        sub.zg = min(s.zs_high for s in group[:3])
        sub.zd = max(s.zs_low for s in group[:3])
        sub.update_boundaries()
        subs.append(sub)
    return subs


def _split_oversized(zss: List[ZS]) -> List[ZS]:
    """对中枢列表做 9 段分裂：≥9 段的中枢拆成三段子中枢，其余原样。

    落地缠论段2.1「延伸超9段升级」——单个 ≥9 段中枢经 ③划分只得 1 个盘整、
    单独无法升级；拆开后子中枢经划分、上一级扫描可聚成高级中枢。
    """
    out: List[ZS] = []
    for zs in zss:
        if len(zs.lines) <= 8:
            out.append(zs)
        else:
            out.extend(_split_one(zs))
    return out


class RecursiveCalculator:
    """递归装配计算器。无状态，每次 calculate 全量重算。"""

    def calculate(
        self, xds: List[XD], ld_provider: LdProvider, wzgx_config: str
    ) -> List[LevelResult]:
        """把线段递归装配成多级中枢/走势类型层级树。

        每级：扫描(ZsCalculator) → 9段分裂 → 划分(ZslxCalculator) → 走势类型
        经 _as_units 变下一级走势单元。终止：扫不出中枢，或走势类型 <3。
        """
        if not xds:
            return []

        results: List[LevelResult] = []
        units: List[LINE] = list(xds)
        level = 0
        while level < _MAX_LEVELS:
            zss = ZsCalculator(
                require_alternation=(level == 0)
            ).calculate(units)
            zss = _split_oversized(zss)
            if not zss:
                break
            zslxs = ZslxCalculator().calculate(
                zss, units, ld_provider, wzgx_config
            )
            results.append(LevelResult(level, zss, zslxs))
            if len(zslxs) < 3:
                break
            units = _as_units(zslxs)
            level += 1
        return results
