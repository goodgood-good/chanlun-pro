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
            # 仅由中枢扩张(expand,本体相交)连接、无趋势方向 → 盘整。方向 = 整段核心净位移
            # (末中枢末核心段终点 vs 首中枢首核心段起点)，沿用旧 zslx_calculator._classify。
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

    @staticmethod
    def _merge_same_type(wts: List[ZSLX]) -> List[ZSLX]:
        """合并相邻同 zslx_type 的走势类型(原文 line7264:连续走势类型必转化为其他类型→
        相邻同类型不可能是两个独立完成走势类型,只能是『一个扩展的走势类型』)。

        依据:line16429「只有背驰才和走势转折有必然联系」+ line20105「下跌最后一个中枢
        扩展=未完成走势类型的延续、还在一个走势类型里」+ line20108「背驰后反弹不回抽最后
        中枢则趋势延续」。病灶(301004):qs 背驰(底背驰)在持续下跌中途切出『下跌→下跌→
        下跌』,但价格没真反转、继续下台阶(=扩展/延续),把那些中途背驰/反转边界变成走势
        类型内部、合并回一个下跌走势类型。不同类型(下跌→盘整)是合法连接,不并。

        合并用 _finalize 重收尾:cur_dir 按目标 zslx_type 还原(上涨→trend_up/下跌→
        trend_down/盘整→None),done 取末段、start_idx 取首段(prev.index=首走势类型 start_idx)。
        """
        _dir = {"上涨": "trend_up", "下跌": "trend_down"}
        merged: List[ZSLX] = []
        for zx in wts:
            if merged and zx.zslx_type == merged[-1].zslx_type:
                prev = merged[-1]
                combined = list(prev.zss) + list(zx.zss)
                merged[-1] = ZslxBranchCalculator._finalize(
                    combined, prev.index, _dir.get(zx.zslx_type), done=zx.done
                )
            else:
                merged.append(zx)
        return merged

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
                # 反转后新走势类型方向由其后 classify_rel 定(不能用反转方向 rel——反弹高点
                # 中枢后续若 trend_up 则它是上涨趋势的起点,而非单盘整;原文趋势=≥2依次同向中枢)。
                cur, cur_start, cur_dir = [zi], i, None
            else:
                cur.append(zi)
                if cur_dir is None and rel in ("trend_up", "trend_down"):
                    cur_dir = rel             # 趋势方向坐实(只认 trend_*，expand 不写入)

            # 背驰边界：只在【趋势背驰 qs】切(走势终完美=趋势完成,原文 7260/22415)；
            # 盘整背驰 pz 是中枢震荡内的力度衰减、不构成走势类型边界，不切。
            if not reverse and cur is not None:
                dv = done_divergence[i]
                if dv is not None and dv.is_beichi and getattr(dv, "kind", None) == "qs":
                    wts.append(self._finalize(cur, cur_start, cur_dir, done=True))
                    cur, cur_dir, cur_start = None, None, -1

        if cur is not None:
            wts.append(self._finalize(cur, cur_start, cur_dir, done=False))
        # 合并相邻同类型走势类型(原文 line7264:连续走势类型必不同类型;同类型=扩展、一个走势类型)
        return self._merge_same_type(wts)
