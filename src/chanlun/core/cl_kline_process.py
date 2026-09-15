# -*- coding: utf-8 -*-
"""
缠论K线包含关系处理模块（支持增量更新）
"""
from copy import copy
from dataclasses import dataclass
from typing import List
from chanlun.core.types import Kline, CLKline


@dataclass(frozen=True)
class InitialSourceAlternatives:
    """初始前情缺失时，同一价格几何对应的两种原始 K 线归属。"""

    geometry: tuple
    up_sources: tuple[int, ...]
    down_sources: tuple[int, ...]


class CL_Kline_Process:
    """
    处理原始K线的包含关系，并将结果维护在内部的 ``self.cl_klines`` 中。

    该类不是“传入列表 -> 返回结果列表”的纯函数，而是一个有状态的增量更新器：
    - 输入 ``src_klines`` 用于驱动内部 ``cl_klines`` 状态更新
    - 结果由调用方通过 ``self.cl_klines`` 读取
    - 支持对最后一根原始K线进行同 index 的数值更新（re-paint）
    """

    def __init__(self, *, _initial_direction=None):
        self.cl_klines: List[CLKline] = []
        # 跟踪最后一个已处理的 *原始* K线索引
        self._last_src_kline_index: int = -1
        # 供下游增量计算使用的进程内数据代次。每次实际处理新增/更新原始 K 后
        # 单调递增；旧 pickle 没有该字段时由 property 安全回退到 0。
        self._structure_revision: int = 0
        # L065 82–100 的方向定义需要此前不包含的 K 线。没有前情时
        # 分别保留向上、向下两个假设，输出共同几何及带候选证据的左肩。
        # 这是对资料缺失的工程处理，不是作者规定的第三种包含方向。
        self._initial_direction = _initial_direction
        self._initial_variants = ()
        self._initial_signature = None
        self._initial_common_offset = None
        self._initial_left_contexts = ()
        self.initial_context_unresolved = False
        self.initial_excluded_raw_count = 0
        self.initial_source_alternatives: tuple[InitialSourceAlternatives, ...] = ()

    @property
    def structure_revision(self) -> int:
        """返回当前包含处理结果的数据代次。"""

        return int(getattr(self, "_structure_revision", 0))

    def _need_merge(self, k1: CLKline, k2: CLKline) -> bool:
        """判断两根缠论K线是否存在包含关系"""
        k1_contains_k2 = k1.h >= k2.h and k1.l <= k2.l
        k2_contains_k1 = k2.h >= k1.h and k2.l <= k1.l
        return k1_contains_k2 or k2_contains_k1

    def _resolve_direction(self, last_cl_k: CLKline, new_cl_k: CLKline) -> str:
        """只接受明确的非包含方向，或初始双分支中的显式假设。"""
        if new_cl_k.h > last_cl_k.h and new_cl_k.l > last_cl_k.l:
            return 'up'
        if new_cl_k.h < last_cl_k.h and new_cl_k.l < last_cl_k.l:
            return 'down'
        if self._initial_direction in ('up', 'down'):
            return self._initial_direction
        raise ValueError("inclusion direction requires prior non-included context")

    @staticmethod
    def _initial_geometry(k):
        # L062 37–61 的分型只比较高低价；保留极值的实际时间/坐标，
        # 防止把不同位置的同价 K 线混为一根。原始成员归属另行保存，
        # 不能因为成员不同而丢掉两种初始前情都已经确定的左肩。
        return k.h, k.l, k.date, k.k_index

    def _process_without_initial_context(self, src_klines):
        signature = tuple((k.index, k.date, k.h, k.l, k.o, k.c, k.a) for k in src_klines[:2])
        if signature != self._initial_signature:
            self._initial_signature = signature
            self._initial_variants = tuple(CL_Kline_Process(_initial_direction=d) for d in ('up', 'down'))
            self._initial_common_offset = None
            self._initial_left_contexts = ()
            self.initial_source_alternatives = ()
            self.cl_klines.clear()
        for variant in self._initial_variants:
            variant.process_cl_klines(src_klines)
        first = self._initial_variants[0]
        offset = self._initial_common_offset
        if offset is None:
            other = self._initial_variants[1]
            common = 0
            for left, right in zip(reversed(first.cl_klines), reversed(other.cl_klines)):
                if self._initial_geometry(left) != self._initial_geometry(right):
                    break
                common += 1
            offset = len(first.cl_klines) - common
            other_offset = len(other.cl_klines) - common
            self._initial_left_contexts = (
                (first.cl_klines[offset - 1], other.cl_klines[other_offset - 1])
                if common and offset > 0 and other_offset > 0 else ()
            )
            self.initial_source_alternatives = tuple(
                InitialSourceAlternatives(
                    self._initial_geometry(left),
                    tuple(k.index for k in left.klines),
                    tuple(k.index for k in right.klines),
                )
                for left, right in zip(first.cl_klines[offset:], other.cl_klines[len(other.cl_klines) - common:])
                if tuple(k.index for k in left.klines) != tuple(k.index for k in right.klines)
            )
            self.cl_klines.clear()
            # 至少保留两个共同的已闭合前情，再加活动末 K。盘中末 K
            # 并回前一根时，合并方向仍由共同前情决定。
            if common >= 3:
                self._initial_common_offset = offset
                self._initial_variants = (first,)
        # 确定共同前情之后只更新尾部，不在每根行情上重复制全部历史。
        # 盘中原先独立的末 K 更新后，可能并回前一根；共同后缀会缩短。
        # 必须从新旧尾部中更早的位置重建，不能留下旧的倒数第二根。
        context_count = int(bool(self._initial_left_contexts))
        common_count = len(first.cl_klines) - offset + context_count
        refresh = max(0, min(len(self.cl_klines), common_count) - 1)
        del self.cl_klines[refresh:]
        if context_count and refresh == 0:
            context = copy(self._initial_left_contexts[0])
            context.index = 0
            context.initial_context_alternatives = self._initial_left_contexts
            self.cl_klines.append(context)
        for k in first.cl_klines[offset + max(0, refresh - context_count):]:
            shared = copy(k)
            shared.index = len(self.cl_klines)
            self.cl_klines.append(shared)
        self.initial_context_unresolved = True
        # 排除于共同几何的初始来源仍可作为多候选左肩被引用。
        self.initial_excluded_raw_count = (first.cl_klines[offset].klines[0].index
                                           if offset < len(first.cl_klines) else len(src_klines))
        self._last_src_kline_index = src_klines[-1].index
        self._structure_revision = self.structure_revision + 1

    def _merge_klines(self, k1: CLKline, k2: CLKline, direction: str) -> CLKline:
        """根据指定方向合并两根K线。"""
        if direction == 'up':
            # 向上合并：高取高、低取高
            h, l = max(k1.h, k2.h), max(k1.l, k2.l)
            o, c = l, h  # 缠论合并K线不强调OC，仅示意性赋值
            date, k_index = (k1.date, k1.k_index) if k1.h > k2.h else (k2.date, k2.k_index)
        else:  # 向下
            # 向下合并：高取低、低取低
            h, l = min(k1.h, k2.h), min(k1.l, k2.l)
            o, c = h, l
            date, k_index = (k1.date, k1.k_index) if k1.l < k2.l else (k2.date, k2.k_index)

        merged = CLKline(
            # k_index 落在原始K线坐标系，供 MACD 切片、角度计算使用
            k_index=k_index, date=date, h=h, l=l, o=o, c=c, a=k1.a + k2.a,
            klines=k1.klines + k2.klines,
            # index 保持缠论K线序号稳定，供 BiCalculator 增量回退与成笔距离判断使用
            index=k1.index,
            _n=k1.n + k2.n,
            _q=k1.q
        )
        merged.up_qs = direction
        return merged

    def _process_one_kline(self, current_k: Kline):
        """
        核心原子操作：将一根原始K线加入到 cl_klines 序列中。
        """
        # --- A. 初始化第一根缠论 K 线 ---
        if not self.cl_klines:
            new_cl_k = CLKline(
                k_index=current_k.index, date=current_k.date,
                h=current_k.h, l=current_k.l, o=current_k.o, c=current_k.c, a=current_k.a,
                klines=[current_k], index=0, _n=1
            )
            self.cl_klines.append(new_cl_k)
            self._last_src_kline_index = current_k.index
            return

        # --- B. 获取当前最新的缠论 K 线 ---
        last_cl_k = self.cl_klines[-1]

        # k_index 用原始K线坐标，index 用缠论K线序号
        new_cl_k = CLKline(
            k_index=current_k.index, date=current_k.date,
            h=current_k.h, l=current_k.l, o=current_k.o, c=current_k.c, a=current_k.a,
            klines=[current_k], index=len(self.cl_klines), _n=1
        )

        # --- C. 判断包含关系 ---
        # 无重叠即视为缺口
        has_gap = new_cl_k.l > last_cl_k.h or new_cl_k.h < last_cl_k.l

        if has_gap:
            new_cl_k.q = True
            self.cl_klines.append(new_cl_k)

        elif self._need_merge(last_cl_k, new_cl_k):
            # 确定合并方向
            # 优先级：
            #   1. 上一根缠论 K 已经标记过方向（last_cl_k.up_qs）→ 沿用
            #   2. 上一根缠论 K 与更前一根有明确高低关系 → 用上文趋势判断
            #   3. 缺少前情 → 仅内部初始双分支可以使用显式假设
            direction: str
            if last_cl_k.up_qs is not None:
                direction = last_cl_k.up_qs
            elif len(self.cl_klines) >= 2:
                prev_cl_k = self.cl_klines[-2]
                if last_cl_k.h > prev_cl_k.h and last_cl_k.l > prev_cl_k.l:
                    direction = 'up'
                elif last_cl_k.h < prev_cl_k.h and last_cl_k.l < prev_cl_k.l:
                    direction = 'down'
                else:
                    direction = self._resolve_direction(last_cl_k, new_cl_k)
            else:
                direction = self._resolve_direction(last_cl_k, new_cl_k)

            merged_k = self._merge_klines(last_cl_k, new_cl_k, direction)
            merged_k.index = last_cl_k.index
            self.cl_klines[-1] = merged_k

        else:
            # 有重叠但无包含，追加
            self.cl_klines.append(new_cl_k)

        # --- D. 更新状态 ---
        self._last_src_kline_index = current_k.index

    def process_cl_klines(self, src_klines: List[Kline]):
        """
        基于原始K线序列增量更新内部 ``self.cl_klines`` 状态。

        注意：
        - 该方法的结果保存在 ``self.cl_klines`` 中
        - 调用方不应依赖其返回值
        - 传入的 ``src_klines`` 用于驱动内部状态更新，而不是作为纯函数输入返回新列表
        """
        if not src_klines:
            return

        if self._initial_direction is None:
            ambiguous = len(src_klines) >= 2 and self._need_merge(src_klines[0], src_klines[1])
            if ambiguous:
                self._process_without_initial_context(src_klines)
                return
            if self.initial_context_unresolved:
                # 前两根被行情修订或补充前情后，重新按已知上下文构建。
                self.cl_klines.clear()
                self._last_src_kline_index = -1
                self._initial_variants = ()
                self._initial_signature = None
                self._initial_common_offset = None
                self._initial_left_contexts = ()
                self.initial_context_unresolved = False
                self.initial_excluded_raw_count = 0
                self.initial_source_alternatives = ()

        processed_change = False

        # 调用方每根新 bar 都传入全量 src_klines(= KlineDataProcessor.klines)，前缀里
        # index < _last_src_kline_index 的旧数据必然全部命中下方「情况1」被跳过。
        # KlineDataProcessor 保证 klines[i].index == i（连续 0-based），故可 O(1) 直接切到
        # 尾部起点，免去每根 bar 都 O(n) 空转扫描整列（此前是 O(n^2) 累积热点）。语义不变：
        # 起点恰是第一个 index>=_last 的元素，逐根处理逻辑一字未动；「情况1」的 continue
        # 作为防御保留（万一坐标系异常则退化回原全量扫描的安全行为）。
        start = self._last_src_kline_index
        if start < 0:
            start = 0

        for current_k in src_klines[start:]:

            # --- 情况1: 这是一个旧数据 ---
            if current_k.index < self._last_src_kline_index:
                continue

            # --- 情况2: 这是一个更新数据 ---
            if current_k.index == self._last_src_kline_index:
                # 拆成两个分支避免最常见场景下的无意义重算：
                #   - fast path: dirty_cl_k 只含 current_k → 原地重做最后一个 cl_k 的字段
                #   - slow path: dirty_cl_k 由多根原始 K 合并而来 → 走 pop + 重放逻辑
                if not self.cl_klines:
                    self._process_one_kline(current_k)
                    processed_change = True
                    continue

                dirty_cl_k = self.cl_klines[-1]
                # fast path：脏 cl_k 只由当前这根原始 K 构成
                if (
                    len(dirty_cl_k.klines) == 1
                    and dirty_cl_k.klines[0].index == current_k.index
                ):
                    # 直接 pop 然后单根 _process_one_kline 等价于在原位重算，
                    # 但避免了「重放 valid_prev_klines」的循环开销
                    # （这里就算是空循环也省下了一次列表推导）
                    self.cl_klines.pop()
                    # 把 _last_src_kline_index 回退到上一根的边界，
                    # _process_one_kline 内部会按当前 cl_klines 状态正确续上
                    if self.cl_klines:
                        self._last_src_kline_index = self.cl_klines[-1].klines[-1].index
                    else:
                        self._last_src_kline_index = -1
                    self._process_one_kline(current_k)
                    processed_change = True
                    continue

                # slow path：原通用逻辑，处理「dirty_cl_k 由多根原始 K 合并而来」
                self.cl_klines.pop()
                valid_prev_klines = [k for k in dirty_cl_k.klines if k.index < current_k.index]

                if valid_prev_klines:
                    self._last_src_kline_index = valid_prev_klines[-1].index
                else:
                    if self.cl_klines:
                        self._last_src_kline_index = self.cl_klines[-1].klines[-1].index
                    else:
                        self._last_src_kline_index = -1

                # 重放之前的有效 K 线，恢复现场
                for prev_k in valid_prev_klines:
                    self._process_one_kline(prev_k)

                # 处理当前这根更新后的 K 线
                self._process_one_kline(current_k)
                processed_change = True

            # --- 情况3: 这是一个新数据 (Index 更大) ---
            else:
                self._process_one_kline(current_k)
                processed_change = True

        if processed_change:
            self._structure_revision = self.structure_revision + 1
