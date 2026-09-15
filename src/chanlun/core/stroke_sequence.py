"""按原文的相邻顶底顺序建立关系，不选择整张图的完成状态。

此文件保留v13的全区间极值研究模型；当前生产由stroke_resolver执行
用户路径乙，不调用这里的图搜索，也不把这里的旧路径当作新规则的预期。

来源均为 D:/缠论/chanlun_lesson_corpus：
L062 正文 64–82 行：笔连接相邻顶底，不能隔过其他顶底。
L065 正文 166–214 行：中间已有顶底时，不能把多笔当成一笔。
L077 正文 169–229、247–283 行：同类取舍、相邻连接与 N/N+1。

完整的反向分解是排除长连接的充分证据；尚不能接到相同终点的反向
候选单独保存，不能把候选直接当作全图已经保留的分型。以下是对
上述段落的程序表达，不是作者给出的 Python 算法或永久完成定理。
所有分型保留在顺序表中；价格前沿只用来排除以后不可能再满足
区间极值的起点，不表示从原始行情或历史划分中删除分型。
"""

from bisect import bisect_left
from dataclasses import dataclass, replace
from typing import Callable

from chanlun.core.stroke_rules import is_strictly_more_extreme
from chanlun.core.types import FX


@dataclass(frozen=True)
class FractalRelation:
    """以一个分型结束的局部关系及相邻性排除证据，索引指向 points。"""

    endpoint: int
    eligible: tuple[int, ...]
    adjacent: tuple[int, ...]
    excluded: tuple[tuple[int, int], ...]
    open_reversals: tuple[tuple[int, int, int], ...]


class SourceStrokeSequence:
    """分型 → 局部关系 → 相邻关系；不依赖已输出的笔或锁定字段。"""

    def __init__(self):
        self.points: list[FX] = []
        self.relations: list[FractalRelation] = []
        self.frontiers: dict[str, list[int]] = {"ding": [], "di": []}
        self.turns: dict[int, tuple[int, int, int]] = {}
        self.ancestors: list[int] = []
        self._checkpoint = None

    def append(self, fx: FX, locally_valid: Callable[[FX, FX], bool]) -> FractalRelation:
        if self.points and fx.k.index <= self.points[-1].k.index:
            raise ValueError("source stroke sequence requires increasing fractal centers")
        index = len(self.points)
        opposite = "di" if fx.type == "ding" else "ding"
        eligible = tuple(old for old in self.frontiers[opposite]
                         if locally_valid(self.points[old], fx))
        reducible = 0
        reachable = 0
        for later_start in eligible:
            reducible |= self.ancestors[later_start]
            reachable |= (1 << later_start) | self.ancestors[later_start]
        adjacent = tuple(old for old in eligible if not (reducible >> old) & 1)
        excluded = tuple((old, later) for old in eligible if (reducible >> old) & 1
                         for later in eligible if (self.ancestors[later] >> old) & 1)
        open_reversals = tuple(self.turns[old] for old in adjacent if old in self.turns)
        relation = FractalRelation(index, eligible, adjacent, excluded, open_reversals)
        changed_turns = []
        self._checkpoint = (
            {kind: list(values) for kind, values in self.frontiers.items()}, changed_turns,
        )
        # A→B→C 只是一个反向候选。没有 C→D 的完整分解时，它还
        # 不能单独否定 A→D；保留证据供后面的同类取舍和整图检查使用。
        for middle in adjacent:
            for first in self.relations[middle].adjacent:
                if first not in self.turns:
                    self.turns[first] = (first, middle, index)
                    changed_turns.append(first)
        frontier = self.frontiers[fx.type]
        while frontier and is_strictly_more_extreme(fx, self.points[frontier[-1]]):
            frontier.pop()
        frontier.append(index)
        self.points.append(fx)
        self.relations.append(relation)
        self.ancestors.append(reachable)
        return relation

    def rewind_last(self):
        if self._checkpoint is None:
            raise ValueError("no source stroke sequence checkpoint")
        self.frontiers, changed_turns = self._checkpoint
        for first in changed_turns:
            del self.turns[first]
        self.points.pop()
        self.relations.pop()
        self.ancestors.pop()
        self._checkpoint = None

    def live_starts(self):
        return set(self.frontiers["ding"]) | set(self.frontiers["di"])

    def has_live_path_from(self, anchor):
        """固定历史与该起点时，必要价格关系图里是否还存在可延续节点。

        False 意味着单纯追加无法恢复该前缀；True 仅表示候选尚在，
        不保证候选经过整条取舍检查后一定能接通。
        """
        return any(point == anchor or (self.ancestors[point] >> anchor) & 1
                   for point in self.live_starts())

    def decomposition(self, first: int, last: int):
        """按需展开排除长连接的具体分型序列，便于独立复核。"""
        via = next((later for earlier, later in self.relations[last].excluded
                    if earlier == first), None)
        if via is None:
            return ()
        paths = {first: (first,)}
        for endpoint in range(first + 1, via + 1):
            predecessor = next((old for old in self.relations[endpoint].eligible
                                if old in paths), None)
            if predecessor is not None:
                paths[endpoint] = (*paths[predecessor], endpoint)
        if via not in paths:
            raise AssertionError("missing source relation decomposition")
        return (*paths[via], last)

    @staticmethod
    def retained_reversal(parent: int, retained: list[int]):
        """从已保留序列查找实际反向结构，不从候选关系冒认已选端点。

        L065 166–214、L077 247–283：仅出现过近的反向分型时，现有
        合格反向结构不能直接消失；后来的同类取舍仍须另外判断。
        """
        for first, middle, last in zip(retained, retained[1:], retained[2:]):
            if first == parent:
                return first, middle, last
        return None


@dataclass(frozen=True)
class SelectedFractal:
    fx: FX
    parent: int = -1
    origin: int = -1


@dataclass(frozen=True)
class OmittedPredecessor:
    """候选使用了当前保留笔内部已舍弃的分型，记录舍弃依据。"""

    candidate: int
    discarded: int
    stroke_start: int
    stroke_end: int


@dataclass(frozen=True)
class RetainedPathConflict:
    """候选前驱路径绕过已保留转折；局部几何资格仍在关系表中。"""

    candidate: int
    required: tuple[int, ...]
    proposed: tuple[int, ...]


@dataclass(frozen=True)
class FractalSelection:
    endpoint: int
    previous_endpoint: int
    selected_endpoint: int
    action: str
    changed: bool
    omitted: tuple[OmittedPredecessor, ...] = ()
    conflicts: tuple[RetainedPathConflict, ...] = ()


@dataclass(frozen=True)
class EqualSelectionDependency:
    """当前连接点之前仍存在同价候选；其取舍尚依赖后续条件。"""

    start: int
    earlier: int
    chosen: int
    following: int


@dataclass(frozen=True)
class StrokeBoundary:
    """尚未证明如何连接的两个观察范围，不是一条笔。

    L062 64–82、L066 回复 277–280 的条件不能为拼接而放宽。
    L069 151–184 允许取舍未决；保留范围是对未决事实的工程表达。
    """

    left: int
    right: int
    observed_by: int
    reason: str = "unresolved_connection"


class SourceStrokeSelection:
    """根据分型关系执行同类取舍，不读取 BI、完成字段或时间缓存。

    原文依据为 L069 28–49、151–184 与 L077 247–283。起点编号用于
    表达暂时取舍之间的依赖；等待可接续关系的策略属于实现推导，
    不能把这个编号或等待状态本身当成完成证明。
    """

    def __init__(self):
        self.nodes: list[SelectedFractal] = []
        self._path_bits: list[int] = []
        self.selected = -1
        self.pending_continuation = False
        self.retained_components: tuple[tuple[int, ...], ...] = ()
        self.boundaries: tuple[StrokeBoundary, ...] = ()
        self._checkpoint = None

    def path(self, endpoint=None):
        current = self.selected if endpoint is None else endpoint
        result = []
        while current >= 0:
            result.append(current)
            current = self.nodes[current].parent
        return list(reversed(result))

    def components(self):
        """各范围内部相连；范围之间不能 zip 成一条虚构的笔。"""
        active = tuple(self.path())
        return self.retained_components + ((active,) if active else ())

    def unresolved_equal_choices(self, sequence, retained=None):
        """L077 253–283 的相等分型取舍依赖，不是通用完成判据。

        较早等价候选仍保留未来连接的价格资格时，仅仅从较晚候选接
        出下一笔，不足以把该连接点的取舍视为已经解决。
        """
        retained = self.path() if retained is None else list(retained)
        live = sequence.live_starts()
        if len(retained) < 3:
            return ()
        # 只检查最后一个仍允许前移的连接点，与下面的恢复条件一致。
        # 若其后已有合格反向结构，再恢复更早候选就会绕过应保留的
        # 转折（L062 64–82、L065 166–214），不能持续冻结此前的证据。
        start, chosen, following = retained[-3:]
        fx = sequence.points[chosen]
        dependencies = []
        for earlier in range(start + 1, chosen):
            other = sequence.points[earlier]
            if (earlier in live and other.type == fx.type and other.val == fx.val
                    and self.path(earlier)[:-1] == retained[:-2]):
                dependencies.append(EqualSelectionDependency(start, earlier, chosen, following))
        return tuple(dependencies)

    def _retained_predecessors(self, candidates, retained, *, endpoint=None, sequence=None):
        """先按当前取舍排除已 X 的分型，再谈剩余分型的先后顺序。

        L077 271–283 的“最先一个”针对经过取舍的分型；同一笔内部的
        分型不能从历史候选表直接返回，绕过已保留的相邻连接。这里的
        X 随支持它的连接而定，不是删除原始分型或给旧笔永久上锁。
        候选自身及其前驱均须检查，防止经由另一个候选恢复已 X 的点。
        """
        if not retained:
            return tuple(candidates), ()
        interior = (1 << (retained[-1] + 1)) - (1 << retained[0])
        for point in retained:
            interior &= ~(1 << point)
        accepted, omitted = [], []
        for candidate in candidates:
            discarded = self._path_bits[candidate] & interior
            # L077 253–283 的工程推导：最后一笔延伸时，较早等价的
            # 连接点若重新具备资格，不能仅因旧的临时连接而持续排除。
            # 只允许把最后一个同价连接点前移；不恢复它之后已经 X 的
            # 内部分型，不吞掉此前保留的反向结构。局部资格由 relation
            # 提供，这个例外本身不构成全图不可修改的完成证明。
            if discarded and self._equal_junction_candidate(candidate, retained, endpoint, sequence):
                discarded &= ~(1 << candidate)
            if not discarded:
                accepted.append(candidate)
                continue
            point = (discarded & -discarded).bit_length() - 1
            position = bisect_left(retained, point)
            omitted.append(OmittedPredecessor(
                candidate, point, retained[position - 1], retained[position],
            ))
        return tuple(accepted), tuple(omitted)

    def _equal_junction_candidate(self, candidate, retained, endpoint, sequence):
        if endpoint is None or len(retained) < 3:
            return False
        junction, tail = retained[-2:]
        new_fx = sequence.points[endpoint]
        old_fx, chosen_fx = sequence.points[candidate], sequence.points[junction]
        return (
            retained[-3] < candidate < junction
            and self.path(candidate)[:-1] == list(retained[:-2])
            and old_fx.type == chosen_fx.type and old_fx.val == chosen_fx.val
            and new_fx.type == sequence.points[tail].type
            and is_strictly_more_extreme(new_fx, sequence.points[tail])
        )

    def _compatible_predecessors(self, relation, retained, sequence):
        """活动范围和历史恢复共用的完整路径检查。

        原文相邻取舍的实现约束（L065 166–214、L077 247–283）：
        延伸末笔须保留此前转折；同价末连接点的条件重选另有证据。
        被拒绝的局部边留在 sequence.relations，不写入选择节点的
        父链；否则其后代会绕过这里的判断。这不是永久完成定理。
        """
        normalized, omitted = self._retained_predecessors(
            relation.adjacent, retained, endpoint=relation.endpoint, sequence=sequence,
        )
        if not retained:
            return normalized, omitted, ()
        origin = self.nodes[retained[-1]].origin
        required = tuple(retained[:-1])
        accepted, conflicts = [], []
        for candidate in normalized:
            proposed = tuple(self.path(candidate))
            if (self.nodes[candidate].origin != origin
                    or proposed[:len(required)] == required
                    or self._equal_junction_candidate(candidate, retained, relation.endpoint, sequence)):
                accepted.append(candidate)
            else:
                conflicts.append(RetainedPathConflict(candidate, required, proposed))
        return tuple(accepted), omitted, tuple(conflicts)

    def _source_initial_reselection(self, retained, parent, endpoint, sequence):
        """L069 正文 28–49 的四分型关系，不能推广为整条历史清空。

        P→A 为当前唯一连接，A→B 太近，B 比 P 更极端、C 比 A 更
        极端且 B→C 有资格时，用 B→C 重选初始观察。后面的合格
        反向结构已经存在时，不再把这个初始图例套到整个历史前缀。
        """
        candidate = self.path(parent)
        if len(retained) != 2 or len(candidate) != 1:
            return False
        p, a = retained
        b, c = parent, endpoint
        if not p < a < b < c:
            return False
        points = sequence.points
        return (
            points[b].k.index - points[a].k.index < 4
            and is_strictly_more_extreme(points[b], points[p])
            and is_strictly_more_extreme(points[c], points[a])
        )

    def _resumable_component(self, relation, sequence):
        """后来的合格连接可以解决先前的待接续范围。

        L077 247–283 的相邻取舍仍须对早先保留的范围核验。后段尚未
        接通的候选不能反过来把旧末端永久冻结；但也不能绕过早先已
        保留的反向结构。relation 已排除具有完整反向分解的长连接。
        """
        fx = sequence.points[relation.endpoint]
        for component, path in enumerate(self.retained_components):
            normalized, omitted, conflicts = self._compatible_predecessors(relation, path, sequence)
            origin = self.nodes[path[-1]].origin
            connected = [i for i in normalized if self.nodes[i].origin == origin]
            if not connected:
                continue
            parent = min(connected)
            old = self.nodes[path[-1]]
            if old.fx.type == fx.type and not is_strictly_more_extreme(fx, old.fx):
                continue
            return component, parent, omitted, conflicts
        return None

    def append(self, relation: FractalRelation, sequence: SourceStrokeSequence):
        index, previous = relation.endpoint, self.selected
        if index != len(self.nodes):
            raise ValueError("fractal selection requires the next source relation")
        self._checkpoint = (
            self.selected, self.pending_continuation,
            self.retained_components, self.boundaries,
        )
        fx = sequence.points[index]
        retained = self.path()
        origin = self.nodes[previous].origin if previous >= 0 else -1
        normalized, omitted, conflicts = self._compatible_predecessors(relation, retained, sequence)
        connected = tuple(old for old in normalized if self.nodes[old].origin == origin)
        candidates = connected or normalized
        parent = min(candidates) if candidates else -1
        resumed = self._resumable_component(relation, sequence)
        if resumed is not None:
            component, parent, omitted, conflicts = resumed
        viable = sequence.live_starts()
        self.nodes.append(SelectedFractal(
            fx, parent, self.nodes[parent].origin if parent >= 0 else index,
        ))
        self._path_bits.append((1 << index) | (self._path_bits[parent] if parent >= 0 else 0))
        action = "retain_qualified_reversal" if conflicts else "await_qualified_opposite"
        changed = False
        if resumed is not None:
            # 先前候选的选择事件与节点仍可回查，不把待接续候选当成
            # 永久完成的笔；当前输出恢复为有直接连接证据的连续范围。
            self.retained_components = self.retained_components[:component]
            self.boundaries = self.boundaries[:component]
            self.selected = index
            self.pending_continuation = False
            action, changed = "resolve_pending_connection", True
        elif parent >= 0:
            old = self.nodes[previous] if previous >= 0 else None
            candidate_path = self.path(index)
            if retained and not connected:
                if self._source_initial_reselection(retained, parent, index, sequence):
                    self.selected = index
                    self.pending_continuation = False
                    action, changed = "reselect_initial", True
                elif candidate_path[0] > retained[-1]:
                    # 局部资格已经成立而前后接法未决：保存两个范围，
                    # 不用任何历史极值冻结后段，也不抛弃整个旧前缀。
                    self.retained_components += (tuple(retained),)
                    self.boundaries += (StrokeBoundary(retained[-1], candidate_path[0], index),)
                    self.selected = index
                    action, changed = "start_pending_component", True
                else:
                    # 跨越另一个暂定范围的候选仍在 relation 中保留。
                    # 不把它直接提升为可以吞掉已保留转折的长笔。
                    action = "await_selection_evidence"
                    self.pending_continuation = True
            elif (old is not None and self.nodes[parent].origin == origin
                  and old.fx.type == fx.type and not is_strictly_more_extreme(fx, old.fx)):
                action = "retain_earlier_same_type"
                if old.parent != parent:
                    self.pending_continuation = True
            else:
                self.selected = index
                self.pending_continuation = False
                action = "append" if parent == previous else "reselect"
                changed = True
        if changed and self.boundaries and self.boundaries[-1].right != self.path()[0]:
            self.boundaries = (*self.boundaries[:-1], replace(
                self.boundaries[-1], right=self.path()[0], observed_by=index,
            ))
        if self.boundaries or (self.selected >= 0 and self.selected not in viable):
            self.pending_continuation = True
        return FractalSelection(index, previous, self.selected, action, changed, omitted, conflicts)

    def rewind_last(self):
        if self._checkpoint is None:
            raise ValueError("no source fractal selection checkpoint")
        (self.selected, self.pending_continuation,
         self.retained_components, self.boundaries) = self._checkpoint
        self.nodes.pop()
        self._path_bits.pop()
        self._checkpoint = None
