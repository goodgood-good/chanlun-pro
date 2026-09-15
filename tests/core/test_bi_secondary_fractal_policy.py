"""真实行情的旧笔间隔与候选路径取舍回归。"""

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


def test_qqq_old_stroke_starts_from_the_previously_skipped_extreme():
    cd = CL("QQQ.US", "30m", strict_base_config(), market="us")
    cd.process_klines(_qqq_prefix())

    bottoms = {
        fx.k.index: fx.val for fx in cd.get_fxs() if fx.type == "di"
    }
    assert bottoms[11] == 591.101
    assert bottoms[14] == 597.153
    first = cd.get_bis()[0]
    assert first.end.k.index - first.start.k.index >= 4
    assert first.start.k.index == 11
    assert first.start.val == 591.101
    assert (8, 14) not in {(bi.start.k.index, bi.end.k.index) for bi in cd.get_bis()}
    assert cd.bi_calculator.audit_endpoint_ranges() == []


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
    assert incremental.bi_calculator.audit_endpoint_ranges() == batch.bi_calculator.audit_endpoint_ranges()
