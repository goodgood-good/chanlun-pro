"""zslx_branch.py — P4a 走势类型划分（基于 zs_branch 内联背驰）。

把 zs_branch 的 L0 已完成中枢序列(done_zss + done_divergence)切成走势类型(ZSLX)：
双信号边界——背驰(复用 done_divergence,不重判) + 方向反转(classify_rel,上涨↔下跌)。
expand(中枢扩张/本体相交)不切——按走势级别延续定理一(原文第20课)延续，升级(L1
中枢=3 个方向交替的 L0 走势类型)实体化留 P4b。孤立、不接 CL、不依赖 beichi_calculator。

设计见 docs/chanlun_core_redesign_4a_走势类型划分_design.md。
"""
from __future__ import annotations

from typing import List, Optional

from chanlun.core.cl_interface import ZS, ZSLX
from chanlun.core.zs_branch import DivergenceResult, classify_rel


class ZslxBranchCalculator:
    """级别无关的走势类型划分（基于 zs_branch 中枢+内联背驰）。无状态，全量重算。"""

    @staticmethod
    def _finalize(
        zss: List[ZS], start_idx: int, cur_dir: Optional[str], done: bool
    ) -> ZSLX:
        """把一个中枢列表收尾成 ZSLX：分类、边界(含进入/离开段 a/b)、包络。"""
        if cur_dir in ("trend_up", "trend_down"):
            # 趋势：≥2 依次同向(本体分离)中枢。
            direction = "up" if cur_dir == "trend_up" else "down"
            zslx_type = "上涨" if direction == "up" else "下跌"
        else:
            # 盘整：单中枢，或仅由中枢扩张(expand,本体相交)连接、无趋势方向的多中枢
            # (高级别中枢候选，实体化留 P4b)。方向 = 整段核心净位移(末中枢末核心段
            # 终点 vs 首中枢首核心段起点)，沿用旧 zslx_calculator._classify 口径。
            # 前提：done 中枢已 correct_exit，lines 是本体(离开段剥到 z.end)。
            zslx_type = "盘整"
            first_seg, last_seg = zss[0].lines[0], zss[-1].lines[-1]
            direction = "up" if last_seg.end.val >= first_seg.start.val else "down"
        # 走势类型边界 = 第一中枢进入段 a → 末中枢离开段 b（原文 a+A+b），缺则退化用核心段
        first = zss[0].start if zss[0].start is not None else zss[0].lines[0]
        last = zss[-1].end if zss[-1].end is not None else zss[-1].lines[-1]
        zslx = ZSLX(
            zslx_level=getattr(zss[0], "level", None),   # L0 中枢通常无 level；级别由 P4b 递归时管理
            start=first.start, end=last.end,
            start_line=first, end_line=last,
            _type=direction, index=start_idx, done=done,
        )
        zslx.zss = list(zss)
        zslx.zslx_type = zslx_type
        # 喂回 zs_branch 备用(P4b)：ZsCalculator 靠构成段 zs_high/zs_low 判重叠
        zslx.zs_high = max(zs.gg for zs in zss)
        zslx.zs_low = min(zs.dd for zs in zss)
        return zslx

    def calculate(
        self,
        done_zss: List[ZS],
        done_divergence: List[Optional[DivergenceResult]],
    ) -> List[ZSLX]:
        """把已完成中枢序列切成走势类型（末个 done=False）。

        双信号边界：方向反转(classify_rel 上涨↔下跌) + 背驰(查 done_divergence)。
        expand(中枢扩张/本体相交)不切——按走势级别延续定理一(原文第20课)延续，
        L1 中枢升级留 P4b。done_divergence 与 done_zss 索引对齐。
        """
        if not done_zss:
            return []
        wts: List[ZSLX] = []
        cur: Optional[List[ZS]] = [done_zss[0]]
        cur_start = 0                         # cur 第一个中枢在 done_zss 的索引
        cur_dir: Optional[str] = None         # "trend_up"|"trend_down"|None(未定/仅扩张)

        for i in range(1, len(done_zss)):
            zi = done_zss[i]
            if cur is None:                   # 上一走势类型被背驰终结，zi 另起
                cur, cur_start, cur_dir = [zi], i, None
                continue

            rel = classify_rel(cur[-1], zi)   # "trend_up"|"trend_down"|"expand"
            # 只有【方向反转】(上涨↔下跌)才切断走势类型。expand(中枢本体相交=高级别
            # 中枢候选)按走势级别延续定理一(第20课)延续、不切——升级实体化(L1 中枢=3
            # 个方向交替的 L0 走势类型)留 P4b；同向(rel==cur_dir)亦延续。
            reverse = (
                cur_dir is not None
                and rel in ("trend_up", "trend_down")
                and rel != cur_dir
            )
            if reverse:
                wts.append(self._finalize(cur, cur_start, cur_dir, done=True))
                cur, cur_start, cur_dir = [zi], i, None
            else:
                cur.append(zi)
                if cur_dir is None and rel in ("trend_up", "trend_down"):
                    cur_dir = rel             # 趋势方向坐实(只认 trend_*，expand 不写入)

            # 背驰边界（仅非方向反转时；zi 刚并入 cur，其背驰 = done_divergence[i]）
            if not reverse and cur is not None:
                dv = done_divergence[i]
                if dv is not None and dv.is_beichi:
                    wts.append(self._finalize(cur, cur_start, cur_dir, done=True))
                    cur, cur_dir, cur_start = None, None, -1

        if cur is not None:
            wts.append(self._finalize(cur, cur_start, cur_dir, done=False))
        return wts
