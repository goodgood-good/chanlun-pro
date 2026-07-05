# -*- coding: utf-8 -*-
"""
三类买卖点识别引擎

识别 1/2/3 类买卖点。

复用 ``cl.beichi_zs_oscillation`` / ``cl.beichi_qs`` / ``cl.zss_is_qs`` 做背驰与趋势判定，
通过 ``LINE.add_mmd`` / ``LINE.add_bc`` 把识别结果挂到笔/线段上。
"""
from __future__ import annotations

import bisect
from typing import List, Optional, Tuple, TYPE_CHECKING

from chanlun.core.types import LINE, ZS
from chanlun.tools.log_util import LogUtil

if TYPE_CHECKING:
    # 避免 cl.py <-> bs_point_calculator.py 的循环 import
    from chanlun.core.cl import CL


class BsPointCalculator:
    """
    三类买卖点识别引擎

    使用方式（在 ``cl.py::process_mmd()`` 中编排）::

        bi_calc = BsPointCalculator(self, zs_type='bi')
        bi_calc.calculate(self.bi_calculator.bis, self.bis_zss_calculator.zss)

        xd_calc = BsPointCalculator(self, zs_type='xd')
        xd_calc.calculate(self.xd_calculator.xds, self.zss_calculator.zss)

    :param cl: ``CL`` 主类引用，用于复用 ``beichi_zs_oscillation`` / ``beichi_qs`` / ``zss_is_qs``
    :param zs_type: 中枢类型，``'bi'`` 表示笔层，``'xd'`` 表示线段层。决定结果挂在哪一层。
    :param strict_3_mode: 三买判定模式。
                         ``True`` = 严格定义（反抽必须严格 < ZG）；
                         ``False`` = 允许 ≤ ZG（默认，实战口径）
    :param min_signal_interval: 1 类信号最小间隔（按 xd index 距离）。
                               同方向 + 同对照中枢的 1buy/1sell 相邻两次 xd index 差 < 此值
                               时只保留第一个，避免「一波趋势背驰被密集多次报点」。
                               默认 10（约对应同一波背驰的窗口长度）。
                               设为 0 关闭过滤（保留全部信号）。
                               注：仅作用于 1 类信号，2/3 类有自己的对照逻辑，密度天然较低。
    """

    def __init__(
        self,
        cl: 'CL',
        zs_type: str = 'xd',
        strict_3_mode: bool = False,
        min_signal_interval: int = 10,
    ):
        # 'bi'/'xd' 为基础级别；'L{n}'(如 'L0'/'L1')为递归升级级别——买卖点跑在
        # 递归各级走势单元上时用独立分桶,不与 bi/xd 基础买卖点冲突。
        if not zs_type or not (zs_type in ('bi', 'xd') or zs_type.startswith('L')):
            raise ValueError(
                f"zs_type 必须是 'bi'/'xd' 或 'L{{n}}'(递归升级级别), 当前传入: {zs_type}"
            )
        if min_signal_interval < 0:
            raise ValueError(
                f"min_signal_interval 必须 >= 0, 当前传入: {min_signal_interval}"
            )
        self.cl = cl
        self.zs_type = zs_type
        self.strict_3_mode = strict_3_mode
        self.min_signal_interval = min_signal_interval

        # 增量化:bi 层(bis 换新列表→identity 前缀)+ xd 层(xds 原地改→长度 buffer
        # 前缀,见 _bsp_restart_point)持久实例启用;L 层 + fresh 实例无前轮状态恒走全量 P=0。
        # xd 层 legacy 买卖点写 zs_type_mmds['xd']、不被 recursive_branch 消费(后者用独立
        # L 级 BsPointCalculator),故增量安全。3 检测器跨轮累积状态改为实例字段,并按「提交
        # 该条目的 line.index」记录值;增量重启时过滤掉 value >= 重启点 P 的尾部条目即复原到
        # 「as-of P」(检测器全 backward-only:一条线的买卖点只依赖其之前的线/中枢/状态,故
        # 前缀稳定 + 状态复原即等价全量)。
        self._inc_enabled = (zs_type in ('bi', 'xd'))
        self._st_1buy: dict = {}   # (type, ref_zs.index) -> 最近 1 类信号 line.index
        self._st_2buy: dict = {}   # (anchor.type, anchor.index) -> 该锚点 2 类回抽 line.index
        self._st_3buy: dict = {}   # (zs.index, name) -> 首次回抽/回拉 line.index(含失败触碰)
        self._last_lines_obj = None
        self._last_zss_obj = None
        self._pool = None          # 按方向的 1 类信号线池(增量维护,见 _build_1mmd_pools_inc)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def calculate(self, lines: List[LINE], zss: List[ZS]) -> None:
        """
        主入口：按 1buy → 2buy → 3buy 顺序识别买卖点，结果直接写回 ``LINE.zs_type_mmds``。

        :param lines: 本级别的所有线段（or 笔）
        :param zss: 本级别已识别的中枢列表（来自 ``ZsCalculator.calculate``）
        """
        if not lines:
            self._reset_bsp_state()
            self._last_lines_obj = None
            self._last_zss_obj = None
            return

        # 增量重启点 P(仅 bi 层持久实例可 >0):lines[0:P] 的买卖点/背驰可原样保留
        # (3 检测器 backward-only + 前缀 lines/zss 稳定 + 状态复原到 as-of P)。P=0 即
        # 退化为原「无状态全量重算」。BsPointCalculator 原每轮全清重算、增量结果恒等于
        # 一次性全量;持久化 + 状态复原后改为只重放活跃尾部 [P:],等价不变(对拍网守)。
        P = self._bsp_restart_point(lines, zss) if zss else 0
        if P > 0:
            self._clear_previous_results(lines, start=P)   # 只清尾部,前缀 mmd 保留
            self._restore_bsp_state(P)                     # 状态复原到 as-of P
        else:
            self._clear_previous_results(lines)
            self._reset_bsp_state()
        # 记录本轮输入对象供下次增量 identity 比对(zss 空也更新,避免下次 LCP 失真)。
        self._last_lines_obj = lines
        self._last_zss_obj = zss

        if not zss:
            return

        # 预过滤 + 预计算 start_keys, 把 valid_zss 切片从 O(M) 降到 O(log M)。
        # ZsCalculator 输出的 zss 按 zs.start.start.k.k_index 严格升序。
        # 注意: 不能因 clean_zss 为空就 early return —— 2buy 条件 A (now.low >=
        # prev_1line.low) 不依赖 zss, 必须跑完整套 detect。
        clean_zss, start_keys = self._build_zss_index(zss)

        # 顺序不可调整：2buy 依赖 1buy 已被识别（通过 mmd_exists 反查）。
        # start=P:增量时只从重启点起重放(前缀检测结果已保留、状态已复原)。
        self._detect_1buy_1sell(lines, zss, clean_zss, start_keys, start=P)
        self._detect_2buy_2sell(lines, zss, clean_zss, start_keys, start=P)
        self._detect_3buy_3sell(lines, zss, clean_zss, start_keys, start=P)

    def _clear_previous_results(self, lines: List[LINE], start: int = 0) -> None:
        """清空 ``lines`` 上 ``self.zs_type`` 维度的旧买卖点与背驰。

        ``BsPointCalculator`` 的结果只写入 ``LINE.zs_type_mmds`` /
        ``zs_type_bcs`` 这两个按 zs_type 分桶的字典,故只需清空对应桶。

        前提契约:``zs_type_mmds['bi'/'xd']`` / ``zs_type_bcs['bi'/'xd']``
        这两个桶由 ``BsPointCalculator`` 独占写入。若将来有其他写入者往
        同名桶写入自定义买卖点,此处整桶清空会静默吞掉它们 —— 那种场景
        应改用独立的 zs_type key。
        """
        for line in lines[start:]:
            old_mmds = list(getattr(line, 'zs_type_mmds', {}).get(self.zs_type, []))
            old_bcs = list(getattr(line, 'zs_type_bcs', {}).get(self.zs_type, []))
            if getattr(line, 'default_zs_type', None) == self.zs_type:
                if old_mmds and hasattr(line, 'mmds'):
                    old_mmd_ids = {id(mmd) for mmd in old_mmds}
                    line.mmds = [mmd for mmd in line.mmds if id(mmd) not in old_mmd_ids]
                if old_bcs and hasattr(line, 'bcs'):
                    old_bc_ids = {id(bc) for bc in old_bcs}
                    line.bcs = [bc for bc in line.bcs if id(bc) not in old_bc_ids]
            line.zs_type_mmds[self.zs_type] = []
            line.zs_type_bcs[self.zs_type] = []

    # ------------------------------------------------------------------
    # ① 增量重启:重启点 + 状态复原
    # ------------------------------------------------------------------
    @staticmethod
    def _identity_prefix_len(new_seq, old_seq) -> int:
        """二分求 new/old 序列的 identity 公共前缀长度 O(log n)。

        依赖上游 rebuild 契约(bi 笔/bi_zss 完成中枢均「换新列表/对象、共享前缀身份
        保留」):一次变更只换连续后缀对象,故 ``new[i] is old[i]`` 单调,可二分。
        """
        if old_seq is None:
            return 0
        lo, hi = 0, min(len(new_seq), len(old_seq))
        while lo < hi:
            mid = (lo + hi) // 2
            if new_seq[mid] is old_seq[mid]:
                lo = mid + 1
            else:
                hi = mid
        return lo

    @staticmethod
    def _zs_anchor_k(zs: ZS):
        """中枢时间锚 k_index(进入段起点;无进入段用首核心段起点),与 _build_zss_index
        一致。取不到返回 None。"""
        start_line = zs.start
        if start_line is None and getattr(zs, 'lines', None):
            start_line = zs.lines[0]
        if start_line is None or start_line.start is None:
            return None
        try:
            return start_line.start.k.k_index
        except AttributeError:
            return None

    def _reset_bsp_state(self) -> None:
        self._st_1buy = {}
        self._st_2buy = {}
        self._st_3buy = {}

    def _restore_bsp_state(self, p: int) -> None:
        """复原 3 检测器状态到「as-of P」:只保留 value(提交时 line.index) < P 的条目,
        丢弃上轮尾部 [P:] 的贡献(那些线本轮重算)。"""
        self._st_1buy = {k: v for k, v in self._st_1buy.items() if v < p}
        self._st_2buy = {k: v for k, v in self._st_2buy.items() if v < p}
        self._st_3buy = {k: v for k, v in self._st_3buy.items() if v < p}

    def _bsp_restart_point(self, lines: List[LINE], zss: List[ZS]) -> int:
        """增量重启点 P:lines[0:P] 买卖点可保留。仅 bi 层持久实例可 >0,否则 0(全量)。

        约束取交集(全 backward-only,无需前瞻 buffer,仅留 1 段防 off-by-one):
          - bis identity 公共前缀 d_lines(bi_calculator 变更换新列表、前缀对象身份保留);
          - zss 稳定:首个「非 identity 稳定」中枢(含每轮变化的 pending)的时间锚 k 之后
            的线,其 valid_zss 触及该变化中枢 → 不安全,P 须 ≤ 这些线之前。
        任一前提不满足(首次/对象未换/锚取不到)→ 0,安全退化全量。
        """
        if not self._inc_enabled or self._last_lines_obj is None:
            return 0
        if lines is self._last_lines_obj:
            # xds 原地改(同列表对象,末段 done/end 翻转或原地 append):identity 前缀失效
            # (对象身份不变、值已改),改用长度 buffer 定稳定前缀(删末2段容纳 xd 回溯 d≤2,
            # 实测上界2、buffer=0),fall through 做 zss 稳定检查 + off-by-one。bi 层 bis
            # 恒换新列表、不走此支。
            p = max(0, len(lines) - 2)
            # 运行时哨兵(对齐 xd/zs):复用前缀依赖倒数第3段 xd(p-1)已 done;
            # 若它仍 pending(回溯深度疑≥3),len-2 不足 → return 0 全量重启(恒正确)。
            if p >= 1 and not getattr(lines[p - 1], "done", True):
                return 0
        else:
            p = self._identity_prefix_len(lines, self._last_lines_obj)
        if self._last_zss_obj is not None and zss is not self._last_zss_obj:
            d_zss = self._identity_prefix_len(zss, self._last_zss_obj)
            if d_zss < len(zss):
                thr = self._zs_anchor_k(zss[d_zss])
                if thr is None:
                    return 0
                # bis 的 end.k.k_index 随笔序单调升;从 min(p,len)-1 向前找首个 < thr 的笔,
                # 其位置+1 即「valid_zss 全在稳定中枢前缀内」的安全线数上界。
                bound = 0
                for i in range(min(p, len(lines)) - 1, -1, -1):
                    try:
                        ek = lines[i].end.k.k_index
                    except AttributeError:
                        return 0
                    if ek < thr:
                        bound = i + 1
                        break
                p = min(p, bound)
        return max(0, min(p, len(lines)) - 1)

    # bisect 二分辅助 (供 _detect_* 复用)
    @staticmethod
    def _build_zss_index(zss: List[ZS]) -> Tuple[List[ZS], List[int]]:
        """预过滤 zss + 抽时间锚点 k_index 升序数组, 供 bisect 二分。

        开头中枢可以没有进入段(``ZS.start is None``)。这种中枢仍是合法中枢,
        用第一条核心线段的起点作为时间锚；只有连核心线段起点也不可用时才跳过。
        剩下 clean_zss 与 start_keys 同长度且顺序一致。
        """
        clean_zss: List[ZS] = []
        start_keys: List[int] = []
        for zs in zss:
            start_line = zs.start
            if start_line is None and getattr(zs, 'lines', None):
                start_line = zs.lines[0]
            if start_line is None or start_line.start is None:
                continue
            clean_zss.append(zs)
            start_keys.append(start_line.start.k.k_index)
        return clean_zss, start_keys

    def _find_subordinate_1mmd_in_window(
        self, now_line: LINE, target_type: str, end_tolerance: int = 0
    ) -> Optional[LINE]:
        """xd 层用:在 ``now_line`` 末端查找同向的
        次级别(笔层) 1 类买卖点所挂的笔。

        - 仅在 ``self.zs_type == 'xd'`` 时启用——bi 层无更细次级别。
        - 默认只接受落在 ``now_line.end.k.k_index`` 的次级别 1 类点；
          ``end_tolerance`` 可显式放宽末端容差。
        - 同向:``bi.type == target_type``;mmd 名 = ``1buy``(down)/``1sell``(up)。
        - 命中即返回该笔;无命中返回 None。
        """
        if self.zs_type != 'xd':
            return None
        target_mmd = '1buy' if target_type == 'down' else '1sell'
        try:
            start_k = now_line.start.k.k_index
            end_k = now_line.end.k.k_index
        except AttributeError:
            return None
        end_tolerance = max(0, end_tolerance)
        lower_bound = max(start_k, end_k - end_tolerance)
        found: Optional[LINE] = None
        for bi in self.cl.get_bis():
            if bi.type != target_type:
                continue
            try:
                bi_end_k = bi.end.k.k_index
            except AttributeError:
                continue
            if not (lower_bound <= bi_end_k <= end_k):
                continue
            mmds = getattr(bi, 'zs_type_mmds', {}).get('bi', [])
            if any(m.name == target_mmd for m in mmds):
                found = bi
        return found

    @staticmethod
    def _build_sub_1mmd_index(bis: List[LINE]) -> dict:
        """预建「带次级别(笔层) 1 类信号的笔」索引,供 ``_sub_1mmd_from_index``
        O(log) 窗口查找,消除 ``_find_subordinate_1mmd_in_window`` 的 per-line
        全量笔扫描 O(xds×bis)。仅 xd 层用(bi 层无更细次级别,不建)。

        按笔方向分桶:每桶只收「``bi.type == 桶向`` 且挂有该向 1 类信号
        (down→``1buy`` / up→``1sell``)」的笔,与 scan 的命中条件逐字对齐。
        笔的 ``end.k.k_index`` 沿 ``get_bis()`` 顺序严格递增(相邻笔共享端点
        分型),故各桶 keys 天然升序、可直接 bisect。等价性见
        ``tests/chan_core/test_bs_point_sub1mmd.py``。

        :return: ``{'down': (keys, bis), 'up': (keys, bis)}``,keys 与 bis 同长对齐。
        """
        out = {'down': ([], []), 'up': ([], [])}
        for bi in bis:
            t = getattr(bi, 'type', None)
            if t not in ('down', 'up'):
                continue
            try:
                bi_end_k = bi.end.k.k_index
            except AttributeError:
                continue
            target_mmd = '1buy' if t == 'down' else '1sell'
            mmds = getattr(bi, 'zs_type_mmds', {}).get('bi', [])
            if any(m.name == target_mmd for m in mmds):
                keys, lst = out[t]
                keys.append(bi_end_k)
                lst.append(bi)
        return out

    @staticmethod
    def _sub_1mmd_from_index(
        index: dict, now_line: LINE, target_type: str, end_tolerance: int = 0
    ) -> Optional[LINE]:
        """``_find_subordinate_1mmd_in_window`` 的索引版(O(log) 等价)。

        在 ``index[target_type]`` 上 bisect 出落在窗口
        ``[max(start_k, end_k-tol), end_k]`` 内的笔,取窗口内末端 K 最大的一根
        (= scan 的「最后命中覆盖 found」语义,因 keys 升序即遍历序)。无则 None。
        """
        if target_type not in ('down', 'up'):
            return None
        try:
            start_k = now_line.start.k.k_index
            end_k = now_line.end.k.k_index
        except AttributeError:
            return None
        end_tolerance = max(0, end_tolerance)
        lower_bound = max(start_k, end_k - end_tolerance)
        keys, lst = index[target_type]
        hi = bisect.bisect_right(keys, end_k)
        lo = bisect.bisect_left(keys, lower_bound)
        if hi > lo:
            return lst[hi - 1]
        return None

    @staticmethod
    def _breaks_last_zs(now_line: LINE, last_zs: ZS) -> bool:
        """now_line 是否在几何上「跌破/突破最后一个走势中枢」。

        判据三条全部满足才算「跌破末中枢」：
          1. 末中枢已完成(``last_zs.done is True`` 且 ``last_zs.end`` 非 None)——
             pending 中枢的延伸区间还在变,无法定位「跌破点」;
          2. ``now_line`` 不在末中枢内部段之前——``now_line.index >= last_zs.end.index``
             涵盖两种合法位置:其一 now_line 本身就是末中枢的离开段(``last_zs.end``,
             已并入中枢核心)、其二 now_line 在离开段之后(脱离中枢后的延续段);
          3. ``now_line`` 的 low/high 突破末中枢延伸区间 dd/gg——
             ``low > last_zs.dd`` (下跌) / ``high < last_zs.gg`` (上涨)表示
             仍在中枢瞬间波动内,非「跌破」。等号(``=``)留给「now_line 自身
             制造 dd/gg」的离开段——它就是「跌破」的那一段。

        旧实现完全没有几何判据,只靠 ``beichi_qs`` 力度衰减 + 「创新低/高 vs A 段」
        识别,会把末中枢内部段(low 还在 dd 之上)误识别为 1 买/1 卖。
        """
        if not last_zs.done or last_zs.end is None:
            return False
        if now_line.index < last_zs.end.index:
            return False
        if now_line.type == 'down':
            return now_line.low <= last_zs.dd
        return now_line.high >= last_zs.gg

    @staticmethod
    def _filter_valid_zss_by_now_end_k(
        clean_zss: List[ZS],
        start_keys: List[int],
        now_end_k: int,
        require_end_complete: bool = False,
    ) -> List[ZS]:
        """按 zs.start.start.k.k_index < now_end_k 切片 (bisect O(log M))。

        前提: ``start_keys`` 严格升序 (来自 ``_build_zss_index``)。

        Args:
            clean_zss: 已预过滤的 zss
            start_keys: 与 clean_zss 同长的 start.k.k_index 升序数组
            now_end_k: 当前线段结束 K index
            require_end_complete: True 时额外过滤 zs.end.end.k.k_index < now_end_k
                                  (用于 3buy/3sell, 要求中枢必须已完成 end 段)
        """
        idx = bisect.bisect_left(start_keys, now_end_k)
        prefix = clean_zss[:idx]
        if not require_end_complete:
            return prefix
        return [
            zs for zs in prefix
            if zs.end is not None and zs.end.end is not None
            and zs.end.end.k.k_index < now_end_k
        ]

    # ------------------------------------------------------------------
    # 第一类买卖点
    # ------------------------------------------------------------------
    def _detect_1buy_1sell(
        self,
        lines: List[LINE],
        zss: List[ZS],
        clean_zss: Optional[List[ZS]] = None,
        start_keys: Optional[List[int]] = None,
        start: int = 0,
    ) -> None:
        """
        第一类买卖点 = 趋势背驰（直接复用 ``cl.beichi_qs``）

        判定条件：
            1. 至少 2 个同向中枢，构成 'up' / 'down' 趋势（``cl.zss_is_qs`` 已校验）
            2. 当前线段方向与趋势方向一致（``cl.beichi_qs`` 已校验）
            3. 当前线段相对"进入前一个中枢的同方向线段"力度衰减（``cl.beichi_qs`` 已校验）

        命名规则：
            - 下跌趋势末段背驰 → ``1buy``
            - 上涨趋势末段背驰 → ``1sell``

        :param lines: 本级别全部线段
        :param zss: 本级别已识别的中枢列表
        """
        if len(zss) < 2:
            # cl.beichi_qs 内部已防御 len(zss)<2，但提前 short-circuit 可省一次循环
            return

        # 信号最小间隔过滤：记录每个 (type, zs_index) 维度上"最近一次"已识别的
        # 1 类信号 xd index，丢弃间隔 < min_signal_interval 的后续同类信号，
        # 避免同一波趋势背驰被密集多次报点。
        # 增量:复用实例级持久状态(已由 _restore_bsp_state 复原到 as-of start)。
        last_signal_xd_idx = self._st_1buy

        for now_line in lines[start:]:
            # 防未来函数：每个 now_line 只能用自身形成前已存在的中枢判定，否则
            # beichi_qs 内部 zss[-2:] 会让历史 xd 用未来中枢做对照（事后追认），
            # 同一 xd 在不同快照下被反复识别。切片规则：zs.start 进入段起点 K
            # 索引 < now_line 结束 K 索引。bisect 把这步从 O(M) 降到 O(log M)。
            now_end_k = now_line.end.k.k_index
            valid_zss = self._filter_valid_zss_by_now_end_k(
                clean_zss, start_keys, now_end_k
            )
            if len(valid_zss) < 2:
                continue

            # 几何判据：1 类买卖点 = 跌破/突破最后一个走势中枢后形成的背驰点。
            # 仅力度衰减不够,还须确认 now_line 几何上脱离末中枢：末中枢须已完成、
            # now_line 是末中枢的离开段或在其后、且 low/high 突破延伸最低/最高(dd/gg)。
            if not self._breaks_last_zs(now_line, valid_zss[-1]):
                continue

            # 复用 cl.beichi_qs（内部已做趋势校验 + 力度对比）
            try:
                is_bc, compare_lines = self.cl.beichi_qs(lines, valid_zss, now_line)
            except Exception as e:
                # 力度对比依赖 MACD，缺数据时跳过单根线段，不阻断整批
                LogUtil.warning(
                    f"[BsPointCalculator] beichi_qs 异常: line.index={now_line.index}, err={e}"
                )
                continue

            if not is_bc or not compare_lines:
                continue

            # 趋势背驰必须创新低/高：beichi_qs 仅做 MACD 力度衰减比较，未校验价格
            # 几何条件，需补「创新低/高 + 力度衰减」复合条件。
            base_line = compare_lines[0]
            if now_line.type == 'down' and now_line.low >= base_line.low:
                continue  # 下跌段未创新低 → 不构成 1buy
            if now_line.type == 'up' and now_line.high <= base_line.high:
                continue  # 上涨段未创新高 → 不构成 1sell

            # 命名：下跌段背驰 → 一买；上涨段背驰 → 一卖
            mmd_name = '1buy' if now_line.type == 'down' else '1sell'

            # 对照中枢统一用 valid_zss[-1]（now_line 时间位置上的实际末中枢），
            # 不能用全量 zss[-1]：后者对历史线段是"未来才形成的中枢"。
            ref_zs = valid_zss[-1]

            # 去重：同一线段同一名字不重复挂（增量计算保护）
            if self._mmd_already_attached(now_line, mmd_name, ref_zs):
                continue

            # 信号最小间隔过滤：上一次同方向 + 同对照中枢的 1 类信号若在
            # min_signal_interval 个 xd 内则跳过本次（保留更早那个）。
            # key 必须用 valid_zss[-1].index（实际对照中枢），不是全量 zss[-1]。
            if self.min_signal_interval > 0:
                key = (now_line.type, valid_zss[-1].index)
                last_idx = last_signal_xd_idx.get(key)
                if last_idx is not None and (now_line.index - last_idx) < self.min_signal_interval:
                    LogUtil.debug(lambda:
                        f"[BsPointCalculator] {mmd_name} 因最小间隔过滤跳过: "
                        f"line.index={now_line.index}, last_idx={last_idx}, "
                        f"interval={now_line.index - last_idx} < {self.min_signal_interval}"
                    )
                    continue
                # 无论后续 add_mmd 是否真的写入（_mmd_already_attached 已防重复），
                # 这里都先记录本次为"最近一次信号"
                last_signal_xd_idx[key] = now_line.index

            # 写入背驰
            now_line.add_bc(
                _type='qs',
                zs=ref_zs,
                compare_line=compare_lines[0],
                compare_lines=compare_lines,
                bc=True,
                zs_type=self.zs_type,
            )
            # 写入买卖点（zs 挂在 now_line 时间位置上的末中枢，符合区间套语义）
            now_line.add_mmd(
                name=mmd_name,
                zs=ref_zs,
                zs_type=self.zs_type,
                msg=(
                    f'趋势背驰：当前 {now_line.type} 段相对 '
                    f'index={compare_lines[0].index} 的同向段力度衰减'
                ),
            )
            LogUtil.debug(lambda:
                f"[BsPointCalculator] 识别到 {mmd_name}: line.index={now_line.index}, "
                f"compare_idx={compare_lines[0].index}, zs.index={ref_zs.index}"
            )

    # ------------------------------------------------------------------
    # 第二类买卖点
    # ------------------------------------------------------------------
    def _detect_2buy_2sell(
        self,
        lines: List[LINE],
        zss: List[ZS],
        clean_zss: Optional[List[ZS]] = None,
        start_keys: Optional[List[int]] = None,
        start: int = 0,
    ) -> None:
        """
        第二类买卖点 = 一类买卖点后的反抽再回调（不创新低/高），或盘整背驰

        **路径 1(仅 xd 层)**: 本级别第二类买卖点由次级别相应走势的第一类买点
        构成——xd 层 ``now_line`` 末端若挂有同向次级别(笔层) 1 类信号,直接识别为
        本级别 2 类。优先于经验法。要求 ``process_mmd`` 先跑 bi 层(已在 cl.py 中保证)。

        **路径 2·经验法兜底**: 判定条件：
            1. 当前线段往前能找到同方向的 1buy / 1sell（必须先调 ``_detect_1buy_1sell``）
            2. 一买/一卖与当前段之间至少隔了 1 段反向走势（构成"反抽 → 再回调"）
            3. 满足以下任一条件：
               - **条件 A（强）**：当前段的低/高未突破一买的低/高 → 标准二买
               - **条件 B（弱）**：当前段虽创新低/高，但与一买所在段构成盘整背驰
                 （复用 ``cl.beichi_zs_oscillation``,中枢震荡口径）

        命名规则：
            - 下跌段（一买后再下跌）未破前低 / 盘整背驰 → ``2buy``
            - 上涨段（一卖后再上涨）未破前高 / 盘整背驰 → ``2sell``

        :param lines: 本级别全部线段
        :param zss: 本级别已识别的中枢列表
        """
        # 必须 lines 非空才有反查空间
        if not lines:
            return

        # 2 买 = 1 买后首次次级别回抽：每个 1 买锚点只产一次 2 买，
        # 后续不破前低的回抽属中枢震荡，不再重复记 2 买（否则一波震荡里一个 1 买
        # 会刷出十几个 2 买）。key 改 (anchor.type, anchor.index) 业务键(增量跨轮稳定,
        # 不用 id);value = 该锚点 2 买回抽 line.index。复用实例级持久状态(已复原到 as-of start)。
        attached_2_anchors = self._st_2buy

        # 增量化(消除 _find_recent_1mmd_lines per-line 倒扫 O(n²)):1 类信号此时已
        # 全挂好且本方法不改它 → 预建按方向的 1mmd 线池一次,循环内 O(log) 查询。
        # 等价性见 tests/chan_core/test_bs_point_1mmd.py。
        _1mmd_pools, _1mmd_keys = self._build_1mmd_pools_inc(lines, start)

        # 路径1(仅 xd 层):预建「带笔层 1 类的笔」索引一次,消除
        # _find_subordinate_1mmd_in_window per-line 全量笔扫 O(xds×bis)。bi 层无
        # 次级别 → 不建,后续 sub_1bi 恒 None(与 scan 在 bi 层恒返回 None 等价)。
        # bi 此刻已定(bi 层 calculate 先于 xd 层跑完),循环内不变,故可外提。
        _sub_1mmd_index = (
            self._sub_index_from_bi_pool()
            if self.zs_type == 'xd' else None
        )

        for i in range(start, len(lines)):
            now_line = lines[i]
            if i < 2:
                # 二买/二卖至少需要 1 个一买 + 1 段反抽 + 当前段 = 3 段
                continue

            # ---- 路径 1 ----
            # xd 层时,优先看次级别(笔层) 1 类是否落在 now_line 末端,有则
            # 直接产出本级别 2 类,跳过经验法。(索引版,等价于 scan 见上)
            sub_1bi = (
                self._sub_1mmd_from_index(_sub_1mmd_index, now_line, now_line.type)
                if _sub_1mmd_index is not None else None
            )
            if sub_1bi is not None:
                now_end_k = now_line.end.k.k_index
                valid_zss_dl1 = self._filter_valid_zss_by_now_end_k(
                    clean_zss, start_keys, now_end_k
                )
                if valid_zss_dl1:
                    ref_zs_dl1 = valid_zss_dl1[-1]
                    mmd_name_dl1 = '2buy' if now_line.type == 'down' else '2sell'
                    if not self._mmd_already_attached(now_line, mmd_name_dl1, ref_zs_dl1):
                        now_line.add_mmd(
                            name=mmd_name_dl1,
                            zs=ref_zs_dl1,
                            zs_type=self.zs_type,
                            msg=(
                                f'2类买卖点: 次级别(笔) {sub_1bi.index} '
                                f'挂 1 类 → 本级别 2 类'
                            ),
                        )
                        LogUtil.debug(lambda:
                            f"[BsPointCalculator] 识别到 {mmd_name_dl1}: "
                            f"line.index={now_line.index}, sub_bi.index={sub_1bi.index}, "
                            f"zs.index={ref_zs_dl1.index}"
                        )
                        continue  # 已识别,跳过经验法

            # ---- 路径 2·经验法兜底 ----
            # 扫描最近 3 个同向 1 类信号各自尝试构建 2buy/2sell：一波趋势中可
            # 有多个 1buy 锚点，后续回踩对照其中任一个都应允许识别。
            prev_1lines = self._recent_1mmd_from_pool(
                _1mmd_pools, _1mmd_keys, now_line.type, i, max_count=3
            )
            if not prev_1lines:
                continue

            # 准备 valid_zss（条件 B 用，提到外面避免每个 prev 重复计算）。
            # 防未来函数：cl.beichi_zs_oscillation 用 zss[-1] 做震荡对照，必须按 now_line
            # 时间位置切片 zss，避免历史 xd 用未来中枢做对照。
            now_end_k = now_line.end.k.k_index
            valid_zss_2 = self._filter_valid_zss_by_now_end_k(
                clean_zss, start_keys, now_end_k
            )

            # 对每个候选 prev_1line 尝试判定（找到第一个命中的就跳出）
            for prev_1line in prev_1lines:
                # 校验：一买与当前段之间至少有 1 段反向走势
                if i - prev_1line.index < 2:
                    continue
                # 首次回抽约束：该 1 买锚点已产过 2 买 → 跳过(后续回抽属中枢震荡)
                if (prev_1line.type, prev_1line.index) in attached_2_anchors:
                    continue

                # ---- 条件 A：不创新低 / 不创新高（强条件，边界宽容：>= / <=）----
                mmd_name: Optional[str] = None
                msg = ""
                if now_line.type == 'down' and now_line.low >= prev_1line.low:
                    mmd_name = '2buy'
                    msg = (
                        f'一买后反抽再下跌未创新低: '
                        f'now.low={now_line.low} >= prev_1line.low={prev_1line.low}'
                    )
                elif now_line.type == 'up' and now_line.high <= prev_1line.high:
                    mmd_name = '2sell'
                    msg = (
                        f'一卖后反抽再上涨未创新高: '
                        f'now.high={now_line.high} <= prev_1line.high={prev_1line.high}'
                    )

                # ---- 条件 B：创新低/高但盘整背驰（弱条件，双中枢对照）----
                if mmd_name is None and valid_zss_2:
                    # 优先用末中枢，失败再尝试次末中枢（覆盖更多场景）
                    candidates_zs = [valid_zss_2[-1]]
                    if len(valid_zss_2) >= 2:
                        candidates_zs.append(valid_zss_2[-2])
                    for ref_zs_for_pz in candidates_zs:
                        try:
                            is_pz_bc, _ = self.cl.beichi_zs_oscillation(ref_zs_for_pz, now_line)  # 中枢震荡口径(原 beichi_pz 旧语义, 审计 F4 拆分后 legacy 行为保持不变)
                        except Exception as e:
                            LogUtil.warning(
                                f"[BsPointCalculator] beichi_zs_oscillation 异常: "
                                f"line.index={now_line.index}, zs.index={ref_zs_for_pz.index}, err={e}"
                            )
                            is_pz_bc = False
                        if is_pz_bc:
                            mmd_name = '2buy' if now_line.type == 'down' else '2sell'
                            msg = (
                                f'一买后创新低但盘整背驰（对照 zs.index={ref_zs_for_pz.index}），'
                                f'归为二买/二卖'
                            )
                            break

                if mmd_name is None:
                    continue

                # 二买的 zs 字段挂在"一买所在中枢"上（保持语义一致：mmd.zs 表示参考的中枢）
                ref_zs = self._find_mmd_zs_on_line(prev_1line, target_name=(
                    '1buy' if now_line.type == 'down' else '1sell'
                ))
                if ref_zs is None:
                    # 防御：找不到一买的中枢就用切片后的最近一个中枢兜底
                    ref_zs = valid_zss_2[-1] if valid_zss_2 else None
                if ref_zs is None:
                    continue  # 无任何中枢可挂，放弃

                # 去重（增量计算保护）
                if self._mmd_already_attached(now_line, mmd_name, ref_zs):
                    break  # 已挂过同名 mmd，无需再尝试更早的 prev_1line

                now_line.add_mmd(
                    name=mmd_name,
                    zs=ref_zs,
                    zs_type=self.zs_type,
                    msg=msg,
                )
                attached_2_anchors[(prev_1line.type, prev_1line.index)] = now_line.index  # 标记该锚点已产 2 买(首次回抽)
                LogUtil.debug(lambda:
                    f"[BsPointCalculator] 识别到 {mmd_name}: line.index={now_line.index}, "
                    f"prev_1line.index={prev_1line.index}, msg={msg}"
                )
                break  # 已成功挂载，跳过更早的 prev_1line（避免同 xd 重复 2buy）

    # ------------------------------------------------------------------
    # 二买的辅助方法
    # ------------------------------------------------------------------
    def _find_recent_1mmd_lines(
        self, prev_lines: List[LINE], target_type: str, max_count: int = 3
    ) -> List[LINE]:
        """
        从 ``prev_lines`` 中倒序查找最近 ``max_count`` 根挂了 ``1buy`` 或 ``1sell``
        的同方向线段（多锚点 2buy 识别，一波趋势中可有多个一买锚点）。

        返回顺序：从最近到最远（``[最近, 次近, 更早, ...]``）

        :param prev_lines: 候选线段列表（必须不含当前 now_line）
        :param target_type: ``'down'`` 找 1buy；``'up'`` 找 1sell
        :param max_count: 最多返回多少根（默认 3 根，平衡召回率和性能）
        :return: 同方向 1 类信号线段列表（最多 max_count 个）
        """
        target_mmd_name = '1buy' if target_type == 'down' else '1sell'
        result: List[LINE] = []
        for line in reversed(prev_lines):
            if line.type != target_type:
                continue
            mmds = getattr(line, 'zs_type_mmds', {}).get(self.zs_type, [])
            if any(mmd.name == target_mmd_name for mmd in mmds):
                result.append(line)
                if len(result) >= max_count:
                    break
        return result

    @staticmethod
    def _build_1mmd_pools(lines: List[LINE], zs_type: str):
        """预建「按方向的 1 类信号线池」(index 升序),供 _recent_1mmd_from_pool
        O(log) 查询,消除 _find_recent_1mmd_lines 的 per-line 倒扫 O(n²)。
        'down' 池 = 挂 1buy 的 down 线;'up' 池 = 挂 1sell 的 up 线。
        前提:调用时 1 类信号已全部挂好(_detect_1buy_1sell 先跑)且后续不改。"""
        pools = {'down': [], 'up': []}
        for line in lines:
            mmds = getattr(line, 'zs_type_mmds', {}).get(zs_type, [])
            if line.type == 'down':
                if any(m.name == '1buy' for m in mmds):
                    pools['down'].append(line)
            elif line.type == 'up':
                if any(m.name == '1sell' for m in mmds):
                    pools['up'].append(line)
        keys = {k: [line.index for line in v] for k, v in pools.items()}
        return pools, keys

    def _build_1mmd_pools_inc(self, lines: List[LINE], start: int):
        """_build_1mmd_pools 的增量包装(消除每轮 O(n) 全量建池):
        start>0 时复用上轮 self._pool 的稳定前缀(index<start 且 lines[index] 仍是该对象,
        后一条件剔除上轮尾部已被重建的 stale 笔)+ 对 lines[start:] 全量建池后并接;
        否则全量。1 类信号 index 升序天然有序,并接后仍升序。"""
        prev = self._pool
        if start <= 0 or prev is None:
            pools, keys = self._build_1mmd_pools(lines, self.zs_type)
        else:
            n = len(lines)
            pools = {
                d: [l for l in prev.get(d, []) if l.index < start and l.index < n and lines[l.index] is l]
                for d in ('down', 'up')
            }
            tail, _ = self._build_1mmd_pools(lines[start:], self.zs_type)
            pools['down'].extend(tail['down'])
            pools['up'].extend(tail['up'])
            keys = {d: [l.index for l in pools[d]] for d in ('down', 'up')}
        self._pool = pools
        return pools, keys

    def _sub_index_from_bi_pool(self) -> dict:
        """xd 层路径1用「带 bi 层 1 类的笔」按 end_k 升序索引。bi 层(process_mmd 中先跑)
        已把这些笔收进 cl._bi_bsp._pool(增量维护),直接复用其稀疏池建 end_k 键(O(signals)),
        取代每轮 self.cl.get_bis() 的 O(bis) 全量重扫(并绕开 get_bis 的 deepcopy)。
        池缺失(bi 层尚无中枢/未启用)时回退原全量 scan。"""
        bi_bsp = getattr(self.cl, '_bi_bsp', None)
        bi_pool = getattr(bi_bsp, '_pool', None) if bi_bsp is not None else None
        if bi_pool is None:
            return self._build_sub_1mmd_index(self.cl.get_bis())
        out = {}
        for d in ('down', 'up'):
            keys, bis_ = [], []
            for b in bi_pool.get(d, []):
                try:
                    keys.append(b.end.k.k_index)
                except AttributeError:
                    continue
                bis_.append(b)
            out[d] = (keys, bis_)
        return out

    @staticmethod
    def _recent_1mmd_from_pool(pools, keys, target_type: str, before_index: int, max_count: int = 3):
        """等价于 _find_recent_1mmd_lines(lines[:before_index], target_type, max_count):
        取 target_type 池中 index < before_index 的最近 max_count 根,最近在前。"""
        pool = pools.get(target_type, [])
        if not pool:
            return []
        cut = bisect.bisect_left(keys[target_type], before_index)
        return pool[max(0, cut - max_count):cut][::-1]

    def _find_mmd_zs_on_line(
        self, line: LINE, target_name: str
    ) -> Optional[ZS]:
        """
        在 ``line`` 上找 ``target_name`` 对应的 MMD，并返回其 ``zs`` 字段。

        :param line: 已知挂有目标 MMD 的线段
        :param target_name: ``'1buy'`` / ``'1sell'`` 等
        :return: ``MMD.zs``，或 ``None``
        """
        mmds = getattr(line, 'zs_type_mmds', {}).get(self.zs_type, [])
        for mmd in mmds:
            if mmd.name == target_name:
                return mmd.zs
        return None

    # ------------------------------------------------------------------
    # 第三类买卖点
    # ------------------------------------------------------------------
    def _detect_3buy_3sell(
        self,
        lines: List[LINE],
        zss: List[ZS],
        clean_zss: Optional[List[ZS]] = None,
        start_keys: Optional[List[int]] = None,
        start: int = 0,
    ) -> None:
        """
        第三类买卖点 = 中枢形成后，离开中枢的次级别走势 + 反抽不回中枢区间 [ZG, ZD]

        判定条件：
            1. 存在已完成的中枢（``zs.done == True`` 且 ``zs.real == True``）
            2. 当前线段位于中枢离开段之后（``now_line.start.index >= zs.end.end.index``）
            3. 当前线段方向与离开段方向相反（即"反抽"）
            4. 反抽幅度未触及中枢区间：
               - 中枢向上离开 → 反抽 ``low > zs.zg`` → ``3buy``
               - 中枢向下离开 → 反抽 ``high < zs.zd`` → ``3sell``

        :param lines: 本级别所有线段
        :param zss: 本级别中枢列表

        **重要**:并不是中枢上方的任何回调回抽都是第三类买卖点,必须是第一次。
        一个中枢只能产生一个 3 买、最多再加一个 3 卖,后续不破 ZG 的反抽不重复挂。
        `first_return_seen` 字典以 (zs.index, mmd_name) 为键确保 first-touch 语义。
        旧实现对每个不破 ZG 的反抽都识别,在密集震荡行情下 3 类信号会指数级爆炸。
        """
        # first-touch 守卫:每个中枢每种 3 类信号只处理第一次回抽/回拉。
        # key = (zs.index, mmd_name); value = 首次回抽/回拉的 now_line.index。
        # 复用实例级持久状态(已复原到 as-of start);含「失败触碰」项故不可从 mmds 重建。
        first_return_seen = self._st_3buy

        # 增量化(消除 O(n²)):related_zs == _find_related_zs_for_3rd 的结果 ==
        # 「zs.end.index 最大且 < now_line.index(cond-C)」的结构合格中枢。
        # 在严格递进的线序里 zs.end.index < now_line.index ⇒ zs 的 start/end 的
        # k_index 必 < now_line.end.k.k_index(now_end_k),故 cond-C 已 subsume 原
        # _filter_valid_zss_by_now_end_k 的 cond-S/cond-E(require_end_complete 防未来
        # 函数);防未来函数语义不变。结构合格中枢按 end.index 升序,每根 now_line 用
        # bisect O(log M) 取「< now_line.index 的最大 end.index」,取代 per-line 全扫
        # valid_zss(原 O(n²) 主因)。行为由大网 SH.600519_5m(809笔/83个3类点)钉死。
        _src_zss = clean_zss if clean_zss is not None else zss
        _elig = sorted(
            (zs for zs in _src_zss
             if zs.done and zs.real and zs.end is not None and zs.end.end is not None),
            key=lambda zs: zs.end.index,
        )
        _elig_end_idx = [zs.end.index for zs in _elig]

        for now_line in lines[start:]:
            _k = bisect.bisect_left(_elig_end_idx, now_line.index)
            related_zs = _elig[_k - 1] if _k > 0 else None
            if related_zs is None:
                continue

            if related_zs.end is None:
                continue
            if related_zs.end.type == 'up' and now_line.type == 'down':
                expected_mmd_name = '3buy'
            elif related_zs.end.type == 'down' and now_line.type == 'up':
                expected_mmd_name = '3sell'
            else:
                continue

            # First-touch semantics: the first return after leaving a centre
            # consumes the third-kind opportunity even when it fails the ZG/ZD test.
            ftkey = (related_zs.index, expected_mmd_name)
            if ftkey in first_return_seen:
                LogUtil.debug(lambda:
                    f"[BsPointCalculator] {expected_mmd_name} first return for zs[{related_zs.index}] "
                    f"was line[{first_return_seen[ftkey]}], skip line[{now_line.index}]"
                )
                continue
            first_return_seen[ftkey] = now_line.index

            mmd_name, msg = self._judge_3rd_bs_point(now_line, related_zs)
            if mmd_name is None:
                continue

            # 去重：同一线段同一中枢同一名字不重复挂
            if self._mmd_already_attached(now_line, mmd_name, related_zs):
                continue

            now_line.add_mmd(
                name=mmd_name,
                zs=related_zs,
                zs_type=self.zs_type,
                msg=msg,
            )
            LogUtil.debug(lambda:
                f"[BsPointCalculator] 识别到 {mmd_name}: line.index={now_line.index}, "
                f"zs.index={related_zs.index}, msg={msg}"
            )

    # ------------------------------------------------------------------
    # 三买的辅助方法（仅供 _detect_3buy_3sell 使用）
    # ------------------------------------------------------------------
    def _find_related_zs_for_3rd(
        self, now_line: LINE, zss: List[ZS]
    ) -> Optional[ZS]:
        """
        寻找 ``now_line`` 用于判定三买/三卖时所对应的"被离开中枢"。

        规则：
            - 中枢必须已完成（``done == True``）且有效（``real == True``）
            - ``now_line`` 必须严格在中枢离开段之后（不能是中枢内部线段，也不能是离开段本身）
            - 取所有合法中枢中"离开段最近"的那一个

        说明：本方法委托 ``_find_all_candidate_zss_for_3rd`` 取候选列表第一个
        （= 离开段最近的中枢）；``_detect_3buy_3sell`` 主流程即调用本方法。

        :param now_line: 候选反抽线段
        :param zss: 中枢列表（按 index 升序）
        :return: 对应中枢，或 ``None``（无合法中枢）
        """
        candidates = self._find_all_candidate_zss_for_3rd(now_line, zss)
        return candidates[0] if candidates else None

    def _find_all_candidate_zss_for_3rd(
        self, now_line: LINE, zss: List[ZS]
    ) -> List[ZS]:
        """
        扫描所有满足时间/状态条件的候选中枢，按"离开段距离 now_line 由近到远"排序。

        规则（与 _find_related_zs_for_3rd 一致）：
            - 中枢已完成（``done == True``）且有效（``real == True``）
            - ``now_line`` 必须严格在中枢离开段之后
            - ``now_line`` 不能是中枢的核心线段或离开段本身

        :param now_line: 候选反抽线段
        :param zss: 中枢列表（按 index 升序，已经过 valid_zss 时间窗口切片）
        :return: 按"离开段最近"排序的中枢列表（可能为空）
        """
        candidates: List[ZS] = []
        for zs in zss:
            if not zs.done or not zs.real:
                continue
            if zs.end is None or zs.end.end is None:
                # 完成的中枢必有 end，本分支属防御性
                continue

            # now_line 必须在中枢离开段"之后"
            if now_line.index <= zs.end.index:
                continue

            # now_line.index > zs.end.index(上面 717 已保证)⇒ now_line 必在中枢
            # 所有核心线段(index ≤ zs.end.index)之后,也不可能是离开段本身;故原
            # `now_line in zs.lines`(对每条核心线调 __eq__,3 类检测的主热点来源)
            # 与 `now_line is zs.end` 两个判断恒为假、冗余,删除以消除 per-zs 的
            # O(zs.lines) __eq__ 扫描。行为由大网(SH.600519_5m,809笔/83个3类点)钉死。
            candidates.append(zs)

        # 按 zs.end.index 倒序（最近的 zs 排第一）
        candidates.sort(key=lambda zs: zs.end.index, reverse=True)
        return candidates

    def _judge_3rd_bs_point(
        self, now_line: LINE, zs: ZS
    ) -> tuple[Optional[str], str]:
        """
        判定 ``now_line`` 相对中枢 ``zs`` 是否构成三买/三卖。

        :return: (mmd_name, msg)。``mmd_name`` 为 ``'3buy'`` / ``'3sell'`` / ``None``
        """
        leave_direction = zs.end.type  # 离开段方向
        if leave_direction not in ('up', 'down'):
            return None, ""

        # 反抽方向必须与离开段相反
        if now_line.type == leave_direction:
            return None, ""

        if leave_direction == 'up':
            # 中枢向上离开 → now_line 是 down 反抽 → 反抽未跌破 ZG → 三买
            threshold_ok = (
                now_line.low > zs.zg if self.strict_3_mode else now_line.low >= zs.zg
            )
            if threshold_ok:
                op = '>' if self.strict_3_mode else '>='
                return (
                    '3buy',
                    f'中枢向上离开后反抽未跌破 ZG: line.low={now_line.low} {op} ZG={zs.zg}',
                )
            return None, ""

        # leave_direction == 'down'
        # 中枢向下离开 → now_line 是 up 反抽 → 反抽未触及 ZD → 三卖
        threshold_ok = (
            now_line.high < zs.zd if self.strict_3_mode else now_line.high <= zs.zd
        )
        if threshold_ok:
            op = '<' if self.strict_3_mode else '<='
            return (
                '3sell',
                f'中枢向下离开后反抽未触及 ZD: line.high={now_line.high} {op} ZD={zs.zd}',
            )
        return None, ""

    def _mmd_already_attached(
        self, line: LINE, mmd_name: str, zs: ZS
    ) -> bool:
        """
        检查 ``line`` 在 ``self.zs_type`` 下是否已经挂过同名 + 同中枢的买卖点（避免增量计算重复挂）。

        BI / XD 都继承自 LINE 并提供 ``zs_type_mmds: Dict[str, List[MMD]]``，
        且都有 ``mmd_exists`` 方法。这里使用更精确的"同 MMD.zs"比较以支持去重。
        """
        existing_mmds = getattr(line, 'zs_type_mmds', {}).get(self.zs_type, [])
        for mmd in existing_mmds:
            if mmd.name == mmd_name and mmd.zs is zs:
                return True
        return False
