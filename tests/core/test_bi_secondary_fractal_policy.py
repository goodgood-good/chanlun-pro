"""真实行情回归：严格笔允许距离合格的次高/次低分型成笔。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "QQQ.US_30m.parquet"


def _qqq_prefix() -> pd.DataFrame:
    return (
        pd.read_parquet(FIXTURE)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(60)
        .reset_index(drop=True)
    )


def _signature(cd: CL) -> list[tuple[int, int, str, bool]]:
    return [
        (bi.start.k.index, bi.end.k.index, bi.type, bi.is_done())
        for bi in cd.get_bis()
    ]


def test_qqq_30m_near_lower_bottom_does_not_block_later_secondary_bottom():
    cd = CL("QQQ.US", "30m", strict_base_config(), market="us")
    cd.process_klines(_qqq_prefix())

    bottoms = {
        fx.k.index: fx.val for fx in cd.get_fxs() if fx.type == "di"
    }
    assert bottoms[11] == 591.101
    assert bottoms[14] == 597.153
    assert _signature(cd)[:4] == [
        (8, 14, "down", True),
        (14, 28, "up", True),
        (28, 40, "down", True),
        (40, 44, "up", False),
    ]


def test_qqq_30m_secondary_fractal_policy_is_incrementally_stable():
    frame = _qqq_prefix()
    batch = CL("QQQ.US", "30m", strict_base_config(), market="us")
    batch.process_klines(frame)

    incremental = CL("QQQ.US", "30m", strict_base_config(), market="us")
    for row in frame.itertuples(index=False):
        incremental.process_kline_values(
            row.date,
            row.open,
            row.high,
            row.low,
            row.close,
            row.volume,
        )

    assert _signature(incremental) == _signature(batch)
