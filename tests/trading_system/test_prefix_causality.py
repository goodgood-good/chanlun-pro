import numpy as np
import pandas as pd

from chanlun.core.cl import CL
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
    point_signature,
)


def _deterministic_klines(bar_count: int) -> pd.DataFrame:
    rng = np.random.RandomState(7)
    start = pd.Timestamp("2024-01-01 09:30:00", tz="Asia/Shanghai")
    rows: list[dict[str, object]] = []
    price = 100.0
    previous_high = 100.3
    previous_low = 99.7
    for index in range(bar_count):
        price += rng.randn() * 0.6 + 0.4 * np.sin(index / 9.0)
        price = max(price, 5.0)
        high = price + 0.25
        low = price - 0.25
        if index % 11 == 10:
            high = max(high, previous_high) + 0.15
            low = min(low, previous_low) - 0.15
        rows.append(
            {
                "date": start + pd.Timedelta(minutes=index),
                "high": high,
                "low": low,
                "open": price,
                "close": price,
                "volume": 1000.0,
            }
        )
        previous_high, previous_low = high, low
    return pd.DataFrame(rows)


def deterministic_cl_state(*, bar_count: int) -> CL:
    cd = CL(
        "SZ.000001",
        "1m",
        {"macd_ld_use_htf": True, "recursive_zs_diversity": False},
    )
    cd.process_klines(_deterministic_klines(bar_count))
    return cd


def test_future_append_cannot_mutate_confirmed_prefix() -> None:
    prefix_cd = deterministic_cl_state(bar_count=180)
    full_cd = deterministic_cl_state(bar_count=220)
    cutoff = prefix_cd.get_src_klines()[-1].date

    prefix = extract_confirmed_points(
        prefix_cd,
        code="SZ.000001",
        source_frequency="1m",
        as_of=cutoff,
    )
    full = extract_confirmed_points(
        full_cd,
        code="SZ.000001",
        source_frequency="1m",
        as_of=full_cd.get_src_klines()[-1].date,
    )

    assert prefix
    assert point_signature(prefix) == point_signature(
        tuple(point for point in full if point.confirmed_at <= cutoff)
    )
