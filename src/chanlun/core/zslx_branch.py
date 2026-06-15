"""zslx_branch.py — P4a 走势类型划分（基于 zs_branch 内联背驰）。

把 zs_branch 的 L0 已完成中枢序列(done_zss + done_divergence)切成走势类型(ZSLX)：
双信号边界——背驰(复用 done_divergence,不重判) + 方向反转(classify_rel,上涨↔下跌)。
expand(中枢扩张/本体相交)不切——按走势级别延续定理一(原文第20课)延续，升级(L1
中枢=3 个方向交替的 L0 走势类型)实体化留 P4b。孤立、不接 CL、不依赖 beichi_calculator。

设计见 docs/chanlun_core_redesign_4a_走势类型划分_design.md。
"""
from __future__ import annotations

from typing import List, Optional
from weakref import WeakKeyDictionary

from chanlun.core.types import LINE, ZS, ZSLX
from chanlun.core.zs_branch import DivergenceResult, classify_rel


def _swing_body(z: ZS) -> tuple:
    """摆动腿反转判定用的中枢本体包络 (lo, hi)：剔除已确认的离开段远摆。

    反转判定原本直接用 zs.dd/zs.gg(全段本体极值)。但 3 段成枢时,若末段是离开段
    (定向冲出核心区、终点比起点更远离 [zd,zg]),其远摆会把 gg/dd 撑爆 → 反向中枢
    无法「脱离」该极值 → 反转判定永假(R84 摆动腿失明,2026-06-13;SH.600519 5m V 型
    底中枢 z2 dd=1322 gg=1565,L1 kuozhan 中枢全丢)。correct_exit 因 min_body=3 对
    3 段中枢剥不动离开段,故在此为反转判定单独剥:末段确为离开段(终点更远离核心区)
    且剥后≥2 段时,用剩余段的 [min low, max high]。退化(无 lines/不足/末段非离开)→
    用 zs.dd/zs.gg,与旧行为一致(4 段及以上中枢的离开段已由 correct_exit 剥除)。
    """
    lines = getattr(z, "lines", None)
    if lines and len(lines) >= 3:
        last = lines[-1]
        if last.start is not None and last.end is not None:
            zd, zg = z.zd, z.zg
            d_end = 0.0 if zd <= last.end.val <= zg else min(
                abs(last.end.val - zd), abs(last.end.val - zg))
            d_start = 0.0 if zd <= last.start.val <= zg else min(
                abs(last.start.val - zd), abs(last.start.val - zg))
            if d_end > d_start:                       # 末段终点更远离核心区 → 离开段,剥除
                body = lines[:-1]
                return (min(ln.zs_low for ln in body), max(ln.zs_high for ln in body))
    return (z.dd, z.gg)


class ZslxBranchCalculator:
    """级别无关的走势类型划分（基于 zs_branch 中枢+内联背驰）。

    输出是 done_zss 的**纯函数**(done_divergence v1 不参与边界,见 calculate;且无
    zs.type 等副作用,区别于 legacy ZslxCalculator)。done_zss 已 identity 稳定、
    append-only frozen(进入/离开段校正身份缓存),跨 K 多数不变 → 按值签名 memo
    「边界划分计划」(swing_segments+subsplit+trend_dir,占本调用 ~82% 的 classify_rel
    密集部分);ZSLX 仍每次 _finalize **新建**,语义与全量重算等价(对象新鲜、无跨 K
    共享突变——chart 旧链路会就地写 zslx.zs_type_mmds,故不缓存 wts 本身)。
    增量等价由 tests/chan_core/test_incremental_equivalence 递归层对拍守。"""

    def __init__(self) -> None:
        # 计划缓存:sig(done_zss) → [(a, b, swing_dir, trend_dir), …]。命中即跳过
        # swing_segments/subsplit/trend_dir 全部 classify_rel 重扫(纯函数,只依赖 done_zss)。
        self._plan_cache: dict = {}
        # done 中枢 zs_sig 弱键缓存:done 中枢几何冻结,签名计算一次即复用,使每次
        # _signature 仅 O(中枢数) 廉价查表(不重建每中枢 line_sig)。WeakKeyDictionary:
        # 中枢 GC 时自动剔除、不污染 ZS.__dict__(快照/pickle 无感),ZS 默认 id-hash 可弱键。
        self._zs_sig_cache: "WeakKeyDictionary[ZS, tuple]" = WeakKeyDictionary()

    def __getstate__(self):
        # WeakKeyDictionary 不可 pickle 且为瞬态(可重建),序列化时丢弃;_plan_cache
        # 键是值签名(非身份),反序列化后仍有效,可保留。
        state = self.__dict__.copy()
        state.pop("_zs_sig_cache", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        if "_zs_sig_cache" not in self.__dict__:
            self._zs_sig_cache = WeakKeyDictionary()
        if "_plan_cache" not in self.__dict__:
            self._plan_cache = {}

    @staticmethod
    def _line_sig(line: LINE) -> tuple:
        return (
            getattr(line, "type", None),
            getattr(getattr(line.start, "k", None), "index", None),
            getattr(getattr(line.end, "k", None), "index", None),
            line.start.val,
            line.end.val,
            bool(getattr(line, "done", False)),
        )

    def _zs_sig(self, zs: ZS) -> tuple:
        """单中枢值签名:忠实覆盖 calculate 读到的全部几何——
        scalar(level/done/zd/zg/dd/gg)、进入段 zs.start / 离开段 zs.end(均为 LINE 或
        None)、core lines 全段(classify_rel 取 lines[:3]、_swing_body 取 lines[:-1]、
        _finalize 取 lines[0]/[-1]) → 等签名 ⟺ 等几何 ⟺ 等输出。done 中枢弱键缓存。"""
        done = bool(getattr(zs, "done", False))
        cache = self._zs_sig_cache
        if done:
            hit = cache.get(zs)
            if hit is not None:
                return hit
        lines = getattr(zs, "lines", None) or []
        entry = getattr(zs, "start", None)   # 进入段(LINE 或 None)
        leave = getattr(zs, "end", None)     # 离开段(LINE 或 None)
        sig = (
            getattr(zs, "level", None),
            done,
            getattr(zs, "zd", None), getattr(zs, "zg", None),
            getattr(zs, "dd", None), getattr(zs, "gg", None),
            None if entry is None else self._line_sig(entry),
            None if leave is None else self._line_sig(leave),
            len(lines),
            tuple(self._line_sig(ln) for ln in lines),
        )
        if done:
            cache[zs] = sig
        return sig

    def _signature(self, done_zss: List[ZS]) -> tuple:
        return tuple(self._zs_sig(zs) for zs in done_zss)

    @staticmethod
    def _finalize(
        zss: List[ZS], start_idx: int, cur_dir: Optional[str], done: bool,
        swing_dir: Optional[str] = None,
    ) -> ZSLX:
        """把一个中枢列表收尾成 ZSLX：分类、边界(含进入/离开段 a/b)、包络。

        盘整段方向(_type) = **净位移·转折点口径**(进入段起点→离开段起点)：原文 line25179
        Ai 严格交替按涨跌定向；段端点=转折点(L25128 段起点=前段结束点/L8131 a1=b1 共享
        端点)，离开段跨越转折属下一段。两类实测病(fix/zhongshu-l0,2026-06-11)：
        ①继承摆动腿方向 swing_dir——up 腿尾部「高位横盘+暴跌收尾」标 up(SH.000001 5m)；
        ②净位移用离开段**终点**——600519 5m down 腿 1428→离开段终点1565 翻成 up。
        净位移缺失/零位移时 fallback swing_dir，再退化内部段净位移——v31 反对的「内部段
        位移反号」是旧核心段口径的病，fallback 顺位仍保留其教训。
        """
        # 走势类型边界 = 第一中枢进入段 a → 末中枢离开段 b（原文 a+A+b），缺则退化用核心段
        first = zss[0].start if zss[0].start is not None else zss[0].lines[0]
        last = zss[-1].end if zss[-1].end is not None else zss[-1].lines[-1]
        if cur_dir in ("trend_up", "trend_down"):
            # 趋势：≥2 依次同向(本体分离)中枢。
            direction = "up" if cur_dir == "trend_up" else "down"
            zslx_type = "上涨" if direction == "up" else "下跌"
        else:
            # 仅由中枢扩张(expand,本体相交)连接、无趋势方向 → 盘整。
            zslx_type = "盘整"
            # 净位移终点=**离开段起点**(转折点口径:L25128 段起点=前段结束点/L8131
            # a1=b1 共享端点):离开段跨越转折、属下一段,用其终点会翻号(600519 down 腿
            # zslx 1428→离开段终点 1565 错标 up;取离开段起点→down ✓)。无离开段
            # (fallback lines[-1] 为本体段)时用该段终点。
            s_val = getattr(first.start, "val", None) if first.start is not None else None
            if zss[-1].end is not None:
                e_val = getattr(last.start, "val", None)
            else:
                e_val = getattr(last.end, "val", None)
            if s_val is not None and e_val is not None and s_val != e_val:
                direction = "up" if e_val > s_val else "down"   # 净位移=Ai 涨跌(L25179)
            elif swing_dir in ("up", "down"):
                direction = swing_dir
            else:
                first_seg, last_seg = zss[0].lines[0], zss[-1].lines[-1]
                direction = "up" if last_seg.end.val >= first_seg.start.val else "down"
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
        bodies = [_swing_body(z) for z in zss]            # 剔除离开段远摆的本体包络(见 _swing_body)
        bounds: List[tuple] = []
        start = 0
        ext_idx = 0                                       # 当前趋势极值中枢索引
        # 初始方向:首两中枢本体中点净位移
        D = "down" if (bodies[1][0] + bodies[1][1]) < (bodies[0][0] + bodies[0][1]) else "up"
        for i in range(1, n):
            z_lo, z_hi = bodies[i]
            e_lo, e_hi = bodies[ext_idx]
            if D == "down":
                if z_lo < e_lo:                           # 创新低 → 下跌延续,更新极值
                    ext_idx = i
                elif z_lo > e_hi:                         # 反向中枢本体脱离谷中枢本体 → 反转
                    bounds.append((start, ext_idx, "down"))
                    start, D = ext_idx + 1, "up"
                    ext_idx = max(range(start, i + 1), key=lambda k: bodies[k][1])
            else:                                          # up
                if z_hi > e_hi:                           # 创新高 → 上涨延续
                    ext_idx = i
                elif z_hi < e_lo:                         # 反向中枢本体脱离峰中枢本体 → 反转
                    bounds.append((start, ext_idx, "up"))
                    start, D = ext_idx + 1, "down"
                    ext_idx = min(range(start, i + 1), key=lambda k: bodies[k][0])
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
        # 边界划分计划(swing_segments+subsplit+trend_dir)是 done_zss 纯函数、占本调用
        # ~82%(classify_rel 密集);按值签名 memo,跨 K 不变即命中跳过全部重扫。trend_dir
        # 并入计划(亦 classify_rel),命中时连同 a/b/swing_dir 一并复用。
        sig = self._signature(done_zss)
        plan = self._plan_cache.get(sig)
        if plan is None:
            plan = []
            for s, e, sdir in self._swing_segments(done_zss):
                for a, b in self._subsplit(done_zss, s, e):   # 子段继承摆动腿方向
                    plan.append((a, b, sdir, self._trend_dir(done_zss[a:b + 1])))
            if len(self._plan_cache) > 64:                    # 多级共用一实例,留余量(见 __init__)
                self._plan_cache.clear()
            self._plan_cache[sig] = plan
        # ZSLX 每次新建(语义同全量重算:对象新鲜、无跨 K 共享突变)。
        last = len(plan) - 1
        wts: List[ZSLX] = []
        for k, (a, b, sdir, tdir) in enumerate(plan):
            wts.append(self._finalize(
                done_zss[a:b + 1], a, tdir, done=(k < last), swing_dir=sdir))
        return wts
