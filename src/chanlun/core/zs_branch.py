"""中枢多假设结构核。

单级别、以确定性线段为输入，产出「冻结的已完成中枢 + 右边缘多假设分支池」。
本模块不依赖、也不改动 zs_calculator.py 与任何生产链路（零回归风险）。

口径：
- 成中枢的重叠用严格 `<`（ZD<ZG 才算非退化重叠）。
- 延伸/扩张的「触及」用闭区间 `<=`（触边即算）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from weakref import WeakKeyDictionary

from chanlun.core.types import LINE, ZS, Config
from chanlun.core.zs_calculator import ZsCalculator
from chanlun.core.beichi_calculator import is_beichi, is_qs, LdProvider


def core_interval(seg_a: LINE, seg_b: LINE, seg_c: LINE) -> Optional[Tuple[float, float]]:
    """前三段重叠的核心区间 [ZD, ZG]。

    ZD=max(三段低), ZG=min(三段高)；严格 ZD<ZG 才算非退化重叠，否则 None。

    几何基元：calculate 已委托 ZsCalculator 找中枢、本函数当前未接线，留作
    下游延伸/扩张判定复用。
    """
    zd = max(seg_a.zs_low, seg_b.zs_low, seg_c.zs_low)
    zg = min(seg_a.zs_high, seg_b.zs_high, seg_c.zs_high)
    if zd >= zg:
        return None
    return (zd, zg)


def envelope(lines: List[LINE]) -> Tuple[float, float]:
    """中枢包络 [DD, GG]：DD=min(所有段低), GG=max(所有段高)（含瞬间波动区间）。"""
    dd = min(ln.zs_low for ln in lines)
    gg = max(ln.zs_high for ln in lines)
    return (dd, gg)


def touches(seg: LINE, lo: float, hi: float) -> bool:
    """线段是否触及闭区间 [lo, hi]（延伸/扩张口径：触边即算，用 ≥/≤）。

    几何基元：同 ``core_interval``，当前未接线，留作下游复用。
    """
    return max(seg.zs_low, lo) <= min(seg.zs_high, hi)


def body_envelope(zs: ZS) -> Tuple[float, float]:
    """中枢本体包络 (DD, GG)：只取前 3 段定义段，剔除延伸/离开段的远摆。

    若用完整 gg/dd（含离开段），离开段总朝下一中枢延伸 → 相邻中枢包络恒重叠
    → 趋势恒判不出。故趋势/扩张判定必须用本体包络。无 lines 时退化用
    zs.dd/zs.gg（测试/边界）。
    """
    if not zs.lines:
        return (zs.dd, zs.gg)
    return envelope(zs.lines[:3])


def _wave_envelope(zs: ZS) -> Tuple[float, float]:
    """波动区间 [DD, GG] = 全部本体段包络(离开段已由 correct_exit 剥出)。

    原文中心定理(L020):GG=max(gn)/DD=min(dn),n 遍历中枢内**所有** Z 段(含延伸段)。
    done+校正后中枢的 zs.gg/zs.dd 正是该口径;gg/dd 缺失(裸桩/测试)退化 body_envelope。
    """
    dd = getattr(zs, "dd", None)
    gg = getattr(zs, "gg", None)
    if dd is None or gg is None:
        return body_envelope(zs)
    return (dd, gg)


def classify_rel(prev: ZS, cur: ZS) -> str:
    """相邻中枢关系三分类——对齐原文中心定理二(L020:39)。

    「后 GG<前 DD ⟺ 下跌及其延续;后 DD>前 GG ⟺ 上涨及其延续;后 ZG<前 ZD 且
    后 GG≥前 DD(或对称) ⟺ 形成高级别的走势中枢」。
    - 波动区间 GG/DD 完全分离 → "trend_up"/"trend_down";
    - 否则 → "expand":核心区[ZD,ZG]分离而波动重叠=定理二升级情形(高级别中枢),
      核心区重叠=延伸族(上游 _merge_overlapping_cores 已并、残余同归)。当前消费方
      (zslx 分段/升级标注)对两种非趋势情形同策,故不再细分;升级实体化时再按
      ZG/ZD 细分(audit O3b)。
    旧实现用「前 3 段本体包络」——延伸段波动被错误剔除(原文 GG 遍历含延伸段),
    升级情形会被误判为趋势,已按定理二字面订正(audit O3a, 2026-07-06)。
    """
    p_dd, p_gg = _wave_envelope(prev)
    c_dd, c_gg = _wave_envelope(cur)
    if c_dd > p_gg:
        return "trend_up"
    if c_gg < p_dd:
        return "trend_down"
    return "expand"


def _zone_dist(v: float, zd: float, zg: float) -> float:
    """价 v 到中枢区间 [zd,zg] 的距离（区间内为 0）。"""
    if zd <= v <= zg:
        return 0.0
    return min(abs(v - zd), abs(v - zg))


def correct_entry(zs: ZS, min_lines: int = 4) -> ZS:
    """进入段校正：剔除被误并入中枢的进入段。

    委托的 ZsCalculator 用「第一根与 [ZD,ZG] 几何重叠的段即核心」的口径，会把
    方向性升/跌入段误当中枢第一段。正确口径：进入段必须朝中枢走、升/跌进区间；
    若引擎认的进入段 ``zs.start`` 背离区间（其终点比起点离区间更远），则它不是真
    进入段——真进入段是 ``zs.lines[0]`` 那根升/跌入段，中枢起点右移到 ``lines[1]``，
    ``zd/zg`` 由新前三段重算。

    返回校正后的中枢（背离才动，否则原样返回）。
    """
    s = zs.start
    if s is None or s.start is None or s.end is None:
        return zs                                  # 开头中枢无进入段可测
    if len(zs.lines) < min_lines + 1:
        return zs                                  # 移除首段后不足成中枢，保守不动
    if _zone_dist(s.end.val, zs.zd, zs.zg) <= _zone_dist(s.start.val, zs.zd, zs.zg):
        return zs                                  # 进入段朝中枢走 → 引擎认对了
    new_core = zs.lines[1:]                         # 背离 → lines[0] 才是真进入段
    iv = core_interval(new_core[0], new_core[1], new_core[2])
    if iv is None:
        return zs
    z = copy.copy(zs)
    z.start = zs.lines[0]                           # 真进入段 = 升/跌入段
    z.lines = list(new_core)
    z.zd, z.zg = iv
    z._bounds_dirty = True
    z.update_boundaries()                           # 重算 gg/dd 包络
    return z


def correct_exit(zs: ZS, min_body: int = 3) -> ZS:
    """离开段剥离（对称于 correct_entry；离开段是独立次级别段，不属中枢本体）。

    委托的 ZsCalculator 把离开段计入 ``lines``（为"第4段确认第3段完成"的最小 4 段
    口径），但离开段是确认/出口、不是中枢本体。done 中枢的 ``lines[-1]`` 即离开段
    （定向冲出区间）→ 剥出本体：``lines = lines[:-1]``，离开段记为 ``z.end``；box /
    gg/dd 用本体。本体保底 ``min_body`` 段（中枢最小 = 3 个走势类型重叠）。
    """
    if not zs.done or len(zs.lines) <= min_body:
        return zs                                  # 未完成 / 剥后不足本体 → 不动
    last = zs.lines[-1]
    if last.start is None or last.end is None:
        return zs
    if _zone_dist(last.end.val, zs.zd, zs.zg) <= _zone_dist(last.start.val, zs.zd, zs.zg):
        return zs                                  # 末段没冲出区间 → 不是离开段，不剥
    z = copy.copy(zs)
    z.lines = list(zs.lines[:-1])                  # 本体（剥掉离开段）
    z.end = last                                   # 离开段（本就该是 z.end）
    z._bounds_dirty = True
    z.update_boundaries()                          # gg/dd 收紧到本体
    return z


@dataclass
class DivergenceResult:
    """一个中枢离开段的背驰判定结果。"""

    is_beichi: bool                   # 是否背驰
    kind: str                         # "qs"(趋势背驰) | "pz"(盘整背驰)
    compare_seg: LINE                 # 比较段 = 中枢进入段 z.start
    leave_seg: LINE                   # 离开段
    provisional: bool                 # 右边缘未坐实(True) / 已固化(False)


@dataclass
class ZsHypothesis:
    """右边缘的一个中枢读法（一个 live 分支）。"""

    zs: ZS                            # 该读法下的中枢对象
    node1: str                        # "core"(末段为核心/延伸) | "leave"(末段为离开段/完成)
    rel_prev: Optional[str] = None    # "trend_up"|"trend_down"|"expand"|None(无前中枢)
    upgrade: bool = False             # True=已达 9 段触发升级（只标记，不实体化）
    divergence: Optional[DivergenceResult] = None   # 离开段背驰(core 读法恒 None)


@dataclass
class ZsBranchResult:
    """单级别一次 calculate 的产出。"""

    done_zss: List[ZS]                # 左侧已冻结的已完成中枢
    live: List[ZsHypothesis]          # 右边缘活分支（通常 1~2 个）
    freeze_idx: int                   # 冻结边界：< freeze_idx 的线段已 settled；live 分支从此起
    done_divergence: List[Optional[DivergenceResult]] = field(default_factory=list)  # 与 done_zss 索引对齐


class ZsBranchCalculator:
    """单级别多假设中枢引擎（薄 manager）。

    「找中枢」委托给已验证的 ``ZsCalculator``——look-ahead 完成判定、进入段提升、
    扫描定位都在那里解决。本类只在其 pending 中枢上加右边缘 core/leave 分叉 +
    本体包络分类；已完成中枢即左侧冻结。

    结构层：leave 读法表示「中枢结构完成」。段数封顶暂不启用（与 9 段升级标记有
    交互）：``max_zs_lines`` 给极大值，让中枢能长到 ≥9 段以触发升级标记。
    """

    _NO_CAP = 10 ** 9     # 暂不封顶

    def __init__(
        self,
        ld_provider: Optional[LdProvider] = None,
        frequency: Optional[str] = None,
        wzgx: str = Config.ZS_WZGX_GD.value,
        min_zs_lines: int = 4,
        pending_min_lines=None,
    ):
        """``ld_provider`` 缺省时不判背驰（退化为纯结构）。

        ``wzgx`` 默认 GD（GG/DD 严格口径，与 CL_CFG 默认一致；生产链路均由调用方
        显式传入，默认值只影响裸构造）。``min_zs_lines`` = 底层 ZsCalculator 的
        center.lines 长度下限(**不含进入段**):递归链现全级别传 5(经
        cl._recursive_l0_min_zs_lines);「level0=4/level≥1=3」为已废历史口径,默认 4 仅裸构造。

        中枢段数不封顶(_NO_CAP)：曾试在第 8 段封顶，但逐标的·逐中枢·逐线段核验证伪
        并撤回——封顶会在第 8 段强制收口、把一个长盘整劈成两个核心区重叠的同级别中枢，
        与「相邻同级别中枢核心区不能重叠」「回抽重回中枢内=未破坏=一个延伸中枢」冲突。
        故保持不封顶：长盘整=一个延伸中枢；延伸超 9 段且内部有结构时由递归层(走势类型
        →level1)/zs_upgrade 升级，不在 level0 劈裂。要显式封顶可给内部 ZsCalculator
        传 max_zs_lines。
        """
        self.ld_provider = ld_provider
        self.frequency = frequency
        self.wzgx = wzgx
        self.min_zs_lines = min_zs_lines
        # 持久 ZsCalculator(递归层增量化):跨 calculate 复用 → 其 identity-LCP 增量
        # (_prefix_stable_restart)在 identity 稳定输入(L0 = xds/bis 浅拷贝、同对象前缀)
        # 上生效,取代原每次 new 实例的全量重建。require_alternation 含义见下方原注释。
        # 调用方(RecursiveBranchCalculator)须按「级别/塔」分实例持久,以免输入抖动。
        self._zc = ZsCalculator(
            require_alternation=True,
            min_zs_lines=min_zs_lines,
            max_zs_lines=self._NO_CAP,
            pending_min_lines=pending_min_lines,   # O2 披露门透传(None=零行为)
        )
        # done 中枢离开段背驰缓存(值签名 key):稳定中枢的 a/c 段+prev 不变 → is_beichi
        # (含 query_macd_ld)结果冻结、跨 K 复用,免对所有中枢每 K 重判背驰。见 _divergence_for。
        self._div_cache: dict = {}
        # 进入/离开段校正缓存(按底层中枢身份):稳定中枢冻结(correct_* 返副本不 mutate 底层、
        # ZsCalculator 稳定前缀冻结)→ 校正结果确定、跨 K 复用,免对所有中枢每 K 重算。WeakKeyDict
        # 弱键中枢 GC 自动剔除;未改动存哨兵(而非中枢本身)以免强值钉住弱键。见 _corrected。
        self._correct_cache: "WeakKeyDictionary[ZS, object]" = WeakKeyDictionary()

    def calculate(self, lines: List[LINE]) -> ZsBranchResult:
        if not lines:
            return ZsBranchResult(done_zss=[], live=[], freeze_idx=0, done_divergence=[])
        # 复用持久 ZsCalculator(见 __init__):identity 稳定输入走增量,否则其内部自动
        # 降级全量(identity-LCP d=0)。require_alternation=True:中枢=3 段方向交替重叠区;
        # level0(线段)天然交替此为 no-op,level≥1(走势类型)构成段不必然交替,
        # 关掉会把『连续 3 个同向走势类型』价格重合处硬挤成假中枢。
        zc = self._zc
        zc.calculate(lines)
        # 进入段/离开段校正：把误当核心的升/跌入段(进入段)、定向冲出的
        # 离开段从中枢本体剥出（进入段 → z.start，离开段 → z.end）
        done: List[ZS] = [self._corrected(z) for z in zc.zss]
        done = self._merge_overlapping_cores(done, lines)   # 并入核心区重叠的相邻中枢
        pending: Optional[ZS] = zc.pending_zs    # 右边缘进行中中枢（单解，无离开段）
        if pending is not None:
            pending = correct_entry(pending, self.min_zs_lines)
        # 不变量(原文 L018:9「前三个…都是完成的才构成中枢」):done 中枢不含未完成构成段。
        # 由扫描结构隐含保证——未完成段只能是序列末段,它要么进 pending、要么作为
        # 「下一段不重叠」的触发者被排除在 done 中枢外(2026-07-06 探针实证双塔全级零违反)。
        for z in done:                           # 合法性不变量（防回归）：本体最小 3 段
            assert len(z.lines) >= 3 and z.zd < z.zg
        done_div: List[Optional[DivergenceResult]] = [
            self._divergence_for(z, done[i - 1] if i > 0 else None, live=False)
            for i, z in enumerate(done)
        ]
        if pending is None:
            return ZsBranchResult(
                done_zss=done, live=[], freeze_idx=len(lines), done_divergence=done_div
            )
        prev = done[-1] if done else None
        live = self._fork_pending(pending, prev)
        for h in live:
            if h.node1 == "leave":                    # 仅离开读法判背驰
                h.divergence = self._divergence_for(h.zs, prev, live=True)
        return ZsBranchResult(
            done_zss=done,
            live=live,
            freeze_idx=self._line_index(pending.lines[0], lines),
            done_divergence=done_div,
        )

    @staticmethod
    def _line_index(target: LINE, lines: List[LINE]) -> int:
        """pending 中枢第一段在 lines 中的下标（按对象身份）。"""
        for k, ln in enumerate(lines):
            if ln is target:
                return k
        return len(lines)

    def _merge_overlapping_cores(self, done: List[ZS], lines: List[LINE]) -> List[ZS]:
        """并入相邻「核心区 [ZD,ZG] 重叠」的同级别中枢。

        中枢破坏当且仅当一次级别走势离开中枢后、其后回抽不重新回到中枢内。引擎
        `_extend_and_check_complete` 只做了第一步(一段离开核心区即收口+从离开段起找
        下一中枢)、漏了第二步(回看回抽是否重回)——把「冲出又回抽」切成两个核心区重叠
        的中枢。两中枢核心区重叠 ⟺ 回抽重回前中枢内 ⟺ 中枢未破坏=应是一个延伸中枢。
        故在此把核心区重叠的相邻 done 中枢并入前者(保留前者核心区——中枢核心由首三段定、
        合并不改之)，离开/连接/后中枢段并作其延伸；gg/dd 重算到合并后全段。

        仅合并直接重叠对、最大合并组=2(保留组首核心区→后中枢移开即止、不级联巨型中枢)，
        合并后相邻核心区重叠对→0。贪心 O(n)、纯函数(inc==batch 在输出层保持)。
        """
        if len(done) < 2:
            return done
        merged: List[ZS] = [done[0]]
        for z in done[1:]:
            head = merged[-1]
            if (head.zd is not None and z.zd is not None
                    and max(head.zd, z.zd) < min(head.zg, z.zg)):
                i0 = self._line_index(head.lines[0], lines)
                i_last = self._line_index(z.lines[-1], lines) if z.lines else len(lines)
                if i0 < len(lines) and i_last < len(lines) and i_last >= i0:
                    m = copy.copy(head)
                    m.lines = list(lines[i0:i_last + 1])      # 本体延伸到后中枢末体段
                    m.end = z.end if z.end is not None else z.lines[-1]   # 离开段取后中枢的
                    m.zd, m.zg = head.zd, head.zg             # 核心区不变(由首三段确定)
                    m._bounds_dirty = True
                    m.update_boundaries()                     # gg/dd/line_num 重算到全段
                    merged[-1] = m
                    continue
            merged.append(z)
        return merged

    @staticmethod
    def _leave_seg(zs: ZS, live: bool) -> Optional[LINE]:
        """中枢离开段：live 分支取末段 lines[-1]；done 中枢取剥出的 z.end
        （correct_exit 已把定向冲出的离开段剥到 z.end），z.end 缺失时退化用末段。"""
        if live:
            return zs.lines[-1] if zs.lines else None
        if zs.end is not None:
            return zs.end
        return zs.lines[-1] if zs.lines else None

    def _is_trend(self, prev_zs: Optional[ZS], zs: ZS, leave: LINE) -> bool:
        """本中枢与前一中枢是否依次同向构成趋势，且趋势方向 == 离开段方向。

        is_qs 的 use_core_envelope 仅在 GD 口径下生效（用前 3 段本体剔离开段远摆）；
        ZGD/ZGGDD 口径用 zd/zg 核心区间、本就无远摆问题。传 True 是为兼容调用方把
        wzgx 配成 GD 的情形。无前中枢 → 非趋势（按盘整背驰处理）。
        """
        if prev_zs is None:
            return False
        if zs.zd is None or zs.zg is None or prev_zs.zd is None or prev_zs.zg is None:
            return False
        d = is_qs(prev_zs, zs, self.wzgx, use_core_envelope=True)
        return d is not None and d == leave.type

    _DIV_MISS = object()             # 背驰缓存"未命中"哨兵(区分缓存的 None 结果)
    _CORRECT_UNCHANGED = object()    # 校正缓存"无改动"哨兵(见 _corrected)

    def _corrected(self, z: ZS) -> ZS:
        """correct_entry/exit 的身份缓存包装(见 __init__):底层中枢冻结时跨 K 复用校正结果。
        未改动(correct_* 原样返回 z)时缓存哨兵、返回 z,以免弱键被强值(z 本身)钉住不 GC。"""
        hit = self._correct_cache.get(z)
        if hit is not None:
            return z if hit is self._CORRECT_UNCHANGED else hit
        done_z = correct_exit(correct_entry(z, self.min_zs_lines))
        self._correct_cache[z] = self._CORRECT_UNCHANGED if done_z is z else done_z
        return done_z

    @staticmethod
    def _div_key(zs: ZS, c: LINE, prev_zs: Optional[ZS]):
        """done 背驰缓存值签名:a(进入段 zs.start)/c(离开段)/zs 区间 + prev 背驰相关状态。
        稳定中枢这些值冻结 → key 稳定命中;活跃尾部 c 延伸 → key 变 → 重算。MACD 对过去区间
        固定,故值签名充分决定 is_beichi(a,c) 与 _is_trend(prev) 结果。"""
        def seg(s):
            if s is None or s.start is None or s.end is None:
                return None
            return (s.type, s.start.k.k_index, s.end.k.k_index, s.start.val, s.end.val)
        pz = None if prev_zs is None else (seg(prev_zs.end), prev_zs.zd, prev_zs.zg)
        # E-LOW-4: 补 lines[:3] 本体包络——zd/zg(核心区)相同但本体几何不同的中枢理论上可碰撞
        #          致缓存陈旧 kind(fuzz 实测 0 分叉、不可达, 防御性加精);body 冻结不破稳定中枢命中。
        body = tuple(seg(l) for l in (zs.lines or [])[:3])
        return (seg(zs.start), seg(c), zs.zd, zs.zg, body, pz)

    def _divergence_for(
        self, zs: ZS, prev_zs: Optional[ZS], live: bool
    ) -> Optional[DivergenceResult]:
        """对中枢判离开段背驰（is_beichi 原语直连）。

        a = 进入段 z.start（趋势时即连接段）；c = 离开段。盘整与趋势两种情形在每个
        中枢上计算同一 = is_beichi(z.start, 离开段)，仅 kind 标签不同。
        无 ld_provider / 无进入段 / 进入段与离开段异向 → None。
        """
        if self.ld_provider is None:
            return None
        a = zs.start
        c = self._leave_seg(zs, live)
        if (a is None or a.start is None or a.end is None
                or c is None or c.start is None or c.end is None):
            return None
        # done 背驰值签名缓存:稳定中枢(a/c/prev 冻结)跨 K 命中,免重判 is_beichi/query_macd_ld;
        # live(pending)或活跃尾部(c 延伸 → key 变)不命中、照算。
        key = None
        if not live:
            key = self._div_key(zs, c, prev_zs)
            cached = self._div_cache.get(key, self._DIV_MISS)
            if cached is not self._DIV_MISS:
                return cached
        if a.type != c.type:                          # 转折型(进入/离开异向=趋势转折点)
            # 转折前趋势的背驰:前中枢同向离开段 vs 本中枢进入段 a(转折前趋势最后段)
            b = prev_zs.end if prev_zs is not None else None
            if (b is None or b is a or b.type != a.type
                    or b.start is None or b.end is None):
                result = None                          # 无前驱/自比/异向/缺端点 → 不判
                # (值相等异实例的 b/a 由 is_beichi 创新高/低严格不等号前提兜底,不会误判)
            else:
                bc = is_beichi(b, a, self.ld_provider, self.frequency)
                result = DivergenceResult(
                    is_beichi=bc, kind="qs",           # 转折=趋势背驰
                    compare_seg=b, leave_seg=a, provisional=live,  # 背驰段=进入段 a
                )
        else:
            kind = "qs" if self._is_trend(prev_zs, zs, c) else "pz"
            bc = is_beichi(a, c, self.ld_provider, self.frequency)
            result = DivergenceResult(
                is_beichi=bc, kind=kind, compare_seg=a, leave_seg=c, provisional=live
            )
        if key is not None:
            if len(self._div_cache) > 4096:
                self._div_cache.clear()
            self._div_cache[key] = result
        return result

    def _fork_pending(self, pending: ZS, prev: Optional[ZS]) -> List[ZsHypothesis]:
        """在 pending 中枢上分叉：core=中枢仍开(done=False)，leave=末段为离开段(done=True)。

        两分支各自独立拷贝（含独立 lines 容器），互不串台——也不别名委托引擎的
        pending 对象；下游若对某分支实体化/延伸 lines 不会污染另一分支或上游。
        """
        upgrade = len(pending.lines) >= 9        # 9 段触发升级（只标记）
        rel = classify_rel(prev, pending) if prev is not None else None
        h1 = self._branch_copy(pending, done=False)   # 仍开
        h2 = self._branch_copy(pending, done=True)    # 完成读法
        return [
            ZsHypothesis(zs=h1, node1="core", rel_prev=rel, upgrade=upgrade),
            ZsHypothesis(zs=h2, node1="leave", rel_prev=rel, upgrade=upgrade),
        ]

    @staticmethod
    def _branch_copy(zs: ZS, done: bool) -> ZS:
        """分支用的独立中枢拷贝：独立 lines 容器（元素 LINE 只读、可继续共享），置 done。"""
        z = copy.copy(zs)
        z.lines = list(zs.lines)
        z.done = done
        return z
