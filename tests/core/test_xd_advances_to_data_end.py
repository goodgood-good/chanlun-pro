"""线段必须一直推进到数据末端，不得在历史中途永久停摆。

`_build_segments` 主循环在「起始三笔无重叠且已有线段」时直接 break。该条件在实时
边缘是对的（反向段确实还没形成），但它被无差别用在历史笔上：一旦某根历史笔处不
满足三笔重叠，其后全部笔——哪怕还有数万根——都不再参与线段划分，而是被
`_emit_pending` 打包成一条巨型未完成线段。线段中枢、走势类型、递归层级与买卖点
随之全部截断在该点。

失效形态是「线段数冻结」而非「末线段落后」：那条巨型未完成尾段的终点仍然贴近数据
末端，所以只看末线段时间会漏判。

实测（tests/fixtures/SZ.002299_1m.parquet，21,954 根 1 分钟）：
    前 25%  笔 636   线段 83
    前 50%  笔 1284  线段 166
    前 75%  笔 1922  线段 171   <- 开始冻结
    前100%  笔 2565  线段 171   <- 数据增 33% 而线段不增

另在 510300 的 1 分钟真实行情上，线段自 2020-03-10 10:50 起永久停摆：喂 600 /
1044 / 1891 个交易日都恒为 973 段。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "SZ.002299_1m.parquet"


def _segments_at(frame: pd.DataFrame, rows: int) -> tuple[int, int]:
    state = CL("SZ.002299", "1m", {}, market="a")
    state.process_klines(frame.iloc[:rows])
    return len(state.get_bis()), len(state.get_xds())


def _segment_signature(item) -> tuple[int, int, str, bool]:
    return (
        int(item.start_line.index),
        int(item.end_line.index),
        str(item.type),
        bool(item.done),
    )


@pytest.fixture(scope="module")
def minute_frame() -> pd.DataFrame:
    return pd.read_parquet(FIXTURE)


def test_segment_count_grows_with_more_history(minute_frame: pd.DataFrame) -> None:
    """笔在增长时线段数不得冻结。"""

    total = len(minute_frame)
    counts = []
    for fraction in (0.25, 0.5, 0.75, 1.0):
        strokes, segments = _segments_at(minute_frame, int(total * fraction))
        counts.append((fraction, strokes, segments))

    for (_, previous_strokes, previous_segments), (
        fraction,
        strokes,
        segments,
    ) in zip(counts, counts[1:]):
        assert strokes > previous_strokes, "前提失效：更长前缀必须产生更多笔"
        assert segments > previous_segments, (
            f"线段在历史中途停摆：前缀 {fraction:.0%} 时笔 "
            f"{previous_strokes}->{strokes} 增长，线段却停在 "
            f"{previous_segments}->{segments}"
        )


def test_no_single_segment_swallows_the_tail(minute_frame: pd.DataFrame) -> None:
    """不得出现一条吞掉大段历史的巨型未完成线段。

    break 之后 `_emit_pending` 会把余下全部笔打成一条尾段，使末线段终点看似正常，
    实则整段历史未被划分。用「末段所含笔数不得远超其余线段中位数」来钉死。
    """

    state = CL("SZ.002299", "1m", {}, market="a")
    state.process_klines(minute_frame)
    segments = state.get_xds()
    assert len(segments) >= 3

    spans = [
        int(item.end_line.index) - int(item.start_line.index) + 1
        for item in segments
    ]
    tail = spans[-1]
    body = sorted(spans[:-1])
    median = body[len(body) // 2]
    assert tail <= median * 10, (
        f"末线段吞掉了 {tail} 根笔，而其余线段中位数仅 {median} 根，"
        "说明主循环提前终止后把剩余历史打包成了一条尾段"
    )


def test_segments_remain_contiguous_alternating_and_valid(
    minute_frame: pd.DataFrame,
) -> None:
    """修复不得通过跳笔、同向拼接或无重叠三笔伪造线段增长。"""

    state = CL("SZ.002299", "1m", {}, market="a")
    state.process_klines(minute_frame)
    strokes = state.get_bis()
    segments = state.get_xds()

    for index, item in enumerate(segments):
        start = int(item.start_line.index)
        end = int(item.end_line.index)
        assert end - start + 1 >= 3
        first, third = strokes[start], strokes[start + 2]
        assert max(float(first.low), float(third.low)) <= min(
            float(first.high),
            float(third.high),
        )
        if index == 0:
            continue
        previous = segments[index - 1]
        assert start == int(previous.end_line.index) + 1
        assert item.type != previous.type


def test_locked_segment_prefix_is_unchanged_by_future_bars(
    minute_frame: pd.DataFrame,
) -> None:
    """追加未来 K 线不得改写短前缀中已锁定的线段。"""

    signatures = []
    total = len(minute_frame)
    for fraction in (0.25, 0.5, 0.75, 1.0):
        state = CL("SZ.002299", "1m", {}, market="a")
        state.process_klines(minute_frame.iloc[: int(total * fraction)])
        signatures.append(
            tuple(_segment_signature(item) for item in state.get_xds())
        )

    for shorter, longer in zip(signatures, signatures[1:]):
        locked = tuple(item for item in shorter if item[3])
        assert longer[: len(locked)] == locked
