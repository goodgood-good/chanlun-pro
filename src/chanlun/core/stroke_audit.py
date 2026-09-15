"""笔端点区间极值的只读核验，不承担分型取舍与自动回退。

依据：D:/缠论/chanlun_lesson_corpus/L066_主力资金的食物链(2007-07-30224205).md
256–280 行，作者 2007-07-31 16:14:47 回复（不是课程正文）。
工程转写：在两端中心所在的合并 K 线闭区间比较最高、最低值；
不包含起点左肩和终点右肩，也不重新加入包含处理已消去的原始价格。
核验通过仅说明这一个必要条件成立，不证明分型相邻或图形完成。
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Sequence

from chanlun.core.stroke_ranges import StrokeRanges
from chanlun.core.stroke_rules import old_pair_valid
from chanlun.core.types import BI, CLKline, FX


@dataclass(frozen=True)
class BiRangeViolation:
    bi_index: int
    direction: str
    done: bool
    start_cl_index: int
    end_cl_index: int
    endpoint_high: float
    endpoint_low: float
    actual_high: float
    actual_low: float
    high_cl_index: int
    low_cl_index: int
    high_source_indices: tuple[int, ...]
    low_source_indices: tuple[int, ...]


@dataclass(frozen=True)
class BiAdjacencyViolation:
    bi_index: int
    done: bool
    start_cl_index: int
    end_cl_index: int
    subdivision_centers: tuple[int, ...]


def audit_bi_adjacency(
    bis: Sequence[BI], fxs: Sequence[FX], cl_klines: Sequence[CLKline],
) -> list[BiAdjacencyViolation]:
    """核验跨笔反例，不代替整个分型序列的取舍证明。

    L062 正文 64–70 行、L065 正文 166–214 行要求不能把应保留的上下
    笔直接跨成一笔。此处独立枚举每个输出跨度内的连接，找到从同一起点
    到同一终点、至少三条合格旧笔连接的证据，即报告该长连接可再分。
    不复用选择器的节点、前驱或位图，不在行情更新的热路径自动执行。
    """
    ranges = StrokeRanges()
    ranges.update(cl_klines)
    centers = [fx.k.index for fx in fxs]
    if any(right <= left for left, right in zip(centers, centers[1:])):
        raise ValueError("adjacency audit requires ordered fractal centers")
    violations = []
    for bi in bis:
        start, end = bi.start.k.index, bi.end.k.index
        if not 0 <= start < end < len(cl_klines):
            raise ValueError(f"BI {bi.index}: merged K-line interval is unavailable")
        inner = fxs[bisect_right(centers, start):bisect_left(centers, end)]
        points = [bi.start, *inner, bi.end]
        # 保存 0、1、2、至少 3 条连接的可达证据。多于三条也必须检查，
        # 不能假定所有错误跨笔都恰好可以分成三条。
        paths = [{0: (start,)}] + [{} for _ in points[1:]]
        for right in range(1, len(points)):
            for left in range(right):
                if not paths[left]:
                    continue
                first, second = points[left], points[right]
                if not old_pair_valid(first, second, ranges.query(first.k.index, second.k.index)):
                    continue
                for count, path in paths[left].items():
                    paths[right].setdefault(min(count + 1, 3), (*path, second.k.index))
        if 3 in paths[-1]:
            violations.append(BiAdjacencyViolation(
                bi.index, bool(bi.is_done()), start, end, paths[-1][3],
            ))
    return violations


def audit_bi_ranges(bis: Sequence[BI], cl_klines: Sequence[CLKline]) -> list[BiRangeViolation]:
    """返回端点价格与区间极值不符的笔，坐标均为从零开始。

    当前笔按时间相连，每根合并 K 线最多参与两个区间的核验，整体
    O(K + B)。该函数按需调用，不增加逐根行情更新时的全历史扫描。
    缺失或错位的区间证据直接报错，不能将空区间当成核验通过。
    """
    violations = []
    for bi in bis:
        start, end = bi.start.k.index, bi.end.k.index
        if not (0 <= start < end < len(cl_klines)):
            raise ValueError(f"BI {bi.index}: merged K-line interval is unavailable")
        span = cl_klines[start:end + 1]
        if any(k.index != start + offset for offset, k in enumerate(span)):
            raise ValueError(f"BI {bi.index}: merged K-line coordinates are inconsistent")
        high = max(span, key=lambda k: k.h)
        low = min(span, key=lambda k: k.l)
        if bi.high == high.h and bi.low == low.l:
            continue
        violations.append(BiRangeViolation(
            bi_index=bi.index,
            direction=bi.type,
            done=bool(bi.is_done()),
            start_cl_index=start,
            end_cl_index=end,
            endpoint_high=bi.high,
            endpoint_low=bi.low,
            actual_high=high.h,
            actual_low=low.l,
            high_cl_index=high.index,
            low_cl_index=low.index,
            # 合并 K 的 k_index 记录方向极值来源，不必同时对应 h 和 l。
            # 因此分别查找实际提供 h/l 的物理 K，不能共用一个 k_index。
            high_source_indices=tuple(k.index for k in high.klines if k.h == high.h),
            low_source_indices=tuple(k.index for k in low.klines if k.l == low.l),
        ))
    return violations
