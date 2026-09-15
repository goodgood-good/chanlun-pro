"""在未接通的笔范围内继续计算条件观察，禁止混入全图确认信号。

L069 正文 151–184 区分临时取舍与完成图形。本模块是据此设计的
工程隔离层，不把局部计算成功视为原文的全图完成证明。
来源：D:/缠论/chanlun_lesson_corpus/L069_月线分段与上海大走势分析、预判(2007-08-09.md。
"""

from copy import copy
from dataclasses import dataclass
from datetime import timezone

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from chanlun.core.types import BI, XD
from chanlun.core.xd_calculator import XdCalculator


@dataclass(frozen=True)
class ConditionalStrokeObservation:
    """只含待定线段和范围元数据；没有正式买卖点接口。"""

    scope_id: str
    component_index: int
    source_strokes: tuple[BI, ...]
    segments: tuple[XD, ...]


class StrokeComponentObservations:
    """复用未变化的范围；前情解决或盘中回滚时撤销旧范围的投影。"""

    def __init__(self):
        self._scopes = {}
        self.observations: tuple[ConditionalStrokeObservation, ...] = ()

    def calculate(self, components):
        scopes, observations = {}, []
        for component_index, source in enumerate(components):
            if component_index == 0 or not source:
                continue
            first = source[0]

            def canonical_time(value):
                return (
                    value.astimezone(timezone.utc)
                    if value is not None and value.utcoffset() is not None
                    else value
                )

            key = (
                canonical_time(first.start.k.date),
                first.start.val,
                first.type,
                canonical_time(first.selected_at),
            )
            previous = self._scopes.get(key)
            if (
                previous is not None
                and previous[0].component_index == component_index
                and len(source) == len(previous[0].source_strokes)
                and all(a is b for a, b in zip(source, previous[0].source_strokes))
            ):
                scopes[key] = previous
                observations.append(previous[0])
                continue
            # 只在私有投影内使用本范围的后继证据。归一化序号是必要的：
            # XdCalculator 的序号指向当前输入列表，不能沿用全图偏移。
            projected = []
            for index, bi in enumerate(source):
                local = copy(bi)
                local.index = index
                local.locked_at = bi.continuation_at
                local.forming = local.locked_at is None
                local.selection_pending = False
                projected.append(local)
            calculator = XdCalculator()
            local_segments = tuple(calculator.calculate(projected))
            scope_id = stable_structure_id("conditional-stroke-scope-v1", *key)
            visible = []
            for segment in local_segments:
                item = copy(segment)
                # 对外只给条件几何；局部确认时间不会冒充全图 locked_at。
                item.scope_confirmed_at = segment.locked_at
                item.locked_at = None
                item.done = False
                item.selection_pending = True
                item.component_index = component_index
                item.observation_scope_id = scope_id
                item.start_line = source[segment.start_line.index]
                item.end_line = source[segment.end_line.index]
                visible.append(item)
            observation = ConditionalStrokeObservation(
                scope_id, component_index, tuple(source), tuple(visible)
            )
            scopes[key] = (observation, tuple(projected), local_segments)
            observations.append(observation)
        self._scopes = scopes
        self.observations = tuple(observations)
        return self.observations

    def centers(self, *, price_quantum, as_of, price_basis_revision, market_scope):
        """结果必须随所属观察范围消费，不能拼接后送入递归/信号算法。"""
        results = []
        for observation, projected, local_segments in self._scopes.values():
            registry = UnitLockRegistry(
                price_basis_revision,
                scope=(*market_scope, observation.scope_id),
            )
            units = adapt_lines(
                local_segments,
                0,
                SourceKind.SEGMENT,
                price_quantum,
                as_of,
                registry,
                constituent_lines=projected,
            )
            result = calculate_centers(units, 0, SourceKind.SEGMENT)
            results.append((observation, result))
        return tuple(results)
