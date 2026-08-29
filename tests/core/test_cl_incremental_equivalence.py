"""Incremental and cold-batch calculations share one strict authority."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.decision_support.trading_system.runtime_config import strict_cl_config


FREQUENCY = "1m"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _config(*, basis: str = "test-incremental-raw") -> dict[str, object]:
    return strict_cl_config(
        structure_price_quantum=Decimal("0.000001"),
        price_basis_revision=basis,
    )


def _generate_klines(count: int, seed: int) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    start = pd.Timestamp("2024-01-01 09:30:00")
    rows: list[dict[str, object]] = []
    price = 100.0
    previous_high, previous_low = price + 0.3, price - 0.3
    for index in range(count):
        price = max(
            price + rng.randn() * 0.6 + 0.4 * np.sin(index / 9.0),
            5.0,
        )
        high, low = price + 0.25, price - 0.25
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


def _line_signature(lines) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            line.start.k.k_index,
            line.end.k.k_index,
            line.type,
            bool(line.is_done()),
            bool(getattr(line, "forming", False)),
            line.locked_at,
        )
        for line in lines
    )


def _strict_signature(cd: CL) -> tuple[object, ...]:
    evidence = cd.get_strict_evidence()
    return (
        _line_signature(cd.get_bis()),
        _line_signature(cd.get_xds()),
        evidence.structure_revision,
        evidence.structure,
        evidence.stroke_center_observations,
        evidence.confirmed_points,
        evidence.approaching_points,
        evidence.divergences,
    )


@pytest.mark.parametrize("seed", (3, 11, 29, 101))
def test_incremental_strict_evidence_equals_cold_batch(seed: int) -> None:
    frame = _generate_klines(160, seed)
    incremental = CL("TST", FREQUENCY, _config(), market="a")
    prefixes = (*range(45, 161, 5), 160)
    for length in dict.fromkeys(prefixes):
        prefix = frame.iloc[:length].reset_index(drop=True)
        incremental.process_klines(prefix)
        batch = CL("TST", FREQUENCY, _config(), market="a")
        batch.process_klines(prefix)
        assert _strict_signature(incremental) == _strict_signature(batch), (
            f"strict incremental result forked at seed={seed}, length={length}"
        )


def test_deepcopied_strict_state_remains_incrementally_equivalent() -> None:
    frame = _generate_klines(180, 37)
    original = CL("TST", FREQUENCY, _config(), market="a")
    original.process_klines(frame.iloc[:120].reset_index(drop=True))
    incremental = copy.deepcopy(original)

    for length in range(125, 181, 5):
        prefix = frame.iloc[:length].reset_index(drop=True)
        incremental.process_klines(prefix)
        batch = CL("TST", FREQUENCY, _config(), market="a")
        batch.process_klines(prefix)
        assert _strict_signature(incremental) == _strict_signature(batch)


def test_production_incremental_path_does_not_repeat_full_fractal_scans() -> None:
    """连续追加普通 K 线时，完整分型扫描只允许发生在首次冷启动。"""

    count = 120
    index = np.arange(count, dtype=float)
    center = 100.0 + np.sin(index / 3.0) * 4.0 + index * 0.01
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-05 09:30:00", periods=count, freq="min"),
            "open": center,
            "high": center + 0.25,
            "low": center - 0.25,
            "close": center,
            "volume": np.full(count, 1000.0),
        }
    )
    incremental = CL("TST", FREQUENCY, _config(), market="a")
    original_collect = incremental.bi_calculator._collect_fxs
    full_scan_lengths: list[int] = []

    def counted_collect(values):
        full_scan_lengths.append(len(values))
        return original_collect(values)

    incremental.bi_calculator._collect_fxs = counted_collect
    incremental.process_klines(frame.iloc[:40].reset_index(drop=True))
    for length in range(41, count + 1):
        incremental.process_klines(frame.iloc[:length].reset_index(drop=True))

    batch = CL("TST", FREQUENCY, _config(), market="a")
    batch.process_klines(frame)

    assert full_scan_lengths == [40]
    assert _strict_signature(incremental) == _strict_signature(batch)


def test_validated_incremental_htf_fast_path_equals_normal_and_cold_batch() -> None:
    """运行时认证过前缀后，快速入口仍必须与唯一生产结果逐项一致。"""

    frame = _generate_klines(190, 73)
    normal = CL("TST", FREQUENCY, _config(), market="a")
    validated = CL("TST", FREQUENCY, _config(), market="a")
    initial = frame.iloc[:150].reset_index(drop=True)
    normal.process_klines(initial)
    validated.process_klines(initial)

    for length in (151, 157, 170, 190):
        prefix = frame.iloc[:length].reset_index(drop=True)
        normal.process_klines(prefix)
        validated.process_validated_incremental_klines(prefix)
        cold = CL("TST", FREQUENCY, _config(), market="a")
        cold.process_klines(prefix)
        assert _strict_signature(validated) == _strict_signature(normal)
        assert _strict_signature(validated) == _strict_signature(cold)


def test_real_data_strict_evidence_equals_cold_batch() -> None:
    path = FIXTURES / "SH.600519_5m.parquet"
    if not path.exists():
        pytest.skip("real market fixture is unavailable")
    frame = pd.read_parquet(path)[
        ["date", "open", "high", "low", "close", "volume"]
    ].reset_index(drop=True)
    upper = min(500, len(frame))
    lower = min(420, upper)
    incremental = CL(
        "SH.600519",
        "5m",
        _config(basis="sh600519-raw"),
        market="a",
    )
    for length in range(lower, upper + 1, 10):
        prefix = frame.iloc[:length].reset_index(drop=True)
        incremental.process_klines(prefix)
        batch = CL(
            "SH.600519",
            "5m",
            _config(basis="sh600519-raw"),
            market="a",
        )
        batch.process_klines(prefix)
        assert _strict_signature(incremental) == _strict_signature(batch)


def test_us_causal_htf_bucket_alignment_differs_from_a_share_alignment() -> None:
    path = FIXTURES / "QQQ.US_30m.parquet"
    if not path.exists():
        pytest.skip("US market fixture is unavailable")
    frame = pd.read_parquet(path)[
        ["date", "open", "high", "low", "close", "volume"]
    ].head(600).reset_index(drop=True)

    us = CL("QQQ.US", "30m", _config(basis="qqq-raw"), market="us")
    us.process_klines(frame)
    a_share = CL("QQQ.US", "30m", _config(basis="qqq-raw"), market="a")
    a_share.process_klines(frame)

    us_hist = np.asarray(us._strict_htf_macd_by_level[0]["hist"])
    a_hist = np.asarray(a_share._strict_htf_macd_by_level[0]["hist"])
    assert (np.abs(us_hist - a_hist) > 1e-9).any()
