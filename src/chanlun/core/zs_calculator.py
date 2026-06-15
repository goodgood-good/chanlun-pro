# -*- coding: utf-8 -*-
"""
中枢计算模块

功能：
ZsCalculator: 负责根据笔或线段列表，识别和构建本级别的中枢。
"""
from __future__ import annotations
from typing import List, Optional

from chanlun.core.types import ZS, LINE


class ZsCalculator:
    """
    标准中枢计算器
    功能：根据输入的线段列表，识别和构建本级别的所有中枢。
    采用全量计算方式，确保每次计算都是独立的、无状态污染的。
    """

    def __init__(self, require_alternation: bool = True, min_zs_lines: int = 4,
                 max_zs_lines: int = 8):
        # require_alternation：是否强制核心三段方向交替。
        # 默认 True——① 主流程线段中枢扫描照旧。④ 递归到 L≥1 时构成段是
        # 走势类型（含无向盘整、不严格交替），传 False 跳过交替检查。
        self.require_alternation: bool = require_alternation
        # min_zs_lines：构成中枢的最小核心线段数（含离开段）。默认 4——
        # 这是**原文一致**的，并非偏离：原文要求「前三个次级别走势类型都是
        # **完成的**才构成中枢」(第18课)，而线段「必须被另一线段破坏才算
        # 完成」(原文线段定义/线段分解定理)；故第 3 段线段须由第 4 段确认其
        # 完成，前 3 段方都「完成」、中枢方成立——计入这条确认/离开段，最小即 4。
        # （勿据「三个线段重合即成中枢」的字面把它改成 3：那忽略了「完成的」
        # 这一限定，会把第 3 段未确认的形态误判成中枢。）④ 递归到 L≥1 时构成段
        # 是走势类型——传入的 ZSLX 单元已是完成走势类型，3 个重叠即成中枢，传 3。
        self.min_zs_lines: int = min_zs_lines
        # max_zs_lines：中枢含核心的最大段数（默认 8 = 3 核心 + 5 延伸）。
        # 原文第33课：「中枢的延伸不能超过 5 段，一旦出现 6 段的延伸，加上形成
        # 中枢本身那三段（=9 段），就构成更大级别的中枢了」。故单个中枢封顶 8 段；
        # 达上限仍重叠 → 在此完成（末段为离开/升级边界），后续振荡由下一中枢承接，
        # 同区间的多个 ≤8 段中枢经扩展（_expand_overlapping）升级为高级别中枢。
        # 不封顶会把长振荡贪心吸成 30 段巨型中枢（核心被首两段钉死、与走势脱节）。
        self.max_zs_lines: int = max_zs_lines
        self.all_lines: List[LINE] = []
        self.zss: List[ZS] = []
        self.pending_zs: Optional[ZS] = None
        # 增量计算状态
        self._last_lines_count: int = 0
        self._last_entry_idx: int = 0  # 上次计算结束时的 entry_idx
        # 增量校验用的尾段快照 (index, start.val, end.val, done)，
        # 任一项变化都意味着前缀已改变，必须降级到全量重算。
        self._last_tail_snapshot: Optional[tuple] = None
        # 上轮喂入的 lines 列表对象引用,供「末段变化但前缀稳定」时按 identity 二分
        # 求公共前缀(_prefix_stable_restart),替代 11% 全量回退。仅当 lines 为
        # 「变更换新列表」(bis 语义:bi_calculator 变更换新对象、prefix 对象身份保留)
        # 时有效;xds 原地改 → lines is _last_lines_obj → 该路径自动让位全量。
        self._last_lines_obj: Optional[List[LINE]] = None

    def calculate(self, lines: List[LINE]) -> List[ZS]:
        """
        计算中枢（支持增量）。

        增量逻辑：如果线段列表在末尾追加了新数据（前缀不变），
        则保留已完成的中枢，仅从 pending_zs 位置或最后完成中枢的
        exit 位置重新计算，避免全量重扫。

        :param lines: 当前级别的所有线段
        :return: 计算出的所有中枢（已完成 + 进行中）
        """
        if not lines:
            self.zss = []
            self.pending_zs = None
            self._last_lines_count = 0
            self._last_entry_idx = 0
            self._last_tail_snapshot = None
            self._last_lines_obj = None
            return []

        # 检查线段数量：中枢最少由 min_zs_lines 段重叠构成（L0 线段中枢
        # 默认 4，离开段计入核心；④ 的 L≥1 走势类型中枢为 3）。进入段可选，
        # 故 min_zs_lines 段即起步、不足则不可能成中枢。
        if len(lines) < self.min_zs_lines:
            self._last_lines_count = len(lines)
            self._last_tail_snapshot = self._build_tail_snapshot(lines)
            self._last_lines_obj = lines
            return []

        # 判断是否为增量更新：除长度比较外，还要校验上次尾段在新 lines 里仍
        # 存在且未变化。XdCalculator 增量时末段 done 可能翻转，仅看 len 会误走
        # 增量分支、从不再存在的 pending_zs 起点扫描而漏识别中枢。
        prefix_unchanged = self._tail_snapshot_consistent(lines)
        # 必须有「已完成中枢」(self.zss 非空)才增量。仅有 pending 中枢(第一个
        # 中枢尚未完成)时不可增量——开头中枢的进入段提升
        # (_promote_opening_entry_if_needed:开头恰好 5 段→提升第1段为进入段、
        # core 从第2段起 entry≥0;6 段则不提升 entry=-1)随段数变化且**不可逆**:
        # 从提升后的 entry≥0 重启无法在中枢延伸到 6 段时 un-promote 回 entry=-1,
        # 会与全量(每次从 -1 重判)分叉(见 tests/chan_core/test_incremental_equivalence)。
        # 第一个中枢一旦完成进 self.zss 即定型(核心段不再延伸),此后增量安全。
        grow = (
            self._last_lines_count > 0
            and len(lines) >= self._last_lines_count
            and bool(self.zss)
        )

        if grow and prefix_unchanged:
            # 末段也稳定:原增量路径。回退点不能信 pending_zs.start.index(上游增量
            # 会重排 line.index),改用业务唯一键重定位,失败则安全降级全量。
            self.all_lines = lines

            restart_idx: Optional[int] = None
            if self.pending_zs and self.pending_zs.start is not None:
                restart_idx = self._locate_line(lines, self.pending_zs.start)
                self.pending_zs = None
            elif self.zss:
                # 没有 pending 时，复用上次记录的 exit 位置；
                # 但 _last_entry_idx 也是基于 line 序号的，越界则降级。
                # -1 是合法值（开头中枢无进入段）。
                if -1 <= self._last_entry_idx <= len(lines) - 4:
                    restart_idx = self._last_entry_idx

            if restart_idx is None:
                # 任意环节定位失败 → 全量重算（-1：从可能的开头中枢扫起）。
                self.zss = []
                self.pending_zs = None
                restart_idx = -1
            self._create_zs_full(start_entry_idx=restart_idx)
        elif self._prefix_stable_restart(lines, grow):
            # 末段变了但「换新列表 + identity 前缀稳定」:已在内部保留安全完成中枢、
            # 从其 exit 重启,替代昂贵的 O(n) 全量回退(bi 笔层中枢主热点)。
            pass
        else:
            # 全量模式（-1：从序列开头扫起，开头三段也可成中枢）
            self.zss = []
            self.pending_zs = None
            self.all_lines = lines
            self._create_zs_full(start_entry_idx=-1)

        # 更新增量状态
        self._last_lines_count = len(lines)
        self._last_tail_snapshot = self._build_tail_snapshot(lines)
        self._last_lines_obj = lines

        # 组合并返回结果
        final_zss = self.zss.copy()
        if self.pending_zs:
            final_zss.append(self.pending_zs)
        return final_zss

    @staticmethod
    def _locate_line(lines: List[LINE], target_line: LINE) -> Optional[int]:
        """在 lines 中重新定位 target_line 的位置。

        用业务唯一键 (start.val, end.val, start.k.k_index, type) 定位，避开
        line.index 漂移：价格不变、原始 K 索引单调递增、type 作方向兜底。
        从尾部往前找，因为同一根线段最可能停留在原位附近。
        """
        try:
            t_start_val = target_line.start.val if target_line.start is not None else None
            t_end_val = target_line.end.val if target_line.end is not None else None
            t_k_index = (
                target_line.start.k.k_index
                if (target_line.start is not None and target_line.start.k is not None)
                else None
            )
            t_type = getattr(target_line, 'type', None)
        except AttributeError:
            return None

        if t_start_val is None or t_k_index is None:
            return None

        # 尾部往前扫，命中即返回（增量场景下基本是 O(1)~O(几个)）
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            try:
                if line.start is None or line.start.k is None:
                    continue
                if (
                    line.start.val == t_start_val
                    and line.start.k.k_index == t_k_index
                    and getattr(line, 'type', None) == t_type
                ):
                    # end.val 可选校验：能命中说明就是同一根
                    if t_end_val is None or (line.end is not None and line.end.val == t_end_val):
                        return i
            except AttributeError:
                continue
        return None

    @staticmethod
    def _build_tail_snapshot(lines: List[LINE]) -> Optional[tuple]:
        """构造 lines 的尾段快照，用于增量模式的前缀一致性校验。

        快照内容：
          - 末段的笔/段 index
          - 末段的 start.val / end.val
          - 末段的 done 状态
          - lines 的总长度

        注意：不能用 id(line) 做快照，因为 XdCalculator 每次增量都会
        pop 掉最后一段并追加新对象，对象身份会变。
        """
        if not lines:
            return None
        last = lines[-1]
        try:
            return (
                len(lines),
                getattr(last, 'index', -1),
                last.start.val if last.start is not None else None,
                last.end.val if last.end is not None else None,
                bool(getattr(last, 'done', False)),
            )
        except AttributeError:
            return None

    def _tail_snapshot_consistent(self, lines: List[LINE]) -> bool:
        """检查上次记录的尾段快照在新的 lines 里是否仍然成立。

        核心校验：
          - 上次记录的 (index, start.val, end.val, done) 在新 lines 中位置
            `len_at_last_calc - 1` 是否仍然吻合
          - 如果吻合 → 前缀未变，可以走增量
          - 否则 → 历史尾段被改写，必须降级到全量
        """
        if self._last_tail_snapshot is None:
            return False
        prev_len, prev_idx, prev_start_val, prev_end_val, prev_done = self._last_tail_snapshot
        if prev_len == 0 or prev_len > len(lines):
            return False
        candidate = lines[prev_len - 1]
        try:
            cand_start = candidate.start.val if candidate.start is not None else None
            cand_end = candidate.end.val if candidate.end is not None else None
            cand_done = bool(getattr(candidate, 'done', False))
        except AttributeError:
            return False
        if getattr(candidate, 'index', -1) != prev_idx:
            return False
        if cand_start != prev_start_val or cand_end != prev_end_val:
            return False
        if cand_done != prev_done:
            return False
        return True

    @staticmethod
    def _identity_prefix_len(new_lines: List[LINE], old_lines: List[LINE]) -> int:
        """二分求 new_lines 与 old_lines 的 identity 公共前缀长度 O(log L)。

        依赖上游 rebuild 契约(bi/xd 均「删后缀 + 新建」):一次变更只换连续后缀对象、
        共享前缀对象不动,故 ``new_lines[i] is old_lines[i]`` 在分歧点前恒真、之后
        恒假(单调),可二分定位。仅在 new/old 为不同列表对象时调用(见 _prefix_stable_restart)。
        """
        lo, hi = 0, min(len(new_lines), len(old_lines))
        while lo < hi:
            mid = (lo + hi) // 2
            if new_lines[mid] is old_lines[mid]:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _prefix_stable_restart(self, lines: List[LINE], grow: bool) -> bool:
        """末段变化但前缀稳定时的增量重启(替代全量回退):返回 True 表示已处理。

        仅当 ``lines`` 是「变更换新列表」(bis 语义:bi_calculator 变更时换新对象、
        prefix 对象身份保留;xds 原地改 → lines is _last_lines_obj → 不适用,返回
        False 让位全量)且与上轮存在 identity 公共前缀时启用。

        保留 ``end.index + 1 < ident_lcp`` 的完成中枢——其核心段及「完成判定回看的
        下一段 lines[end+1]」(两种完成路径均只回看 +1,见 _extend_and_check_complete)
        全落在稳定前缀内,故 frozen 安全;从最后一个安全中枢的 exit 重启重扫,丢弃失效
        pending 与不安全的尾部中枢。只复用**已完成**中枢 → 不触及开头中枢 promote 雷区
        (与「必须有完成中枢才增量」不变量一致)。等价性由对拍网 test_incremental_equivalence 守。
        """
        if not grow or lines is self._last_lines_obj or self._last_lines_obj is None:
            return False
        ident_lcp = self._identity_prefix_len(lines, self._last_lines_obj)
        keep = 0
        restart_idx = -1
        for k, zs in enumerate(self.zss):
            e = zs.end.index if zs.end is not None else -1
            if e >= 0 and e + 1 < ident_lcp:
                keep = k + 1
                restart_idx = e
            else:
                break
        if keep <= 0 or not (0 <= restart_idx <= len(lines) - 4):
            return False
        del self.zss[keep:]
        self.pending_zs = None
        self.all_lines = lines
        self._create_zs_full(start_entry_idx=restart_idx)
        return True

    def _create_zs_full(self, start_entry_idx: int = -1):
        """
        核心函数：扫描并创建中枢

        进入段是可选的：原文中枢定义只讲「3+ 连续次级别走势类型重叠」，
        不含进入段。``entry_idx == -1`` 表示核心段从序列开头 (index 0) 起，
        此时中枢没有进入段（``ZS.start`` 为 None）。

        :param start_entry_idx: 扫描起始的进入段下标；-1 表示从无进入段的
                                开头中枢扫起。增量模式下传上次结束位置。
        """
        entry_idx = start_entry_idx
        # 循环为 3 个核心段留空间；进入段（entry_idx 那一段）可选。
        while entry_idx <= len(self.all_lines) - 4:
            # entry_idx == -1：核心段位于序列开头，中枢没有进入段。
            entry_seg = self.all_lines[entry_idx] if entry_idx >= 0 else None
            core_start_idx = entry_idx + 1

            seg_a, seg_b, seg_c = self.all_lines[core_start_idx:core_start_idx + 3]

            # 检查线段方向是否交替（require_alternation=False 时跳过，供 ④ 的
            # L≥1 走势类型扫描——走势类型含无向盘整、不严格交替）。
            if self.require_alternation and not (
                seg_a.type != seg_b.type and seg_b.type != seg_c.type
            ):
                entry_idx += 1
                continue

            # 原文(行2835·缠回复严格公式)：中枢区间 = (max(a₂,b₂,c₂), min(a₁,b₁,c₁))
            # ——即 A/B/C **三段共同重叠**;等价于第20课(行3585)同向 Z 段
            # ZG=min(g₁,g₂)、ZD=max(d₁,d₂)(相邻段共享端点,故两式等价)。
            # 只取前两段会漏掉 seg_c 的高/低约束、令中枢在趋势侧偏宽(审查 F1)。
            zg = min(seg_a.zs_high, seg_b.zs_high, seg_c.zs_high)
            zd = max(seg_a.zs_low, seg_b.zs_low, seg_c.zs_low)

            # 三段无共同重叠则非中枢。zd<zg 等价于 max(三低)<min(三高),即三段
            # 确有共同重叠,无须再单独校验 seg_c 是否落在前两段区间内。
            if zd >= zg:
                entry_idx += 1
                continue

            # 进入段不参与中枢成立判定：原文中枢 = 3+ 连续次级别走势类型重叠，
            # 不要求进入段与中枢区间重叠。旧实现此处的「进入段重叠门槛」属
            # extra-原文，会漏掉进入段在区间外的合法中枢，已删除。

            # seg_a / seg_b / seg_c 三段重叠先给出候选中枢区间 [zd, zg]；
            # 中枢成立还需第 4 段也重叠（由 _extend_and_check_complete 续判），
            # 不足 4 段重叠的候选在下方按 len(center.lines) >= 4 作废。
            core_lines = [seg_a, seg_b, seg_c]

            # _type 此处仅置初始占位（中间核心段方向）：线段中枢的真实方向
            # （上涨/下跌中枢 up/down、盘整中枢 zd）由 ZslxCalculator 在走势
            # 类型划分后回填（子项目③）；笔中枢未接入 ③，沿用此占位。
            center = ZS(zs_type='xd', start=entry_seg, _type=seg_b.type)
            center.lines = core_lines
            center._bounds_dirty = True  # 整体赋值 lines 后边界缓存失效
            # 初始中枢范围由前三段重叠决定
            center.zg, center.zd = zg, zd

            # 3. 尝试延伸中枢，并检查是否完成
            is_completed, exit_idx = self._extend_and_check_complete(center, core_start_idx + 3)
            self._promote_opening_entry_if_needed(center)
            # gg/dd(延伸包络)在中枢构建中不被读(延伸/完成判据只用核心区 zd/zg),故把
            # update_boundaries 批到此处算一次(center 仍 _bounds_dirty=True → 全量重算
            # 末段 gg/dd;promote 分支已自行 update 则此处走增量幂等),取代 _extend 内每
            # append 一次的 update_boundaries(实测 124k 次→中枢数,省 ~0.1s 纯调用开销)。
            center.update_boundaries()

            if is_completed:
                # 有效中枢须满足：有离开段、核心线段 >= min_zs_lines（L0
                # 线段中枢默认 4，离开段计入核心故含离开段共 >= 4 段重叠）。
                # 进入段可为 None（开头中枢无进入段），故不校验 center.start。
                is_valid_center = (
                        center.end is not None and
                        len(center.lines) >= self.min_zs_lines
                )

                if is_valid_center:
                    center.index = len(self.zss)
                    self.zss.append(center)
                    # 下一个中枢从离开段开始找
                    entry_idx = exit_idx
                    self._last_entry_idx = entry_idx
                else:
                    # 不足 4 段重叠（恰好 3 段重叠、第 4 段即离开）→ 不构成
                    # 中枢，丢弃；entry_idx +1 从下一线段继续找。
                    entry_idx += 1
            else:
                # 未完成说明已走到线段末尾，这是最后一个可能的中枢；
                # 同样须 >= min_zs_lines 段重叠才作为 pending 中枢输出。
                if len(center.lines) >= self.min_zs_lines:
                    self.pending_zs = center
                self._last_entry_idx = entry_idx
                break

    def _promote_opening_entry_if_needed(self, center: ZS) -> None:
        """开头中枢恰好 5 段重叠时，第一段按进入段处理。

        项目 L0 口径为 4 段重叠成中枢，最后一段为离开段；若从数据开头
        直接看到 5 段连续重叠，则第 1 段不再是核心段，而是进入段，
        中枢核心应从第 2 段开始。
        """
        if (
            self.min_zs_lines != 4
            or center.start is not None
            or len(center.lines) != self.min_zs_lines + 1
        ):
            return

        promoted_start = center.lines[0]
        new_lines = center.lines[1:]
        if len(new_lines) < self.min_zs_lines:
            return

        seg_a, seg_b, seg_c = new_lines[0], new_lines[1], new_lines[2]
        new_zg = min(seg_a.zs_high, seg_b.zs_high, seg_c.zs_high)
        new_zd = max(seg_a.zs_low, seg_b.zs_low, seg_c.zs_low)
        if new_zd >= new_zg:
            return

        # 前三段已定 [new_zd,new_zg];其余段(第 4 段起)须各自与该区间重叠。
        for line in new_lines[3:]:
            if max(line.zs_low, new_zd) >= min(line.zs_high, new_zg):
                return

        center.start = promoted_start
        center.lines = new_lines
        center.zg = new_zg
        center.zd = new_zd
        if center.end is promoted_start:
            center.end = new_lines[-1]
        center._bounds_dirty = True
        center.update_boundaries()

    def _extend_and_check_complete(self, center: ZS, start_j: int) -> tuple[bool, int]:
        """
        检查中枢的延伸或完成。

        :param center: 当前中枢对象 (in-out param, 会被修改)
        :param start_j: 开始检查的线段索引 (核心段 seg_c 之后的索引)
        :return: 一个元组 (是否完成: bool, 离开段的索引: int)。
                 如果未完成，返回 (False, last_index)。
                 如果已完成，返回 (True, exit_segment_index)。
        """
        j = start_j
        while j < len(self.all_lines):
            # 第33课封顶：中枢含核心达 max_zs_lines 段、仍在延伸（未自然完成）→
            # 在此封顶完成。末段(center.lines[-1])作离开/升级边界，当前段 j 起下一
            # 中枢核心、续接振荡；同区间多个 ≤max 段中枢经扩展(_expand_overlapping)
            # 升级为高级别中枢。不封顶会把长振荡贪心吸成 30 段巨型中枢、与走势脱节。
            if len(center.lines) >= self.max_zs_lines:
                center.end = center.lines[-1]
                center.done = True
                return True, j - 1

            current_seg = self.all_lines[j]

            current_overlaps = max(current_seg.zs_low, center.zd) < min(current_seg.zs_high, center.zg)

            if current_overlaps:
                # 情况 1：当前线段(j)重叠，可能是核心也可能是离开段，须看 j+1

                # 1.1 检查是否是最后一条线段
                if j == len(self.all_lines) - 1:
                    # 这是最后一条线段，它重叠了，必须是核心成员
                    center.lines.append(current_seg)
                    if len(center.lines) >= self.min_zs_lines:
                        center.end = current_seg
                    return False, j

                # 1.2 预读下一条线段 (next_seg) 以判断是否完成
                next_seg = self.all_lines[j + 1]
                next_overlaps = max(next_seg.zs_low, center.zd) < min(next_seg.zs_high, center.zg)

                if next_overlaps:
                    # 下一线段(j+1)也重叠
                    # 这证明 current_seg(j) *不是* 离开段，它 *是* 核心成员
                    center.lines.append(current_seg)
                    j += 1
                    continue
                else:
                    # 下一线段(j+1)不重叠 → 中枢在此完成。
                    # current_seg(j) 是最后一个与中枢重叠的线段：它从中枢内
                    # 伸出到中枢外，既是中枢的「离开段」，也是中枢的构成段，
                    # 计入核心一并参与「>= 4 段重叠」的成立判定。
                    center.lines.append(current_seg)
                    center.end = current_seg  # 离开段 = 最后一个重叠段
                    center.done = True
                    return True, j  # 下一个中枢的入口仍是离开段 j

            else:
                # --- 情况 2: 当前线段(j)不重叠 → 中枢在此完成 ---
                # current_seg(j) 是第一个脱离中枢的线段（反抽 / 新走势的起点）。
                # 离开段是它前一个线段 j-1，即最后一个重叠段，已在 center.lines
                # 中（初始 seg_c，或之前 append 进来的延伸段）。它同时是构成段
                # 与离开段，留在核心参与「>= 4 段重叠」的成立判定。
                center.end = self.all_lines[j - 1]  # 离开段 = 最后一个重叠段
                center.done = True
                return True, j - 1  # 下一个中枢的入口是 j-1

        # 循环正常结束 (j == len(self.all_lines))
        # 这意味着最后一条线段是 j-1，并且它没有导致中枢完成
        # (这种情况理论上在 1.1 中被覆盖了，但作为兜底)
        return False, j - 1
