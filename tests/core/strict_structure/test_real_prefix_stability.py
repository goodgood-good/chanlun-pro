from decimal import Decimal

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines


def _frame(path, rows):
    return pd.read_parquet(path)[
        ["date", "open", "high", "low", "close", "volume"]
    ].head(rows).reset_index(drop=True)


def _closed_fingerprint(result):
    return tuple(
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
        for center in result.centers
        if center.completed_at is not None
    )


def _strict_cl(code, frequency):
    config = strict_base_config()
    config.update(
        {
        }
    )
    return CL(code, frequency, config, market="a")


def _assert_real_prefix_stability(
    path,
    code,
    frequency,
    row_counts,
    *,
    require_completed=False,
):
    registry = UnitLockRegistry("test-raw")
    frozen = ()
    last_units = ()
    saw_formal = False

    for rows in row_counts:
        frame = _frame(path, rows)
        assert len(frame) == rows
        cd = _strict_cl(code, frequency)
        cd.process_klines(frame)
        as_of = frame.iloc[-1]["date"].to_pydatetime()
        units = adapt_lines(
            cd.get_xds(),
            0,
            SourceKind.SEGMENT,
            Decimal("0.01"),
            as_of,
            registry,
        )
        result = calculate_centers(units, 0, SourceKind.SEGMENT)
        saw_formal = saw_formal or bool(result.centers)

        assert result.locked_unit_count == sum(item.locked for item in units)
        assert all(
            item.locked
            for center in result.centers
            for item in center.body_units
        )
        assert all(
            item.confirmed_at <= as_of
            for item in units
            if item.locked
        )
        current = _closed_fingerprint(result)
        assert current[: len(frozen)] == frozen
        frozen = current
        last_units = units

    assert len(last_units) >= 5, "fixture must produce enough segment evidence"
    assert saw_formal, "fixture must produce a formal center"
    if require_completed:
        assert frozen, "fixture must produce a completed formal center"


def test_maotai_confirmed_center_prefix_never_rewrites():
    _assert_real_prefix_stability(
        "tests/fixtures/SH.600519_5m.parquet",
        "SH.600519",
        "5m",
        (400, 600, 800, 1000, 1200, 1400, 1500),
    )


def test_zhongji_confirmed_center_prefix_never_rewrites():
    _assert_real_prefix_stability(
        "tests/fixtures/SZ.002299_1m.parquet",
        "SZ.002299",
        "1m",
        (1000, 2000, 4000, 6000, 8000),
        require_completed=True,
    )
