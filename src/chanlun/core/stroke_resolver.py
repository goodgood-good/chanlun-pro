"""旧笔的顺序取舍与完成事件。

基础定义：D:/缠论/chanlun_lesson_corpus，L062 37–118、L077 139–148、
247–283；完成后不再修改：L069 151–169。路径乙、P→C、T0→E 和第三条
合格连接确认第一笔来自用户判定，不能冒称作者逐字规则。P→C 与 L066
256–280 作者回复的区间极值要求尚未完成理论协调，见 docs/stroke_path_b_rules.md。
"""

from dataclasses import dataclass
from datetime import datetime

from chanlun.core.stroke_rules import is_strictly_more_extreme
from chanlun.core.types import FX


@dataclass(frozen=True)
class StrokeEndpoint:
    fx: FX
    parent: int = -1
    origin: int = -1


@dataclass(frozen=True)
class StrokeQualification:
    start: int
    end: int
    fractal_visible_at: datetime
    selected_at: datetime
    selected_by: int
    component: int = 0
    selection_pending: bool = False


@dataclass(frozen=True)
class StrokeContinuation:
    """已有合格后继；与不可改写的完成事件分别保存。"""
    start: int
    end: int
    following_end: int
    witnessed_at: datetime


@dataclass(frozen=True)
class StrokeCompletion:
    """A—B完成时的A、B、C、D事实，后续修改C、D不撤销此记录。"""
    start: int
    end: int
    reverse_end: int
    following_end: int
    witnessed_at: datetime
    physical_witness_at: datetime | None = None

    @property
    def witness_path(self):
        return (self.start, self.end, self.reverse_end, self.following_end)


@dataclass(frozen=True)
class StrokeDecision:
    fractal: int
    action: str
    previous_end: int
    selected_end: int
    revised_from: int | None
    observed_at: datetime | None
    revises_confirmed_selection: bool = False
    path_conflicts: tuple = ()


class StrokeResolver:
    """保留物理分型，取舍当前尾部；不重新搜索已经舍弃的历史路径。

    closed_through='all'表示输入都是收盘K线；None表示尚无收盘事实。
    盘中输入传入最后已收盘的原始K线时间，未收盘见证只更新观察路径。
    检查点只用于撤销最后一个物理分型的暂时表示，不是历史修订接口。
    """

    def __init__(self, *, closed_through="all", close_times=None):
        self.nodes: list[StrokeEndpoint] = []
        self._path: tuple[int, ...] = ()
        self._witnesses = []
        self.closed_through = closed_through
        self.close_times = {} if close_times is None else close_times
        self.qualifications: tuple[StrokeQualification, ...] = ()
        self.completions: tuple[StrokeCompletion, ...] = ()
        self._continuations: tuple[StrokeContinuation, ...] = ()
        self.unresolved_equal_choices = ()
        self.blocked_at = None
        self.last_decision = None
        self.revisions: list[StrokeDecision] = []
        self.rejected_paths: list[StrokeDecision] = []
        self._checkpoint = None
        self._initial_opposite = None

    @property
    def selected(self):
        return self._path[-1] if self._path else -1

    def path(self, end=None):
        if end is None or end == self.selected:
            return list(self._path)
        result = []
        while end >= 0:
            result.append(end)
            end = self.nodes[end].parent
        return list(reversed(result))

    def components(self):
        return (self._path,) if self._path else ()

    def edges(self):
        return tuple(zip(self._path, self._path[1:]))

    @property
    def boundaries(self):
        # 不创造独立笔分量：不合格候选停留在物理分型和裁决记录里。
        return ()

    def observed_at(self, fractal_index):
        return self._witnesses[fractal_index]

    @property
    def continuations(self):
        return self._continuations

    def _is_closed(self, witness):
        return witness is not None and (
            self.closed_through == "all" or
            self.closed_through is not None and witness <= self.closed_through
        )

    def confirm_through(self, closed_through):
        self.closed_through = closed_through
        completed = list(self.completions)
        while len(completed) < len(self._path) - 3:
            i = len(completed)
            a, b, c, d = self._path[i:i + 4]
            witness = max(q.selected_at for q in self.qualifications[i:i + 3])
            if not self._is_closed(witness):
                break
            completed.append(StrokeCompletion(a, b, c, d, self.close_times.get(witness, witness), witness))
        self.completions = tuple(completed)

    def _preserves_completed(self, proposed):
        if not self.completions:
            return True
        protected = len(self.completions) + 1
        return proposed[:protected] == self._path[:protected]

    def _record_selection(self, previous, index, witness):
        selected = self.edges()
        stable = 0
        for old, new in zip(previous, selected):
            if old != new:
                break
            stable += 1
        qualifications = list(self.qualifications[:stable])
        for a, b in selected[stable:]:
            qualifications.append(StrokeQualification(a, b, self._witnesses[b], witness, index))
        self.qualifications = tuple(qualifications)
        # A continuation uses two adjacent qualified edges. Both edges remain
        # identical before stable - 1, so their existing records can be kept
        # verbatim. Rebuild only the boundary and changed suffix; edge pairs
        # are unique within a selected endpoint path.
        prefix = max(0, stable - 1)
        previous_continuations = self._continuations
        changed_continuations = previous_continuations[prefix:]
        prior = {(c.start, c.end, c.following_end): c for c in changed_continuations}
        prior_edges = {(c.start, c.end): c for c in changed_continuations}
        completed_edges = {(c.start, c.end) for c in self.completions}
        continuations = list(previous_continuations[:prefix])
        for i in range(prefix, max(0, len(selected) - 1)):
            current, following = self.qualifications[i:i + 2]
            key = (current.start, current.end, following.end)
            # 完成事件已保存当时的反向后继；尾部重选不能改写该笔的
            # 历史承接时间，否则相同完成证据会错误地使下游缓存失效。
            retained = (prior_edges.get(key[:2]) if key[:2] in completed_edges else prior.get(key))
            continuations.append(retained or StrokeContinuation(
                *key, max(current.selected_at, following.selected_at),
            ))
        self._continuations = tuple(continuations)
        self.confirm_through(self.closed_through)
        return stable

    def rewind_last(self):
        if self._checkpoint is None:
            raise ValueError("no stroke resolver checkpoint")
        (self._path, self.qualifications, self.completions, self._continuations,
         self.blocked_at, self.last_decision, revisions, rejected, self._initial_opposite) = self._checkpoint
        del self.revisions[revisions:]
        del self.rejected_paths[rejected:]
        self.nodes.pop()
        self._witnesses.pop()
        self._checkpoint = None

    def advance(self, fx, valid, witness):
        if witness is None:
            raise ValueError("physical fractal requires visibility evidence")
        if self.nodes and fx.k.index <= self.nodes[-1].fx.k.index:
            raise ValueError("fractal centers must advance in time")
        self._checkpoint = (
            self._path, self.qualifications, self.completions, self._continuations,
            self.blocked_at, self.last_decision, len(self.revisions), len(self.rejected_paths), self._initial_opposite,
        )
        index, previous = len(self.nodes), self.selected
        old = self._path
        proposed, action = old, "observe_unqualified_opposite"
        if not old:
            proposed, action = (index,), "initialize"
        else:
            tail = self.nodes[old[-1]].fx
            if len(old) == 1:
                # 尚未有第一笔：短间隔的更极端反向分型先保留，不能让
                # 后来较弱、刚好够距离的同类分型把它覆盖（L077 253–283）。
                pending = self._initial_opposite
                if fx.type != tail.type:
                    if pending is None or is_strictly_more_extreme(fx, self.nodes[pending].fx):
                        pending = self._initial_opposite = index
                    if pending == index and valid(tail, fx):
                        proposed, action = (*old, index), "append"
                elif pending is not None and valid(self.nodes[pending].fx, fx):
                    proposed, action = (pending, index), "initialize_qualified_pair"
                elif is_strictly_more_extreme(fx, tail):
                    proposed, action = (index,), "replace_same_type"
                    self._initial_opposite = None
                else:
                    action = "retain_earlier_same_type"
            elif fx.type == tail.type:
                action = "retain_earlier_same_type"
                if is_strictly_more_extreme(fx, tail) and (
                    valid(self.nodes[old[-2]].fx, fx)
                ):
                    proposed, action = (*old[:-1], index), "replace_same_type"
                    # L069 28–49 的初始四分型关系：首笔尚未完成，A—B
                    # 太近，B比P更极端、C比A更极端，B—C合格，改取B—C。
                    # 只处理最初两个入选端点，不套用到完成前缀或一般历史恢复。
                    if len(old) == 2:
                        p, a = (self.nodes[i].fx for i in old)
                        for candidate in range(old[-1] + 1, index):
                            b = self.nodes[candidate].fx
                            if (b.k.index - a.k.index < 4 and
                                is_strictly_more_extreme(b, p) and valid(b, fx)):
                                proposed, action = (candidate, index), "reselect_initial"
                                break
            elif valid(tail, fx):
                proposed, action = (*old, index), "append"
            elif (len(old) >= 3 and fx.k.index - tail.k.index < 4
                  and is_strictly_more_extreme(fx, self.nodes[old[-2]].fx)
                  and valid(self.nodes[old[-3]].fx, fx)):
                proposed, action = (*old[:-2], index), "short_conflict_remove_two"
        # 所有检查在写入选中链之前完成；失败只增加观察事实。
        blocked = proposed != old and not self._preserves_completed(proposed)
        if blocked:
            proposed, action = old, "await_completed_boundary"
        previous_edges = self.edges()
        self.nodes.append(StrokeEndpoint(fx, proposed[-2] if len(proposed) > 1 and proposed[-1] == index else -1,
                                         proposed[0] if proposed else index))
        self._witnesses.append(witness)
        self._path = proposed
        revised_from = self._record_selection(previous_edges, index, witness) if proposed != old else None
        if blocked:
            self.blocked_at = self.blocked_at or witness
        elif proposed != old:
            self.blocked_at = None
        decision = StrokeDecision(index, action, previous, self.selected, revised_from, witness)
        self.last_decision = decision
        if blocked:
            self.rejected_paths.append(decision)
        if proposed != old and action not in ("initialize", "append"):
            self.revisions.append(decision)

    @classmethod
    def build_batch(cls, fxs, valid, witness, *, closed_through="all", close_times=None):
        result = cls(closed_through=closed_through, close_times=close_times)
        for fx in fxs:
            result.advance(fx, valid, witness(fx))
        return result
