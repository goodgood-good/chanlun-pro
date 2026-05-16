# -*- coding: utf-8 -*-
"""
中枢计算模块

功能：
ZsCalculator: 负责根据笔或线段列表，识别和构建本级别的中枢。
"""
from __future__ import annotations
from typing import List, Optional

from chanlun.core.cl_interface import ZS, LINE


class ZsCalculator:
    """
    标准中枢计算器
    功能：根据输入的线段列表，识别和构建本级别的所有中枢。
    采用全量计算方式，确保每次计算都是独立的、无状态污染的。
    """

    def __init__(self):
        self.all_lines: List[LINE] = []
        self.zss: List[ZS] = []
        self.pending_zs: Optional[ZS] = None
        # 增量计算状态
        self._last_lines_count: int = 0
        self._last_entry_idx: int = 0  # 上次计算结束时的 entry_idx
        # 增量校验用的尾段快照 (index, start.val, end.val, done)，
        # 任一项变化都意味着前缀已改变，必须降级到全量重算。
        self._last_tail_snapshot: Optional[tuple] = None

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
            return []

        # 检查线段数量
        if len(lines) < 4:
            self._last_lines_count = len(lines)
            self._last_tail_snapshot = self._build_tail_snapshot(lines)
            return []

        # 判断是否为增量更新：除长度比较外，还要校验上次尾段在新 lines 里仍
        # 存在且未变化。XdCalculator 增量时末段 done 可能翻转，仅看 len 会误走
        # 增量分支、从不再存在的 pending_zs 起点扫描而漏识别中枢。
        prefix_unchanged = self._tail_snapshot_consistent(lines)
        is_incremental = (
            self._last_lines_count > 0
            and len(lines) >= self._last_lines_count
            and (self.zss or self.pending_zs)
            and prefix_unchanged
        )

        if is_incremental:
            # 增量回退点不能信 pending_zs.start.index：上游增量会重排 line.index。
            # 改用业务唯一键 (start.val, start.k.k_index, type) 重新定位上次
            # pending_zs 的进入段位置，定位失败则安全降级到全量。
            self.all_lines = lines

            restart_idx: Optional[int] = None
            if self.pending_zs and self.pending_zs.start is not None:
                restart_idx = self._locate_line(lines, self.pending_zs.start)
                self.pending_zs = None
            elif self.zss:
                # 没有 pending 时，复用上次记录的 exit 位置；
                # 但 _last_entry_idx 也是基于 line 序号的，越界则降级。
                if 0 <= self._last_entry_idx <= len(lines) - 4:
                    restart_idx = self._last_entry_idx

            if restart_idx is None:
                # 任意环节定位失败 → 全量重算，正确性优先
                self.zss = []
                self.pending_zs = None
                restart_idx = 0
            self._create_zs_full(start_entry_idx=restart_idx)
        else:
            # 全量模式
            self.zss = []
            self.pending_zs = None
            self.all_lines = lines
            self._create_zs_full(start_entry_idx=0)

        # 更新增量状态
        self._last_lines_count = len(lines)
        self._last_tail_snapshot = self._build_tail_snapshot(lines)

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

    def _create_zs_full(self, start_entry_idx: int = 0):
        """
        核心函数：扫描并创建中枢
        :param start_entry_idx: 扫描起始位置（增量模式下从上次结束位置开始）
        """
        entry_idx = start_entry_idx
        # 循环必须为至少一个进入段和3个核心段(共4段)留出空间。
        while entry_idx <= len(self.all_lines) - 4:
            entry_seg = self.all_lines[entry_idx]
            core_start_idx = entry_idx + 1

            seg_a, seg_b, seg_c = self.all_lines[core_start_idx:core_start_idx + 3]

            # 检查线段方向是否交替
            if not (seg_a.type != seg_b.type and seg_b.type != seg_c.type):
                entry_idx += 1
                continue

            # 计算初始三段的重叠区域 (zg, zd)
            zg = min(seg_a.zs_high, seg_b.zs_high, seg_c.zs_high)
            zd = max(seg_a.zs_low, seg_b.zs_low, seg_c.zs_low)

            # 1. 检查三段核心是否有重叠
            if zd >= zg:
                entry_idx += 1
                continue

            # 2. 检查进入段是否与三段核心的重叠区有重叠
            if not (max(entry_seg.zs_low, zd) < min(entry_seg.zs_high, zg)):
                entry_idx += 1
                continue

            # 找到了一个有效的三段核心。
            # 注意：seg_c 此时被假定为核心，如果它稍后被证明是离开段，
            # _extend_and_check_complete 将负责将其移除。
            core_lines = [seg_a, seg_b, seg_c]

            center = ZS(zs_type='xd', start=entry_seg, _type=seg_b.type)
            center.lines = core_lines
            center._bounds_dirty = True  # 整体赋值 lines 后边界缓存失效
            # 初始中枢范围由前三段重叠决定
            center.zg, center.zd = zg, zd
            center.update_boundaries()

            # 3. 尝试延伸中枢，并检查是否完成
            is_completed, exit_idx = self._extend_and_check_complete(center, core_start_idx + 3)

            if is_completed:
                # 有效中枢须同时满足：有进入段、有离开段、核心线段 >= 3
                is_valid_center = (
                        center.start is not None and
                        center.end is not None and
                        len(center.lines) >= 3
                )

                if is_valid_center:
                    center.index = len(self.zss)
                    self.zss.append(center)
                    # 下一个中枢从离开段开始找
                    entry_idx = exit_idx
                    self._last_entry_idx = entry_idx
                else:
                    # 无效中枢丢弃（如初始 seg_c 被确认为离开段导致核心 < 3），
                    # entry_idx +1 从下一线段重新找进入段
                    entry_idx += 1
            else:
                # 未完成说明已走到线段末尾，这是最后一个可能的中枢
                if len(center.lines) >= 3:
                    self.pending_zs = center
                self._last_entry_idx = entry_idx
                break

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
            current_seg = self.all_lines[j]

            current_overlaps = max(current_seg.zs_low, center.zd) < min(current_seg.zs_high, center.zg)

            if current_overlaps:
                # 情况 1：当前线段(j)重叠，可能是核心也可能是离开段，须看 j+1

                # 1.1 检查是否是最后一条线段
                if j == len(self.all_lines) - 1:
                    # 这是最后一条线段，它重叠了，必须是核心成员
                    center.lines.append(current_seg)
                    center.update_boundaries()
                    return False, j

                # 1.2 预读下一条线段 (next_seg) 以判断是否完成
                next_seg = self.all_lines[j + 1]
                next_overlaps = max(next_seg.zs_low, center.zd) < min(next_seg.zs_high, center.zg)

                if next_overlaps:
                    # 下一线段(j+1)也重叠
                    # 这证明 current_seg(j) *不是* 离开段，它 *是* 核心成员
                    center.lines.append(current_seg)
                    center.update_boundaries()
                    j += 1
                    continue
                else:
                    # 下一线段(j+1)不重叠
                    # 根据定义:
                    # - "不进入中枢范围的线段" = next_seg (j+1)
                    # - "离开段" = "前一个线段" = current_seg (j)

                    # current_seg(j) 是离开段，*不要* 将它加入 center.lines
                    center.end = current_seg  # 离开段是 current_seg (j)
                    center.done = True
                    return True, j  # 下一个中枢的入口是 j

            else:
                # --- 情况 2: 当前线段(j)不重叠 ---
                # 2.1 它就是第一个不进入中枢范围的线段
                # 根据定义:
                # - "不进入中枢范围的线段" = current_seg (j)
                # - "离开段" = "前一个线段" = self.all_lines[j-1]

                center.end = self.all_lines[j - 1]  # 离开段是 j-1
                center.done = True

                # *** 修正点 (使用 'is' 进行严格的对象身份检查) ***:
                # 检查 self.all_lines[j-1] (即离开段)
                # 是否 *就是* center.lines 的末尾 (例如初始的 seg_c)。
                # 如果是，说明初始的 seg_c 实际上是离开段，应将其从核心线段中移除。
                if center.lines and center.lines[-1] is center.end:
                    center.lines.pop()
                    center._bounds_dirty = True  # pop 后边界缓存失效

                return True, j - 1  # 下一个中枢的入口是 j-1

        # 循环正常结束 (j == len(self.all_lines))
        # 这意味着最后一条线段是 j-1，并且它没有导致中枢完成
        # (这种情况理论上在 1.1 中被覆盖了，但作为兜底)
        return False, j - 1