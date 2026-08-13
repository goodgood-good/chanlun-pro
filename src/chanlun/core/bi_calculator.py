# -*- coding: utf-8 -*-
from typing import List, Optional

from chanlun.core.types import FX, BI, CLKline


def _fractal_lock_witness(fx: FX):
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


class BiCalculator:
    """
    笔计算器。

    对外仍保持：
    - self.fxs 为识别出的分型列表
    - self.bis 为用于展示/下游消费的笔列表，最后一笔可能未完成

    对内采用“已确认笔 + 当前待定笔”的状态机，每次在最新缠论 K 线上重放，
    优先保证结果正确与全量/增量一致性。
    生产规则只允许严格笔。
    """

    def __init__(self):
        self.bis: List[BI] = []
        self.fxs: List[FX] = []
        self.confirmed_bis: List[BI] = []
        self.pending_bi: Optional[BI] = None
        self.bi_index: int = 0
        self.cl_klines: List[CLKline] = []
        self._last_kline_snapshot: Optional[tuple] = None
        # 增量字段必须在此初始化：calculate() 里 _try_incremental_extend 先于
        # _update_prefix_fingerprint（唯一赋值入口）调用，否则首次调用 AttributeError。
        self._last_processed_kline_count: int = 0
        self._last_prefix_fingerprint: Optional[tuple] = None
        # 持久化单调端点栈(增量):fxs 在增量路径恒为 append-only(前缀不改写,见
        # _try_incremental_extend,实测 rewrite-depth 恒 0),故维护持久栈,新分型
        # 只续跑单调栈(均摊 O(1)/fx),消除每次全量重建的 O(F²)。全量降级(档3)
        # 用新 FX 对象重建,故 incremental=False 时重置;末尾签名再做双保险。
        self._endpoint_stack: List[FX] = []
        self._endpoint_stack_n: int = 0
        self._endpoint_stack_tail_sig: Optional[tuple] = None
        self._endpoint_stable_prefix: int = 0
        # 持久笔列表(建笔增量):复用稳定前缀笔、只重建活跃尾部,消除每次全量
        # _create_bi 的 O(n·B)。与 _endpoint_stack 对齐(len = 端点数 - 1)。
        self._all_bis: List[BI] = []
        # 重索引增量:上次 pending 笔位置。_reindex_bis 只重置
        # [min(稳定前缀边界, 上次pending位置), 末尾] 的 index/done——稳定前缀 confirmed
        # 笔的 index(由 _create_bi 设)与 done(历轮已 True、归纳保持)不变,而 done 的唯一
        # 变化(pending→confirmed)只发生在「上次pending位置」。消除原 O(全部笔)/调用。
        self._prev_pending_pos: int = -1

    def _check_stroke_validity(self, fx1: FX, fx2: FX) -> bool:
        """检查两个分型是否能构成有效的一笔。

        顶底之间必须间隔足够的独立（包含处理后）缠论 K 线，即合并
        缠论 K 线坐标 ``fx.k.index`` 距离至少为 4。
        """
        if fx1.type == fx2.type:
            return False

        if fx2.k.index <= fx1.k.index:
            return False
        if (fx2.k.index - fx1.k.index) < 4:
            return False

        if fx1.type == 'ding':
            if fx2.val >= fx1.val:
                return False
        else:
            if fx2.val <= fx1.val:
                return False

        return True

    @staticmethod
    def _is_more_extreme(new_fx: FX, old_fx: FX) -> bool:
        if new_fx.type != old_fx.type:
            return False
        if new_fx.type == 'ding':
            return new_fx.val > old_fx.val
        return new_fx.val < old_fx.val

    def _find_fractal(self, k1: CLKline, k2: CLKline, k3: CLKline) -> Optional[FX]:
        """简化版分型识别。"""
        if k2.h > k1.h and k2.h > k3.h and k2.l > k1.l and k2.l > k3.l:
            return FX(_type='ding', k=k2, klines=[k1, k2, k3], val=k2.h)
        if k2.l < k1.l and k2.l < k3.l and k2.h < k1.h and k2.h < k3.h:
            return FX(_type='di', k=k2, klines=[k1, k2, k3], val=k2.l)
        return None

    def _collect_fxs(self, cl_klines: List[CLKline]) -> List[FX]:
        """全量扫描缠论 K 线序列，识别所有分型。"""
        fxs: List[FX] = []
        for i in range(1, len(cl_klines) - 1):
            current_fx = self._find_fractal(cl_klines[i - 1], cl_klines[i], cl_klines[i + 1])
            if current_fx is None:
                continue
            current_fx.index = len(fxs)
            fxs.append(current_fx)
        return fxs

    def _create_bi(self, start_fx: FX, end_fx: FX, index: int, done: bool) -> BI:
        bi_type = 'up' if start_fx.type == 'di' else 'down'
        bi = BI(start=start_fx, end=end_fx, _type=bi_type, index=index)
        self._set_bi_completion(bi, done)
        return bi

    @staticmethod
    def _set_bi_completion(
        bi: BI,
        done: bool,
        lock_witness=None,
    ) -> None:
        """同时设置笔的完成状态和下一端点见证。

        即使自身结束分型已经可见，该笔仍是当前待定笔，后续相反端点仍可能替换其
        尾部；只有下一笔端点存在后它才不可变。若只使用 ``bi.end``，会把锁定时刻
        错误提前到该笔仍处于待定状态的前缀。
        """

        bi._end.done = done
        if not done:
            bi.locked_at = None
            return

        witness = lock_witness
        if witness is None:
            witness = _fractal_lock_witness(bi._end)
        if witness is None:
            raise ValueError("completed BI requires physical end-fractal evidence")
        if bi.locked_at is not None and bi.locked_at != witness:
            raise RuntimeError("completed BI lock witness must not move")
        bi.locked_at = witness

    def _first_following_endpoint_witness(self, bi: BI):
        """重放第一个使后续笔成立的更晚分型。"""

        for candidate in self.fxs:
            if candidate.k.index <= bi._end.k.index:
                continue
            if candidate.type == bi._end.type:
                continue
            if not self._check_stroke_validity(bi._end, candidate):
                continue
            witness = _fractal_lock_witness(candidate)
            if witness is not None:
                return witness
        raise ValueError("completed BI requires a following endpoint witness")

    def _reindex_bis(self, stable_from: int = 0):
        """重置笔的 index/done(增量版)。

        仅 [start, 末尾] 需要重置:``start = min(稳定前缀边界 stable_from, 上次 pending 位置)``。
        ① start 之前的 confirmed 笔由 _create_bi 设过 index、且历轮已置 done=True(归纳保持),
        不变;② done 的唯一变化=上次 pending 笔(_prev_pending_pos)本轮转 confirmed,需补
        done=True——故 start 下探到该位置。直连 ``_end`` 避开 property getter(原 _reindex 全量
        遍历占链路 ~30% tottime、.end getter 被调数十万次)。正确性由 test_incremental_equivalence
        + test_bi_reindex_dense(逐前缀含 done) 守护。
        """
        start = stable_from
        if 0 <= self._prev_pending_pos < start:
            start = self._prev_pending_pos
        if start < 0:
            start = 0
        nconf = len(self.confirmed_bis)
        for i in range(start, nconf):
            bi = self.confirmed_bis[i]
            bi.index = i
            self._set_bi_completion(
                bi,
                True,
                lock_witness=self._first_following_endpoint_witness(bi),
            )

        if self.pending_bi is not None:
            self.pending_bi.index = nconf
            self._set_bi_completion(self.pending_bi, False)
            self._prev_pending_pos = nconf
        else:
            self._prev_pending_pos = -1

        self.bi_index = nconf + (1 if self.pending_bi is not None else 0)
        self.bis = list(self.confirmed_bis)
        if self.pending_bi is not None:
            self.bis.append(self.pending_bi)

    def _build_endpoint_stack(self, fxs: List[FX], incremental: bool = False) -> List[FX]:
        """单调栈构造笔端点序列(持久栈增量版)。

        ``fxs`` 在增量路径(档2 _try_incremental_extend)恒为 append-only —— 前缀
        不改写、仅尾部追加 0~1 个新分型(实测 rewrite-depth 恒 0)。故维护持久栈
        ``self._endpoint_stack``:``incremental=True`` 且本次 fxs 是上次 append 扩展
        (前 ``_endpoint_stack_n`` 个未变,O(1) 末尾签名校验)时只续跑新增分型
        (均摊 O(1)/fx);否则(全量降级/校验失败)重置栈从头重建。消除原本每次
        对全部分型重跑单调栈的 O(F²)。全量降级走新 FX 对象重建,故档3
        ``_rebuild_from_fxs(incremental=False)`` 传 False。

        单调栈语义(对每个分型跑 while 循环,逐字保留原情形①②③):
        - 情形①同类：与栈顶同类，更极端则取代栈顶，否则丢弃；
        - 情形②异类成笔：与栈顶异类且满足成笔条件 → 入栈；
        - 情形③异类不成笔：栈顶是被新分型与其同类前驱夹击的「多余端点」则弹出，
          否则（真端点/新分型不够极端/栈不足 3 时兜底）丢弃新分型。
        不变量：stack 始终顶底严格交替（情形③取 stack[-2]/stack[-3] 依赖此）。
        """
        n = self._endpoint_stack_n
        can_incr = (
            incremental
            and 0 < n <= len(fxs)
            and self._endpoint_stack_tail_sig is not None
            and self._fx_sig(fxs[n - 1]) == self._endpoint_stack_tail_sig
        )
        if can_incr:
            stack = self._endpoint_stack
            new_fxs = fxs[n:]
            stable = len(stack)
        else:
            stack = []
            new_fxs = fxs
            stable = 0
        for fx in new_fxs:
            while True:
                if not stack:
                    stack.append(fx)
                    break
                last = stack[-1]
                if fx.type == last.type:
                    # 情形①：同类，保留更极端者
                    if self._is_more_extreme(fx, last):
                        stack.pop()
                        if len(stack) < stable:
                            stable = len(stack)
                        continue
                    break
                # _check_stroke_validity 要求后者 K 线 index 更大；fxs 按
                # K 线 index 升序产出、fx 恒晚于栈内任意元素，方向前提成立。
                if self._check_stroke_validity(last, fx):
                    # 情形②：异类成笔
                    stack.append(fx)
                    break
                # 情形③：异类不成笔
                if len(stack) < 3:
                    break  # 栈不足，无法判定，丢弃 fx
                prev = stack[-2]
                # prev→last 满足成笔距离 ⇒ last 是「合法距离的反弹/回调端点」,不应被
                # 「过路的近距分型 fx」回溯吞并。丢弃 fx,待后续达成笔距离的真实端点经
                # 情形②自然接出下一笔。
                if self._check_stroke_validity(prev, last):
                    break
                # 以下原「夹击弹出」回溯分支:因栈内相邻对 (prev, last) 恒满足成笔距离
                # (均经情形②压入 / 情形①同类替换保持该不变量),上面守卫恒 break ⇒ 此
                # 分支恒不可达,保留作防御性兜底(未来栈维护契约若变动致相邻对可不合法)。
                last_peer = stack[-3]  # 栈顶 last 的同类前驱
                if self._is_more_extreme(last, last_peer):
                    break  # last 是创新高/新低的真端点 → 丢弃 fx
                if not self._is_more_extreme(fx, prev):
                    break  # fx 不够格取代 prev → 丢弃 fx
                # last 被 fx 与 prev 夹击、显得多余 → 弹出 last 与 prev
                stack.pop()
                stack.pop()
                if len(stack) < stable:
                    stable = len(stack)
                continue
        self._endpoint_stack = stack
        self._endpoint_stack_n = len(fxs)
        self._endpoint_stack_tail_sig = self._fx_sig(fxs[-1]) if fxs else None
        self._endpoint_stable_prefix = stable
        return stack

    def _rebuild_from_fxs(self, fxs: List[FX], incremental: bool = False):
        """从分型列表用单调栈重建笔列表(端点栈 + 建笔双增量)。

        ``incremental=True``(档2 append-only)→ 持久栈增量 + 复用稳定前缀笔、
        只重建活跃尾部笔;档3 全量降级用新 FX 对象,传 False 触发重置全建。

        done 不再全量重置:笔 FX 的 ``done`` 仅 ``BI.is_done()`` 经 ``bi.end.done``
        消费(XD 读的是自身 XLFX.done、非笔 FX),且 FX 新建默认 done=True;
        ``_reindex_bis`` 会覆盖所有当前笔端点的 done(confirmed=True/pending=False),
        非端点 FX 的残留 done 无人读,故省去原 O(F) 的全量 ``fx.done=True``。
        """
        endpoints = self._build_endpoint_stack(fxs, incremental=incremental)

        # 端点栈稳定前缀 → 笔稳定前缀:bi[i] 用 endpoints[i] 与 endpoints[i+1],
        # 故 endpoints[:stable] 稳定 ⇒ bi[:stable-1] 可复用、bi[stable-1:] 重建。
        stable_bi = max(0, self._endpoint_stable_prefix - 1)
        bis = self._all_bis
        del bis[stable_bi:]
        for i in range(stable_bi, len(endpoints) - 1):
            bis.append(self._create_bi(endpoints[i], endpoints[i + 1], i, False))

        if bis:
            self.confirmed_bis = bis[:-1]
            self.pending_bi = bis[-1]
        else:
            self.confirmed_bis = []
            self.pending_bi = None

        self._reindex_bis(stable_bi)

    @staticmethod
    def _fx_sig(fx: FX) -> tuple:
        return (
            fx.type,
            getattr(fx.k, "index", None),
            fx.val,
        )

    def _snapshot_matches(self, cl_klines: List[CLKline]) -> bool:
        if not self._last_kline_snapshot or not cl_klines:
            return False
        current_last = cl_klines[-1]
        last_idx, last_h, last_l = self._last_kline_snapshot
        return (
            current_last.index == last_idx
            and current_last.h == last_h
            and current_last.l == last_l
        )

    def _update_snapshot(self):
        if not self.cl_klines:
            self._last_kline_snapshot = None
            return
        last_k = self.cl_klines[-1]
        self._last_kline_snapshot = (last_k.index, last_k.h, last_k.l)

    def calculate(self, cl_klines: List[CLKline]):
        """
        计算笔列表。

        分三档处理：
          1. 末根 snapshot 命中 → 直接 return
          2. 前缀指纹命中 + 仅末尾追加 → 增量扩展 fxs
          3. 其他情况（前缀变更、长度缩短、首次计算）→ 全量重放
        增量分支只在前 N-1 根缠论 K 线完全没动、仅末尾追加时启用，
        否则一律降级到全量，正确性优先。
        """
        if not cl_klines:
            self.cl_klines = []
            self.fxs = []
            self.confirmed_bis = []
            self.pending_bi = None
            self.bis = []
            self.bi_index = 0
            self._last_kline_snapshot = None
            self._last_processed_kline_count = 0
            self._last_prefix_fingerprint = None
            self._endpoint_stack = []
            self._endpoint_stack_n = 0
            self._endpoint_stack_tail_sig = None
            self._endpoint_stable_prefix = 0
            self._all_bis = []
            self._prev_pending_pos = -1
            return

        # 档 1：末根快照命中（数据完全没变）
        if self._snapshot_matches(cl_klines):
            return

        # 档 2：尝试增量扩展
        if self._try_incremental_extend(cl_klines):
            self.cl_klines = cl_klines
            self._update_snapshot()
            self._update_prefix_fingerprint(cl_klines)
            return

        # 档 3：降级全量
        self.cl_klines = cl_klines
        self.fxs = self._collect_fxs(cl_klines)
        self._rebuild_from_fxs(self.fxs)
        self._update_snapshot()
        self._update_prefix_fingerprint(cl_klines)

    def _update_prefix_fingerprint(self, cl_klines: List[CLKline]) -> None:
        """记录本次处理后的前缀指纹，供下次增量判定使用。

        指纹覆盖：(总长度, 倒数第 2 根的 index/h/l)。
        - 总长度：用于判断是否「仅末尾追加」
        - 倒数第 2 根：cl_kline_process 在末尾追加新 K 时一般不动倒数第 2 根，
          但若发生包含合并，倒数第 2 根可能被改写 → 指纹不匹配 → 降级全量。
        """
        self._last_processed_kline_count = len(cl_klines)
        if len(cl_klines) >= 2:
            sec_last = cl_klines[-2]
            self._last_prefix_fingerprint = (
                len(cl_klines),
                sec_last.index,
                sec_last.h,
                sec_last.l,
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

        命中后的策略：
          - 在新增的 cl_klines 上做增量分型识别
          - 把新分型 append 到 self.fxs 后端
          - 重新跑 _rebuild_from_fxs（fxs 全量但分型识别量变小）

        注：保守起见，增量分支只省 _collect_fxs 的 O(N) 扫描；
            _rebuild_from_fxs 仍跑全量，避免笔状态机回退的复杂度。
            实测在 1m 长序列上，_collect_fxs 占比超过 60%，效果显著。
        """
        if self._last_processed_kline_count < 3:
            return False
        if self._last_prefix_fingerprint is None:
            return False
        if len(cl_klines) < self._last_processed_kline_count:
            return False
        prev_len, prev_sec_idx, prev_sec_h, prev_sec_l = self._last_prefix_fingerprint
        # 新 cl_klines 在 prev_len-2 位置应该仍然是当时的「倒数第 2 根」
        anchor_pos = prev_len - 2
        if anchor_pos < 0 or anchor_pos >= len(cl_klines):
            return False
        anchor = cl_klines[anchor_pos]
        if anchor.index != prev_sec_idx or anchor.h != prev_sec_h or anchor.l != prev_sec_l:
            return False

        # 增量识别新分型：从 prev_len-1 开始（旧的"末根"现在有了 right K 可以判分型），
        # 因为分型需要 [i-1, i, i+1] 三根上下文。
        # _collect_fxs 已经按 1..N-1 扫描，重做这一段只针对新增段。
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
            return True
        old_tail = self.fxs[keep_until:]
        if len(old_tail) == len(new_fxs) and all(
            self._fx_sig(old) == self._fx_sig(new)
            for old, new in zip(old_tail, new_fxs)
        ):
            return True
        # 原地删尾 + extend(O(尾段))取代 kept_fxs + new_fxs 的 O(F) 切片+拼接;前缀 FX
        # 对象不动,_rebuild_from_fxs 按 fxs **内容**(经 _fx_sig,非列表身份)增量,等价。
        del self.fxs[keep_until:]
        self.fxs.extend(new_fxs)
        # 笔状态机走持久栈增量(append-only,见 _build_endpoint_stack)
        self._rebuild_from_fxs(self.fxs, incremental=True)
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
