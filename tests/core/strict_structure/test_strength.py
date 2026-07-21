from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind
from chanlun.core.strict_structure.strength import (
    MacdStrengthProvider,
    MacdStrengthUnavailable,
    StrengthSnapshot,
    compare_divergence,
)
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from chanlun.core.types import query_macd_ld


BASE = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)


def source_dates(count: int) -> tuple[datetime, ...]:
    return tuple(BASE + timedelta(minutes=index) for index in range(count))


def shifted_source_dates(count: int) -> tuple[datetime, ...]:
    return tuple(value + timedelta(seconds=1) for value in source_dates(count))


class FakeCD:
    def __init__(self, native_hist, *, native_dif=None, htf=None):
        self._dates = source_dates(len(native_hist))
        self._native = {
            "hist": tuple(native_hist),
            "dif": tuple(native_dif or native_hist),
        }
        self._htf_macd = htf

    def get_src_klines(self):
        return tuple(SimpleNamespace(date=value) for value in self._dates)

    def get_idx(self):
        return {"macd": self._native}


def fake_strength_provider(
    *,
    native_hist,
    native_dif=None,
    htf_hist=None,
    htf_dif=None,
    htf_dates=None,
):
    htf = None
    if htf_hist is not None:
        htf = {
            "hist": tuple(htf_hist),
            "dif": tuple(htf_dif or htf_hist),
            "dates": tuple(htf_dates or source_dates(len(htf_hist))),
        }
    return MacdStrengthProvider(
        FakeCD(native_hist, native_dif=native_dif, htf=htf)
    )


def unit_covering_indexes(
    left: int,
    right: int,
    *,
    direction: str,
    unit_id: str = "unit",
    locked: bool = True,
    start_tick: int | None = None,
    end_tick: int | None = None,
) -> ConstituentUnit:
    dates = source_dates(right + 1)
    if start_tick is None:
        start_tick = 100 if direction == "up" else 110
    if end_tick is None:
        end_tick = 110 if direction == "up" else 100
    available_at = dates[right] + timedelta(minutes=1)
    return ConstituentUnit(
        unit_id=unit_id,
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision="test-raw-v1",
        direction=direction,
        start_tick=start_tick,
        end_tick=end_tick,
        low_tick=min(start_tick, end_tick),
        high_tick=max(start_tick, end_tick),
        market_start=dates[left],
        market_end=dates[right],
        confirmed_at=available_at if locked else None,
        available_at=available_at,
        locked=locked,
        child_ids=(),
    )


def test_strength_slice_uses_only_unit_market_interval():
    provider = fake_strength_provider(native_hist=(9, 8, 1, 2, 7))
    snapshot = provider.snapshot(unit_covering_indexes(2, 3, direction="up"))
    assert snapshot.histogram_area == 3


def test_strength_prefers_aligned_htf_macd():
    provider = fake_strength_provider(
        native_hist=(9, 9, 9),
        htf_hist=(1, 2, 1),
        htf_dates=source_dates(3),
    )
    snapshot = provider.snapshot(unit_covering_indexes(0, 2, direction="up"))
    assert snapshot.source == "macd_htf"
    assert snapshot.histogram_area == 4


def test_same_length_but_misaligned_htf_dates_fall_back_to_native():
    provider = fake_strength_provider(
        native_hist=(1, 2, 1),
        htf_hist=(9, 9, 9),
        htf_dates=shifted_source_dates(3),
    )
    snapshot = provider.snapshot(unit_covering_indexes(0, 2, direction="up"))
    assert snapshot.source == "macd_native"
    assert snapshot.histogram_area == 4


class TableProvider:
    def __init__(self, values):
        self._values = values

    def snapshot(self, unit):
        area, peak, dif = self._values[unit.unit_id]
        return StrengthSnapshot(
            unit_id=unit.unit_id,
            direction=unit.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd_htf",
            available_at=unit.available_at,
        )


def same_direction_units_with_new_low():
    earlier = unit_covering_indexes(
        0,
        1,
        direction="down",
        unit_id="earlier",
        start_tick=110,
        end_tick=90,
    )
    later = unit_covering_indexes(
        2,
        3,
        direction="down",
        unit_id="later",
        start_tick=105,
        end_tick=80,
    )
    return earlier, later


def test_divergence_requires_new_extreme_area_peak_and_dif_decay():
    earlier, later = same_direction_units_with_new_low()
    provider = TableProvider(
        {
            "earlier": (100, -5, -2),
            "later": (80, -3, -1),
        }
    )
    evidence = compare_divergence(earlier, later, provider, kind="trend")
    assert evidence.is_divergent is True
    assert evidence.strength_source == "macd_htf"
    assert evidence.structural_level == 0
    assert evidence.divergence_id
    assert evidence.anchor_at == later.market_end
    assert evidence.confirmed_at == later.confirmed_at


def test_missing_dif_decay_rejects_divergence_even_when_area_is_smaller():
    earlier, later = same_direction_units_with_new_low()
    provider = TableProvider(
        {
            "earlier": (100, -5, -1),
            "later": (80, -3, -2),
        }
    )
    evidence = compare_divergence(earlier, later, provider, kind="trend")
    assert evidence.histogram_area_decayed is True
    assert evidence.histogram_peak_decayed is True
    assert evidence.dif_extreme_decayed is False
    assert evidence.is_divergent is False


def test_strength_rejects_non_finite_or_missing_directional_macd_bars():
    with pytest.raises(ValueError, match="finite"):
        fake_strength_provider(native_hist=(1, np.nan, 2)).snapshot(
            unit_covering_indexes(0, 2, direction="up")
        )
    with pytest.raises(MacdStrengthUnavailable, match="no directional MACD bars"):
        fake_strength_provider(native_hist=(-1, -2, -1)).snapshot(
            unit_covering_indexes(0, 2, direction="up")
        )


def test_strict_strength_matches_existing_formula_for_locked_xd_pair():
    frame = pd.read_parquet("tests/fixtures/SH.600519_5m.parquet")[
        ["date", "open", "high", "low", "close", "volume"]
    ].head(1500).reset_index(drop=True)
    config = {
        **strict_base_config(),
        "price_basis_revision": "test-raw-v1",
        "structure_price_quantum": "0.01",
        "macd_ld_use_htf": True,
        "skip_legacy_zslx": True,
        "skip_legacy_mmd": True,
    }
    cd = CL("SH.600519", "5m", config)
    cd.process_klines(frame)
    raw_lines = tuple(cd.get_xds())
    units = adapt_lines(
        raw_lines,
        0,
        SourceKind.SEGMENT,
        Decimal("0.01"),
        cd.get_src_klines()[-1].date,
        UnitLockRegistry("test-raw-v1"),
    )
    provider = MacdStrengthProvider(cd)

    compared = False
    for later_index, (later_line, later_unit) in enumerate(zip(raw_lines, units)):
        if not later_unit.locked:
            continue
        for earlier_line, earlier_unit in zip(
            raw_lines[:later_index], units[:later_index]
        ):
            if not earlier_unit.locked or earlier_unit.direction != later_unit.direction:
                continue
            try:
                earlier_snapshot = provider.snapshot(earlier_unit)
                later_snapshot = provider.snapshot(later_unit)
            except ValueError:
                continue
            evidence = compare_divergence(
                earlier_unit,
                later_unit,
                provider,
                kind="trend",
            )
            earlier_ld = query_macd_ld(cd, earlier_line.start, earlier_line.end)
            later_ld = query_macd_ld(cd, later_line.start, later_line.end)
            direction = later_unit.direction
            earlier_area = earlier_ld["hist"][
                "up_sum" if direction == "up" else "down_sum"
            ]
            later_area = later_ld["hist"][
                "up_sum" if direction == "up" else "down_sum"
            ]
            earlier_peak = earlier_ld["hist"][
                "max" if direction == "up" else "min"
            ]
            later_peak = later_ld["hist"][
                "max" if direction == "up" else "min"
            ]
            earlier_dif = earlier_ld["dif"][
                "max" if direction == "up" else "min"
            ]
            later_dif = later_ld["dif"][
                "max" if direction == "up" else "min"
            ]
            assert earlier_snapshot.source == later_snapshot.source == "macd_htf"
            assert evidence.price_extreme_confirmed == (
                later_unit.high_tick > earlier_unit.high_tick
                if direction == "up"
                else later_unit.low_tick < earlier_unit.low_tick
            )
            assert evidence.histogram_area_decayed == (later_area < earlier_area)
            assert evidence.histogram_peak_decayed == (
                later_peak < earlier_peak
                if direction == "up"
                else later_peak > earlier_peak
            )
            assert evidence.dif_extreme_decayed == (
                later_dif < earlier_dif
                if direction == "up"
                else later_dif > earlier_dif
            )
            compared = True
            break
        if compared:
            break
    assert compared, "fixture must contain a comparable locked XD pair"
