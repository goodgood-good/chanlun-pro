"""从严格证据快照解析当前确认批次。"""

from __future__ import annotations

from dataclasses import dataclass

from chanlun.core.strict_structure.models import (
    DivergenceEvidence,
    StrictEvidenceResult,
    StrictPointEvidence,
)


@dataclass(frozen=True, slots=True)
class CurrentStrictEvents:
    """同一快照中仍属于最新结构确认批次的正式事件。"""

    points: tuple[StrictPointEvidence, ...]
    divergences: tuple[DivergenceEvidence, ...]


def current_strict_events(
    evidence: StrictEvidenceResult,
) -> CurrentStrictEvents:
    """返回每个递归级别最新锁定前沿所确认的点与背驰。

    买卖点的形态锚点不一定是确认时的最后一个锁定单元。例如，一类点可以先
    锚定在趋势极值，随后由紧邻反向单元完成因果确认。若要求 ``anchor_unit_id``
    等于末端单元，这类合法点会在首次可见时就被永久漏掉。

    当前批次因此以证据的 ``available_at`` 与同级最新锁定前沿比较，而不篡改
    形态锚点。只要同级尚未产生更新的锁定前沿，本批证据仍为当前；新的锁定
    前沿出现后，旧批次自然退出当前选股，但仍完整保留在历史证据账本中。
    """

    frontier_by_level = {}
    for level in evidence.structure.levels:
        locked = tuple(unit for unit in level.units if unit.locked)
        if locked:
            frontier_by_level[level.structural_level] = max(
                unit.available_at for unit in locked
            )

    points = tuple(
        point
        for point in evidence.confirmed_points
        if (frontier := frontier_by_level.get(point.structural_level)) is not None
        and point.available_at >= frontier
    )
    referenced_divergence_ids = {
        point.divergence.divergence_id
        for point in points
        if point.divergence is not None
    }
    divergences = tuple(
        divergence
        for divergence in evidence.divergences
        if divergence.divergence_id in referenced_divergence_ids
        or (
            (frontier := frontier_by_level.get(divergence.structural_level)) is not None
            and divergence.available_at >= frontier
        )
    )
    return CurrentStrictEvents(points=points, divergences=divergences)


__all__ = ("CurrentStrictEvents", "current_strict_events")
