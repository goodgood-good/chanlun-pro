from pathlib import Path

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def load_fixture(name, rows):
    return (
        pd.read_parquet(FIXTURES / name)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(rows)
        .reset_index(drop=True)
    )


def strict_config():
    return {
        **strict_base_config(),
        "structure_price_quantum": "0.01",
        "price_basis_revision": "test-raw-v1",
        "skip_legacy_zslx": True,
        "skip_legacy_mmd": True,
    }


def strict_fingerprint(result):
    return tuple(
        (
            level.structural_level,
            tuple(
                (
                    center.center_id,
                    center.state,
                    tuple(item.unit_id for item in center.initial_units),
                    center.zd_tick,
                    center.zg_tick,
                    center.body_revision,
                    center.completion_leave_unit.unit_id,
                    center.completion_return_unit.unit_id,
                    center.completed_at,
                )
                for center in level.center_result.centers
                if center.completed_at is not None
            ),
            tuple(
                trend
                for trend in level.trend_types
                if trend.locked
            ),
            level.completed_trends,
        )
        for level in result.levels
    )


def _assert_recursive_prefix(
    name,
    code,
    frequency,
    sizes,
    *,
    require_completed=False,
):
    frame = load_fixture(name, sizes[-1])
    assert len(frame) == sizes[-1]
    incremental = CL(code, frequency, strict_config())
    frozen_by_level = {}
    last = ()

    for size in sizes:
        incremental.process_klines(frame.head(size))
        current = strict_fingerprint(
            incremental.get_strict_structure_levels()
        )
        current_levels = {level for level, _centers, _trends, _done in current}
        assert set(frozen_by_level).issubset(current_levels)
        for level, centers, trends, completed in current:
            old_centers, old_trends, old_completed = frozen_by_level.get(
                level,
                ((), (), ()),
            )
            assert centers[: len(old_centers)] == old_centers
            assert trends[: len(old_trends)] == old_trends
            assert completed[: len(old_completed)] == old_completed
            frozen_by_level[level] = (centers, trends, completed)
        last = current

    batch = CL(code, frequency, strict_config())
    batch.process_klines(frame)
    assert strict_fingerprint(batch.get_strict_structure_levels()) == last
    assert last
    assert batch.get_strict_structure_levels().levels[0].units
    if require_completed:
        assert frozen_by_level[0][0], "fixture must produce completed L0 centers"
        assert frozen_by_level[0][2], "fixture must produce COMPLETE snapshots"


def test_maotai_recursive_locked_prefix_is_stable():
    _assert_recursive_prefix(
        "SH.600519_5m.parquet",
        "SH.600519",
        "5m",
        (400, 600, 800, 1000, 1200, 1400, 1500),
    )


def test_zhongji_recursive_locked_prefix_is_stable():
    _assert_recursive_prefix(
        "SZ.002299_1m.parquet",
        "SZ.002299",
        "1m",
        (1000, 2000, 4000, 6000, 8000),
        require_completed=True,
    )


def test_qqq_recursive_locked_prefix_is_stable():
    _assert_recursive_prefix(
        "QQQ.US_30m.parquet",
        "QQQ.US",
        "30m",
        (100, 250, 500, 700, 819),
    )
