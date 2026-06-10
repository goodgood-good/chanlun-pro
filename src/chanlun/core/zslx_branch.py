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
        zss: List[ZS], start_idx: int, cur_dir: Optional[str], done: bool,
        swing_dir: Optional[str] = None,
    ) -> ZSLX:
        """把一个中枢列表收尾成 ZSLX：分类、边界(含进入/离开段 a/b)、包络。

        盘整段方向(_type)取『摆动方向』swing_dir（由 _swing_segments 给，相对前段的涨跌），
        非中枢内部段净位移——原文 line25179 Ai 严格交替按涨跌定向；旧实现用内部段位移导致
        单中枢盘整方向系统性反号(000001 5m 实测)。swing_dir 缺省(直接单元测试 _finalize)
        时退化用内部段净位移。
        """
        if cur_dir in ("trend_up", "trend_down"):
            # 趋势：≥2 依次同向(本体分离)中枢。
            direction = "up" if cur_dir == "trend_up" else "down"
            zslx_type = "上涨" if direction == "up" else "下跌"
        else:
            # 仅由中枢扩张(expand,本体相交)连接、无趋势方向 → 盘整。方向 = 摆动方向。
            zslx_type = "盘整"
            if swing_dir in ("up", "down"):
                direction = swing_dir
            else:
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
        # 喂回 zs_branch 当 L1+ 输入段:zs_high/zs_low = 走势类型**整段高低点**(原文20课
        # gn/dn=Zn 的高、低点,含进入/离开段端点 start/end——趋势段两端远超中枢包络)。
        # 曾用段内中枢 gg/dd 包络:口径过严,L1+ 三段重合判定偏严(2026-06-10 全链对齐
        # line10018,与 zs_upgrade._zslx_span/tongjibie 整段口径同源)。
        hi = max(zs.gg for zs in zss)
        lo = min(zs.dd for zs in zss)
        for fx in (zslx.start, zslx.end):
            v = getattr(fx, "val", None) if fx is not None else None
            if v is not None:
                hi = max(hi, v)
                lo = min(lo, v)
        zslx.zs_high = hi
        zslx.zs_low = lo
        return zslx

    @staticmethod
    def _swing_segments(zss: List[ZS]) -> List[tuple]:
        """中枢本体摆动分段 → [(start, end, dir), …]（连续、覆盖全序列；末段含右边缘）。
        dir = 该摆动腿方向 'up'|'down'（单中枢时 None，方向交回 _finalize 退化处理）。

        反转确认 = 反向中枢本体『完全脱离』极值中枢本体（下跌→上涨:某中枢 dd > 谷中枢 gg；
        上涨→下跌:某中枢 gg < 峰中枢 dd），边界落在极值中枢。
        为何不用 classify_rel 逐对关系:反转处 L0 中枢常严重重叠 → classify_rel 返回 expand
        而非 trend_down/up，对真实反转『失明』(000001 顶 z16→z17 全程 expand)。本体极值摆动
        才看得见反转——这正是原文升级口径(line30931:升级把次级别当线段、高低点=端点;
        line24727:本体分离=中枢关系;line24736:无背驰时第二段走出来后才分解=本体脱离确认)。
        """
        n = len(zss)
        if n == 0:
            return []
        if n == 1:
            return [(0, 0, None)]
        bounds: List[tuple] = []
        start = 0
        ext_idx = 0                                       # 当前趋势极值中枢索引
        # 初始方向:首两中枢本体中点净位移
        D = "down" if (zss[1].dd + zss[1].gg) < (zss[0].dd + zss[0].gg) else "up"
        for i in range(1, n):
            z, zext = zss[i], zss[ext_idx]
            if D == "down":
                if z.dd < zext.dd:                        # 创新低 → 下跌延续,更新极值
                    ext_idx = i
                elif z.dd > zext.gg:                      # 反向中枢本体脱离谷中枢本体 → 反转
                    bounds.append((start, ext_idx, "down"))
                    start, D = ext_idx + 1, "up"
                    ext_idx = max(range(start, i + 1), key=lambda k: zss[k].gg)
            else:                                          # up
                if z.gg > zext.gg:                        # 创新高 → 上涨延续
                    ext_idx = i
                elif z.gg < zext.dd:                      # 反向中枢本体脱离峰中枢本体 → 反转
                    bounds.append((start, ext_idx, "up"))
                    start, D = ext_idx + 1, "down"
                    ext_idx = min(range(start, i + 1), key=lambda k: zss[k].dd)
        bounds.append((start, n - 1, D))
        return bounds

    @staticmethod
    def _trend_dir(seg: List[ZS]) -> Optional[str]:
        """段内趋势方向:≥2 个本体分离同向中枢 → 'trend_up'|'trend_down';否则 None(盘整)。

        摆动方向已由 _swing_segments 定，但『趋势 vs 盘整』须由本体分离判定:纯 expand
        重叠链(中枢扩展/延伸)= 盘整(line21637:中枢扩展属盘整),≥2 依次同向【本体分离】
        中枢才是趋势(line8152)。取净本体分离方向(trend_up 计数 vs trend_down 计数)。
        """
        if len(seg) < 2:
            return None
        ups = sum(1 for k in range(len(seg) - 1)
                  if classify_rel(seg[k], seg[k + 1]) == "trend_up")
        downs = sum(1 for k in range(len(seg) - 1)
                    if classify_rel(seg[k], seg[k + 1]) == "trend_down")
        if ups > downs:
            return "trend_up"
        if downs > ups:
            return "trend_down"
        return None                                        # 无净本体分离方向(纯扩张/平衡)→ 盘整

    @staticmethod
    def _subsplit(zss: List[ZS], s: int, e: int) -> List[tuple]:
        """本体摆动趋势段[s..e]内按同级别中枢细分 → [(a,b),…]。

        原文 line24735 同级别分解(不延伸、允许盘整+盘整);line24727(5分钟三段重合即构成
        中枢);line24728(延伸成6段=两个盘整连接);line30927(选最优分解=中枢震荡最清晰)。
        段内『连续 ≥2 个 expand gap』(=≥3 个本体相交中枢=一个同级别盘整中枢)→ 切成
        趋势腿|盘整|趋势腿,暴露内部中枢震荡(否则大趋势作单一粗 unit、升级后是假宽框)。
        无内部盘整则原样 [(s,e)]。
        """
        if e - s < 2:
            return [(s, e)]
        gaps = [classify_rel(zss[k - 1], zss[k]) for k in range(s + 1, e + 1)]
        is_pz = [False] * (e - s + 1)                      # 中枢 zss[s+t] 是否属内部盘整
        j = 0
        while j < len(gaps):
            if gaps[j] == "expand":
                k = j
                while k < len(gaps) and gaps[k] == "expand":
                    k += 1
                if k - j >= 2:                             # ≥2 连续 expand=≥3 重叠中枢=盘整(line24727)
                    for t in range(j, k + 1):
                        is_pz[t] = True
                j = k
            else:
                j += 1
        out = []
        a = s
        for i in range(s, e + 1):
            if i == e or is_pz[i - s] != is_pz[i + 1 - s]:
                out.append((a, i))
                a = i + 1
        return out

    def calculate(
        self,
        done_zss: List[ZS],
        done_divergence: List[Optional[DivergenceResult]],
    ) -> List[ZSLX]:
        """把已完成中枢序列切成走势类型（末个 done=False）。

        ① 本体摆动分段(_swing_segments):管重叠失明的大反转;② 趋势段内同级别中枢细分
        (_subsplit):暴露内部盘整震荡(line24735 同级别分解);③ 本体分离分类(_trend_dir):
        ≥2 本体分离同向=趋势、否则盘整。背驰(done_divergence)v1 不参与边界——几何摆动自洽
        (line20108:背驰后仍创新极值则趋势延续);早终结的背驰精修留后。done_divergence 保留
        入参以稳定 recursive_branch 接口、供后续精修。
        """
        if not done_zss:
            return []
        bounds = []
        for s, e, sdir in self._swing_segments(done_zss):
            for a, b in self._subsplit(done_zss, s, e):
                bounds.append((a, b, sdir))                # 子段继承摆动腿方向
        last = len(bounds) - 1
        wts: List[ZSLX] = []
        for k, (a, b, sdir) in enumerate(bounds):
            seg = done_zss[a:b + 1]
            wts.append(self._finalize(
                seg, a, self._trend_dir(seg), done=(k < last), swing_dir=sdir))
        return wts
