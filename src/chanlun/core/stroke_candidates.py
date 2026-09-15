"""仅保留旧缓存的类型名称，便于 CL 明确拒绝旧 schema 后重新计算。

v4 的路径评分和 LCA 锁定实现已删除。生产计算使用 StrokeResolver。
pickle 恢复对象时不调用 __init__；这些类型不能再用于新计算。
"""

from dataclasses import dataclass

from chanlun.core.stroke_ranges import StrokeRanges as StrokeRanges
from chanlun.core.types import FX


@dataclass(frozen=True)
class StrokePathNode:
    fx: FX
    parent: int = -1
    depth: int = 0
    jumps: tuple[int, ...] = ()


class StrokeCandidates:
    def __init__(self):
        raise RuntimeError("v4 stroke candidates are obsolete; use StrokeResolver")
