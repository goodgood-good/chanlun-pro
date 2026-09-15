# -*- coding: utf-8 -*-
from typing import List, Optional

from chanlun.core.types import FX, BI, CLKline
from chanlun.core.stroke_ranges import StrokeRanges
from chanlun.core.stroke_resolver import StrokeResolver
from chanlun.core.stroke_rules import (
    fractal_kind, is_strictly_more_extreme, old_pair_geometry_valid,
)


def fractal_lock_witness(fx: FX):
    """返回第一次足以确认 ``fx`` 的物理 K 线前缀。

    分型首次可见后，右肩仍可能是正在参与包含合并的缠论 K 线。冷启动批量计算
    必须重放右肩的源 K 线前缀；若直接采用最终合并右肩的最后日期，会泄露后续
    K 线并与逐根计算产生差异。
    """

    cl_klines = [cl_kline for cl_kline in fx.klines if cl_kline is not None]
    if len(cl_klines) < 3:
        return None
    left, middle, right = cl_klines[-3:]
    sources = list(right.klines)
    if not sources:
        return None

    direction = right.up_qs
    for end in range(1, len(sources) + 1):
        prefix = sources[:end]
        if direction == 'up':
            right_high = max(source.h for source in prefix)
            right_low = max(source.l for source in prefix)
        elif direction == 'down':
            right_high = min(source.h for source in prefix)
            right_low = min(source.l for source in prefix)
        else:
            # 未合并的肩部只有一根来源 K 线。方向缺失属于畸形中间状态，
            # 此时重放其最新来源快照。
            right_high = prefix[-1].h
            right_low = prefix[-1].l

        if fx.type == 'ding':
            confirmed = (
                middle.h > left.h
                and middle.h > right_high
                and middle.l > left.l
                and middle.l > right_low
            )
        else:
            confirmed = (
                middle.l < left.l
                and middle.l < right_low
                and middle.h < left.h
                and middle.h < right_high
            )
        if confirmed:
            return prefix[-1].date
    return None


# 兼容核心内部既有私有调用。交易决策上下文需要复用同一套因果确认时刻，
# 不能另写一份“取右肩最后一根 K 线”的近似规则。
_fractal_lock_witness = fractal_lock_witness


class BiCalculator:
    """包含处理后，按当前旧笔工作规则顺序裁决。

    端点满足旧笔间隔与对称的中心区间条件；顺序裁决器执行用户选定的路径乙。
    本类负责包含后的物理分型、收盘证据和批量/增量状态同步。用户P→C选择与
    原文全区间极值的解释差异保留在 docs/stroke_path_b_rules.md，不冒称已获证明。
    """

    def __init__(self):
        self._closed_through = "all"
        self._last_closed_through = None
        self._close_times = {}
        self.bis: List[BI] = []
        self.fxs: List[FX] = []
        self.confirmed_bis: List[BI] = []
        self.pending_bis: List[BI] = []
        self.pending_bi: Optional[BI] = None
        self.cl_klines: List[CLKline] = []
        self._resolver = StrokeResolver()
        self._ranges = StrokeRanges()
        self._last_kline_snapshot: Optional[tuple] = None
        self._last_source_revision: Optional[int] = None
        self._last_processed_kline_count = 0
        self._last_prefix_fingerprint: Optional[tuple] = None
        self._endpoint_stack: List[FX] = []
        self._endpoint_node_ids = []
        self._endpoint_stable_prefix = 0
        self._state_dirty_bi = 0
        self._all_bis: List[BI] = []
        self._qualification_evidence = ()
        self._completion_evidence = ()
        self._continuation_evidence = ()
        self._endpoint_edge_pairs = []
        self.stroke_components = ()
        self.processing_mode = "uninitialized"

    @property
    def continuation_blocked_at(self):
        """尾部重接触及完成前缀的时刻；后续合法推进后清除。"""
        return self._resolver.blocked_at

    @property
    def completion_evidence(self):
        """由收盘事实确认、在本次历史版本内保持的完成事件。"""
        return self._resolver.completions

    @property
    def continuation_evidence(self):
        """下一合格连接的承接证据；不等于完成图形不可修改的证明。"""
        return self._resolver.continuations

    @property
    def contiguous_bis(self):
        """当前连续笔链；兼容既有下游的连续输入接口。"""
        return list(self.stroke_components[0]) if self.stroke_components else []

    @property
    def unresolved_regions(self):
        return self._resolver.boundaries

    def construction_state(self):
        """可序列化的取舍结果；范围边界本身不是新造的一笔。"""
        nodes = self._resolver.nodes
        return {
            "status": "unresolved_connections" if self.unresolved_regions else self.completion_status,
            "processing_mode": self.processing_mode,
            "completion_is_final": bool(self.confirmed_bis),
            "completion_scope": "completed-prefix-within-data-revision",
            "completed_prefix_count": len(self.confirmed_bis),
            "closed_through": (self._closed_through.isoformat()
                               if hasattr(self._closed_through, "isoformat") else self._closed_through),
            "components": [{
                "index": index, "context_pending": index > 0,
                "strokes": [[b.start.k.index, b.end.k.index] for b in values],
            } for index, values in enumerate(self.stroke_components)],
            "unresolved_regions": [{
                "left_center": nodes[b.left].fx.k.index,
                "right_center": nodes[b.right].fx.k.index,
                "observed_at": self._resolver.observed_at(b.observed_by).isoformat(),
                "left_time": nodes[b.left].fx.k.date.isoformat(),
                "right_time": nodes[b.right].fx.k.date.isoformat(),
                "reason": b.reason,
            } for i, b in enumerate(self.unresolved_regions)],
            "continuation_blocked_at": (self.continuation_blocked_at.isoformat()
                                        if self.continuation_blocked_at else None),
        }

    @property
    def unresolved_endpoint_choices(self):
        """当前已经识别的同价端点取舍依赖，索引指向分型顺序表。"""
        return self._resolver.unresolved_equal_choices

    @property
    def qualification_evidence(self):
        """每条已选连接的物理可见时间与选入时间；不等于永久完成。"""
        return self._resolver.qualifications

    @property
    def completion_status(self):
        return "tail_pending" if self.bis else "no_qualified_stroke"

    @property
    def selection_revisions(self):
        """记录暂定端点的重选；不能把被撤换的连接伪装成从未出现。"""
        return tuple(self._resolver.revisions)

    @staticmethod
    def _check_endpoint_geometry(fx1: FX, fx2: FX) -> bool:
        """L062/L077 旧笔独立 K 线与两端价格条件，不含区间核验。"""
        return old_pair_geometry_valid(fx1, fx2)

    def _check_stroke_validity(self, fx1: FX, fx2: FX) -> bool:
        """用户工作规则的局部资格；不能单独决定取舍或完成。

        间隔与中心区间来自L062/L077的形式化；不以已舍内部分型极值
        否决用户选定的P→C。L066差异另作只读审计，不删除原始价格。
        """
        return (self._check_endpoint_geometry(fx1, fx2)
                and 0 <= fx1.k.index < fx2.k.index < len(self.cl_klines))

    def audit_endpoint_ranges(self):
        """报告与L066全区间极值解释的差异，不作为路径乙的硬否决。"""
        from chanlun.core.stroke_audit import audit_bi_ranges
        return audit_bi_ranges(self.bis, self.cl_klines)

    def audit_endpoint_adjacency(self):
        """旧全区间模型的可分解性诊断，不代表当前路径乙的入选规则。"""
        from chanlun.core.stroke_audit import audit_bi_adjacency
        return audit_bi_adjacency(self.bis, self.fxs, self.cl_klines)

    @staticmethod
    def _is_more_extreme(new_fx: FX, old_fx: FX) -> bool:
        return is_strictly_more_extreme(new_fx, old_fx)

    def _find_fractal(self, k1: CLKline, k2: CLKline, k3: CLKline) -> Optional[FX]:
        kind = fractal_kind(k1, k2, k3)
        if kind is None:
            return None
        return FX(_type=kind, k=k2, klines=[k1, k2, k3], val=k2.h if kind == "ding" else k2.l)

    def _collect_fxs(self, cl_klines: List[CLKline]) -> List[FX]:
        fxs = []
        for i in range(1, len(cl_klines) - 1):
            fx = self._find_fractal(cl_klines[i - 1], cl_klines[i], cl_klines[i + 1])
            if fx is not None:
                fx.index = len(fxs)
                fxs.append(fx)
        return fxs

    def _create_bi(self, start_fx: FX, end_fx: FX, index: int) -> BI:
        if not self._check_stroke_validity(start_fx, end_fx):
            raise ValueError("stroke path emitted an invalid endpoint interval")
        bi = BI(start=start_fx, end=end_fx, _type="up" if start_fx.type == "di" else "down", index=index)
        # 两端具备资格只生成暂定连接，不能通过私有参数绕过完成证据。
        return bi

    def _build_endpoint_stack(self, fxs: List[FX], incremental=False, changed_from=None) -> List[FX]:
        """兼容既有端点列表接口，以相邻关系和证据驱动尾部更新。"""
        previous_edges = self._endpoint_edge_pairs
        self._state_dirty_bi = len(self._all_bis)
        count = len(self._resolver.nodes)
        if incremental and changed_from == count - 1 and count:
            self._resolver.rewind_last()
        elif not incremental or (changed_from is not None and changed_from < count):
            self._resolver = StrokeResolver.build_batch(
                fxs, self._check_stroke_validity, _fractal_lock_witness,
                closed_through=self._closed_through,
                close_times=self._close_times,
            )
            self._state_dirty_bi = 0
        # 先移除被盘中改写的分型，再提交已经收盘的旧见证，随后处理新分型。
        self._resolver.close_times = self._close_times
        self._resolver.confirm_through(self._closed_through)
        for fx in fxs[len(self._resolver.nodes):]:
            self._resolver.advance(fx, self._check_stroke_validity, _fractal_lock_witness(fx))
        # 资格时间改变而坐标不变时，也要更新下游对象缓存。
        current_evidence = self.qualification_evidence
        unchanged = 0
        for old, new in zip(self._qualification_evidence, current_evidence):
            if old != new:
                break
            unchanged += 1
        self._state_dirty_bi = min(self._state_dirty_bi, unchanged)
        self._qualification_evidence = current_evidence
        unchanged_completion = 0
        for old, new in zip(self._completion_evidence, self.completion_evidence):
            if old != new:
                break
            unchanged_completion += 1
        # 状态变化即使没有端点坐标变化，也必须重建对应 BI 对象。
        if self._completion_evidence != self.completion_evidence:
            self._state_dirty_bi = min(self._state_dirty_bi, unchanged_completion)
        self._completion_evidence = self.completion_evidence
        old_observations = {(c.start, c.end): c for c in self._continuation_evidence}
        new_observations = {(c.start, c.end): c for c in self.continuation_evidence}
        for offset, q in enumerate(current_evidence):
            if old_observations.get((q.start, q.end)) != new_observations.get((q.start, q.end)):
                self._state_dirty_bi = min(self._state_dirty_bi, offset)
        self._continuation_evidence = self.continuation_evidence
        node_ids = [i for path in self._resolver.components() for i in path]
        endpoints = [self._resolver.nodes[index].fx for index in node_ids]
        edge_pairs = [(self._resolver.nodes[a].fx, self._resolver.nodes[b].fx)
                      for a, b in self._resolver.edges()]
        stable = 0
        if incremental:
            for old, new in zip(previous_edges, edge_pairs):
                if old[0] is not new[0] or old[1] is not new[1]:
                    break
                stable += 1
        self._endpoint_stack = endpoints
        self._endpoint_node_ids = node_ids
        self._endpoint_edge_pairs = edge_pairs
        self._endpoint_stable_prefix = stable + 1
        return endpoints

    def _rebuild_from_fxs(self, fxs: List[FX], incremental=False, changed_from=None):
        self._build_endpoint_stack(fxs, incremental, changed_from)
        stable_bi = min(max(0, self._endpoint_stable_prefix - 1), self._state_dirty_bi)
        count = len(self._qualification_evidence)
        if stable_bi == len(self._all_bis) == count:
            return

        # 端点资格不冒充永久完成；重选尾部使 XD 的对象前缀缓存重新计算。
        del self._all_bis[stable_bi:]
        observations = {(c.start, c.end): c for c in self.continuation_evidence}
        for index in range(stable_bi, count):
            evidence = self._qualification_evidence[index]
            start, end = self._endpoint_edge_pairs[index]
            bi = self._create_bi(start, end, index)
            bi.fractal_visible_at = evidence.fractal_visible_at
            bi.selected_at = evidence.selected_at
            bi.selection_pending = evidence.selection_pending
            bi.component_index = evidence.component
            continuation = observations.get((evidence.start, evidence.end))
            bi.successor_observed_at = continuation.witnessed_at if continuation else None
            if index < len(self._completion_evidence):
                completion = self._completion_evidence[index]
                bi.locked_at = completion.witnessed_at
                bi.completion_witness = tuple(self._resolver.nodes[i].fx.k.index
                                              for i in completion.witness_path)
                bi.forming = False
            self._all_bis.append(bi)
        confirmed_count = len(self._completion_evidence)
        self.confirmed_bis = self._all_bis[:confirmed_count]
        self.pending_bis = self._all_bis[confirmed_count:]
        self.pending_bi = self.pending_bis[-1] if self.pending_bis else None
        self.bis = list(self._all_bis)
        groups = [[] for _ in self._resolver.components()]
        for bi in self.bis:
            groups[bi.component_index].append(bi)
        self.stroke_components = tuple(tuple(group) for group in groups)

    def calculate_batch(self, cl_klines: List[CLKline], *, source_revision=None, closed_through="all", close_times=None):
        """一次性重建已知快照；不继承先前实例上的端点取舍。"""
        self.__init__()
        self.calculate(cl_klines, source_revision=source_revision, closed_through=closed_through, close_times=close_times)

    @staticmethod
    def _kline_sig(k: CLKline) -> tuple:
        """覆盖合并结果、极值来源和确认分型所需的物理 K 线证据。"""
        return (
            k.index, k.k_index, k.date, k.h, k.l, k.o, k.c, k.a,
            k.n, k.q, k.up_qs,
            tuple((s.index, s.date, s.h, s.l, s.o, s.c, s.a) for s in k.klines),
            tuple(BiCalculator._kline_sig(c) for c in getattr(k, "initial_context_alternatives", ())),
        )

    @classmethod
    def _fx_sig(cls, fx: FX) -> tuple:
        return (
            fx.type,
            fx.val,
            cls._kline_sig(fx.k),
            tuple(cls._kline_sig(k) if k is not None else None for k in fx.klines),
        )

    def _snapshot_matches(
        self,
        cl_klines: List[CLKline],
        source_revision: Optional[int],
    ) -> bool:
        if (
            source_revision is None
            or self._closed_through != self._last_closed_through
            or source_revision != getattr(self, "_last_source_revision", None)
            or not self._last_kline_snapshot
            or not cl_klines
        ):
            return False
        current_last = cl_klines[-1]
        last_idx, last_h, last_l = self._last_kline_snapshot
        return (
            current_last.index == last_idx
            and current_last.h == last_h
            and current_last.l == last_l
        )

    def _update_snapshot(self, source_revision: Optional[int]) -> None:
        self._last_closed_through = self._closed_through
        self._last_source_revision = source_revision
        if not self.cl_klines:
            self._last_kline_snapshot = None
            return
        last_k = self.cl_klines[-1]
        self._last_kline_snapshot = (last_k.index, last_k.h, last_k.l)

    def calculate(
        self,
        cl_klines: List[CLKline],
        *,
        source_revision: Optional[int] = None,
        validated_incremental_prefix: bool = False,
        closed_through="all",
        close_times=None,
    ):
        """
        计算笔列表。

        分三档处理：
          1. 可信数据代次 + 末根 snapshot 同时命中 → 直接 return
          2. 数据代次前进 + 前缀指纹命中 + 仅末尾追加 → 增量扩展 fxs
          3. 其他情况（前缀变更、长度缩短、首次计算）→ 全量重放
        没有 ``source_revision`` 的直接调用不信任列表的历史前缀，一律全量重放。
        生产 CL 路径由包含处理器提供单调代次并显式认证历史前缀未改写，维持
        尾部增量性能。代次前进但没有前缀认证时仍全量重放。
        """
        self._closed_through = closed_through
        self._close_times = {} if close_times is None else close_times
        if not cl_klines:
            self.cl_klines = []
            self.fxs = []
            self.confirmed_bis = []
            self.pending_bi = None
            self.pending_bis = []
            self._resolver = StrokeResolver()
            self._ranges = StrokeRanges()
            self._endpoint_node_ids = []
            self._qualification_evidence = ()
            self._completion_evidence = ()
            self._state_dirty_bi = 0
            self.bis = []
            self._last_kline_snapshot = None
            self._last_source_revision = None
            self._last_closed_through = None
            self._last_processed_kline_count = 0
            self._last_prefix_fingerprint = None
            self._endpoint_stack = []
            self._endpoint_stable_prefix = 0
            self._all_bis = []
            self._continuation_evidence = ()
            self._endpoint_edge_pairs = []
            self.stroke_components = ()
            self.processing_mode = "batch"
            return

        # 档 1：调用方数据代次和末根快照同时命中（数据完全没变）
        if self._snapshot_matches(cl_klines, source_revision):
            self.processing_mode = "unchanged"
            return

        previous_revision = getattr(self, "_last_source_revision", None)
        trusted_forward_revision = (
            validated_incremental_prefix
            and source_revision is not None
            and previous_revision is not None
            and source_revision > previous_revision
        )

        # 校验和尾部重建必须读取本次包含处理结果。
        self.cl_klines = cl_klines
        # 档 2：只有可信数据代次向前推进时才尝试增量扩展。
        if trusted_forward_revision and self._try_incremental_extend(cl_klines):
            self.processing_mode = "incremental"
            self._update_snapshot(source_revision)
            self._update_prefix_fingerprint(cl_klines)
            return

        # 档 3：降级全量
        self._ranges.update(cl_klines)
        self.processing_mode = "batch"
        self.fxs = self._collect_fxs(cl_klines)
        self._rebuild_from_fxs(self.fxs)
        self._update_snapshot(source_revision)
        self._update_prefix_fingerprint(cl_klines)

    def _update_prefix_fingerprint(self, cl_klines: List[CLKline]) -> None:
        """记录本次处理后的前缀指纹，供下次增量判定使用。

        指纹覆盖：(总长度, 倒数第 2 根的完整来源签名)。
        - 总长度：用于判断是否「仅末尾追加」
        - 倒数第 2 根：cl_kline_process 在末尾追加新 K 时一般不动倒数第 2 根，
          但若发生包含合并，倒数第 2 根可能被改写 → 指纹不匹配 → 降级全量。
        """
        self._last_processed_kline_count = len(cl_klines)
        if len(cl_klines) >= 2:
            sec_last = cl_klines[-2]
            self._last_prefix_fingerprint = (
                len(cl_klines),
                self._kline_sig(sec_last),
            )
        else:
            # 不足 2 根时不维护指纹，下次必定走全量
            self._last_prefix_fingerprint = None

    def _try_incremental_extend(self, cl_klines: List[CLKline]) -> bool:
        """尝试增量扩展笔列表。

        命中条件（任一不满足即返回 False，外层会降级全量）：
          1. 上一轮已经处理过 ≥ 3 根 cl_klines（否则没有可信前缀）
          2. 本轮长度 ≥ 上轮长度（不允许缩短，缩短意味着回放）
          3. 上一轮指纹存在且仍命中（前缀未被改写）
          4. 上一轮位于「倒数第 2 根」的指纹在新 cl_klines 中位置不变

        从旧倒数第二根中心起重新识别分型，用完整证据比较后替换尾部。
        纯追加沿用顺序裁决结果；旧尾部分型的证据变化时恢复检查点。
        """
        if self._last_processed_kline_count < 3:
            return False
        if self._last_prefix_fingerprint is None:
            return False
        if len(cl_klines) < self._last_processed_kline_count:
            return False
        prev_len, previous_anchor = self._last_prefix_fingerprint
        # 新 cl_klines 在 prev_len-2 位置应该仍然是当时的「倒数第 2 根」
        anchor_pos = prev_len - 2
        if anchor_pos < 0 or anchor_pos >= len(cl_klines):
            return False
        anchor = cl_klines[anchor_pos]
        if self._kline_sig(anchor) != previous_anchor:
            return False

        self._ranges.update(cl_klines, start=prev_len - 1)

        # 从 prev_len-2 起重做中心：旧倒数第二根的右肩可能被更新，
        # 旧末根及新增 K 线也可能获得右肩，不能只扫描新增中心。
        new_fxs = self._incremental_collect_fxs(cl_klines, start=max(prev_len - 2, 1))
        # 用新 fxs 替换原 fxs 的尾部（从 anchor_pos-1 之后的所有分型都重做）。
        # keep_until = fxs 中 k.index < anchor.index 的数量。fxs 按 k.index 严格升序、
        # 且 anchor 在尾部(prev_len-2)→变化区只是末尾一小段;故**从尾向前扫**(O(尾段)),
        # 取代原从头全扫 O(F)(walk-forward 每根 O(F) → 整体 O(n²) 主源)。等价:升序下
        # 「< anchor 的前缀」与「>= anchor 的后缀」互补,两向扫得同一边界。
        keep_until = len(self.fxs)
        for i in range(len(self.fxs) - 1, -1, -1):
            if self.fxs[i].k.index >= anchor.index:
                keep_until = i
            else:
                break
        # 给 new_fxs 重新编号（接续保留前缀）
        for offset, fx in enumerate(new_fxs):
            fx.index = keep_until + offset
        if keep_until == len(self.fxs) and not new_fxs:
            self._rebuild_from_fxs(self.fxs, incremental=True)
            return True
        old_tail = self.fxs[keep_until:]
        if len(old_tail) == len(new_fxs) and all(
            self._fx_sig(old) == self._fx_sig(new)
            for old, new in zip(old_tail, new_fxs)
        ):
            self._rebuild_from_fxs(self.fxs, incremental=True)
            return True
        # 原地删尾 + extend(O(尾段))取代 kept_fxs + new_fxs 的 O(F) 切片+拼接;前缀 FX
        # 对象不动,_rebuild_from_fxs 按 fxs **内容**(经 _fx_sig,非列表身份)增量,等价。
        del self.fxs[keep_until:]
        self.fxs.extend(new_fxs)
        # 将实际发生变化的尾部边界传给裁决器，恢复该分型之前的状态。
        self._rebuild_from_fxs(self.fxs, incremental=True, changed_from=keep_until)
        return True

    def _incremental_collect_fxs(self, cl_klines: List[CLKline], start: int) -> List[FX]:
        """从 cl_klines[start..-1] 范围内识别分型。

        与 _collect_fxs 的区别：
          - 起始位置可控（避免重复扫前缀）
          - 不维护全局 index（由调用方在合并后统一编号）
        """
        fxs: List[FX] = []
        # 分型识别需要 [i-1, i, i+1]，所以 i 至少从 1 开始
        i_start = max(start, 1)
        for i in range(i_start, len(cl_klines) - 1):
            fx = self._find_fractal(cl_klines[i - 1], cl_klines[i], cl_klines[i + 1])
            if fx is not None:
                fxs.append(fx)
        return fxs
