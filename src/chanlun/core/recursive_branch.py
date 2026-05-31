"""recursive_branch.py — P4b 缠论递归装配（走势类型递归主链 + 独立升级标注）。

把 L0 线段自底向上递归装配成多级层级树：units→zs_branch(中枢+内联背驰)→
zslx_branch(走势类型)→_as_units→units 逐级。升级标注(9段/扩展)是独立旁路、
不改走势类型边界(原文 line16429：中枢扩展⊥走势转折)。

孤立、不接 CL、不动旧 recursive_calculator。
设计见 docs/chanlun_core_redesign_4b_递归装配_design.md。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from chanlun.core.beichi_calculator import LdProvider
from chanlun.core.cl_interface import LINE, ZS, ZSLX
from chanlun.core.zs_branch import DivergenceResult, ZsBranchCalculator, classify_rel
from chanlun.core.zslx_branch import ZslxBranchCalculator

_MAX_LEVELS = 50    # 护栏；走势单元逐级收缩，正常远不及


@dataclass
class LevelResult:
    """单个递归级别的产出。"""
    level: int                                          # 0 = L0
    zss: List[ZS]                                       # 本级已完成中枢
    done_divergence: List[Optional[DivergenceResult]]   # 与 zss 索引对齐(本级内联背驰)
    zslxs: List[ZSLX]                                   # 本级走势类型
    upgrade_idx: List[int] = field(default_factory=list)  # 升级标注:9段/扩展候选的中枢索引(P5 用)
    units: List[LINE] = field(default_factory=list)       # P5c:该级输入段序列(回试段定位)


def _as_units(zslxs: List[ZSLX]) -> List[ZSLX]:
    """ZSLX 喂回 zs_branch 当输入段：返回 index 重排为连续 0,1,2… 的**浅拷贝**
    (不改原 ZSLX——原对象 index 保留 start_idx,供 LevelResult 审图定位)。
    zs_high/zs_low 已由 zslx_branch._finalize 填,浅拷贝沿用。"""
    out: List[ZSLX] = []
    for i, zslx in enumerate(zslxs):
        u = copy.copy(zslx)
        u.index = i
        out.append(u)
    return out


def _mark_upgrades(done_zss: List[ZS]) -> List[int]:
    """本级中枢中「9 段升级 / 中枢扩展候选」的索引（line16429 解耦：仅标注、不改
    走势类型；实体化与 2/3 类买点留 P5）。

    z.lines 元素随级别而异：L0=线段、L≥1=走势类型(ZSLX)，故「9 段」泛指 9 个
    次级别单元（第33课：9 段=3 个次级别走势类型重合）。
    注：calculate 的 pending 分支传入的 H2 中枢未剥离开段(含末段读法)，9 段阈值
    在该路径偏松一档——属 MVP 容差(pending 仅供审图标注，精确实体化留 P5)。"""
    out: List[int] = []
    for i, z in enumerate(done_zss):
        if len(z.lines) >= 9:                          # 9 段升级(9 个次级别单元,第33课)
            out.append(i)
        elif i > 0 and classify_rel(done_zss[i - 1], z) == "expand":  # 中枢扩展(中心定理二本体相交)
            out.append(i)
    return out


class RecursiveBranchCalculator:
    """递归装配计算器。无状态，每次 calculate 全量重算。"""

    def calculate(
        self,
        xds: List[LINE],
        ld_provider: LdProvider,
        wzgx_config: str,
        frequency: Optional[str] = None,
        ld_provider_for_level: Optional[Callable[[int], LdProvider]] = None,
    ) -> List[LevelResult]:
        """把线段递归装配成多级中枢/走势类型层级树。

        每级：zs_branch(中枢+内联背驰) → zslx_branch(走势类型) → _as_units → 下一级。
        L0 构成段=线段(min_zs_lines=4)，L≥1 构成段=走势类型(=3,原文)。
        终止：扫不出中枢 / 走势类型 <3 / 触 _MAX_LEVELS。
        """
        if not xds:
            return []
        results: List[LevelResult] = []
        units: List[LINE] = list(xds)
        zslx_calc = ZslxBranchCalculator()    # 无状态，建一次复用
        level = 0
        while level < _MAX_LEVELS:
            min_lines = 4 if level == 0 else 3
            # 换周期 MACD:各级用对应级别 ld_provider(L0→5m/L1→30m…);无 factory 退化用单一
            lp = ld_provider_for_level(level) if ld_provider_for_level is not None else ld_provider
            res = ZsBranchCalculator(
                ld_provider=lp, frequency=frequency,
                wzgx=wzgx_config, min_zs_lines=min_lines,
            ).calculate(units)
            if not res.done_zss:
                # 右边缘只剩 pending 高级中枢(未被离开段确认完成)：记录其 H2(leave 读法)
                # 中枢 + live 背驰再终止——让层级树展示到右边缘「正在形成」的高级中枢
                # (不上卷:未完成无法切走势类型)。spec §0 MVP「各级只用 done」在此放宽一档:
                # 右边缘 pending 中枢入树(用户验收决策 2026-05-31)。
                pend = [h for h in res.live if h.node1 == "leave"]
                if pend:
                    results.append(LevelResult(
                        level=level, zss=[h.zs for h in pend],
                        done_divergence=[h.divergence for h in pend],
                        zslxs=[], upgrade_idx=_mark_upgrades([h.zs for h in pend]),
                        units=list(units),
                    ))
                break
            zslxs = zslx_calc.calculate(res.done_zss, res.done_divergence)
            assert zslxs, "done_zss 非空时 zslx_branch 必产 ≥1 走势类型(末段 done=False)"
            results.append(LevelResult(
                level=level, zss=res.done_zss, done_divergence=res.done_divergence,
                zslxs=zslxs, upgrade_idx=_mark_upgrades(res.done_zss), units=list(units),
            ))
            if len(zslxs) < 3:
                break
            units = _as_units(zslxs)
            level += 1
        return results
