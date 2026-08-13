from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.macd_htf import CausalPartialHigherMACDCalculator
from chanlun.core.strict_structure.base_profile import strict_base_config
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind
from chanlun.core.strict_structure.strength import (
    ComparisonMeasurement,
    MacdStrengthProvider,
    MacdStrengthUnavailable,
    StrengthSnapshot,
    _comparison_measurement,
    compare_divergence,
)
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from chanlun.core.types import Kline


BASE = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)


def source_dates(count: int) -> tuple[datetime, ...]:
    return tuple(BASE + timedelta(minutes=index) for index in range(count))


def shifted_source_dates(count: int) -> tuple[datetime, ...]:
    return tuple(value + timedelta(seconds=1) for value in source_dates(count))


def source_klines(closes) -> tuple[Kline, ...]:
    return tuple(
        Kline(
            index=index,
            date=BASE + timedelta(minutes=index),
            h=float(close),
            l=float(close),
            o=float(close),
            c=float(close),
            a=0.0,
        )
        for index, close in enumerate(closes)
    )


class FakeCD:
    def __init__(self, dates, *, native, htf_by_level):
        self._dates = tuple(dates)
        self._native = dict(native)
        self._strict_htf_macd_by_level = dict(htf_by_level)

    def get_src_klines(self):
        return tuple(SimpleNamespace(date=value) for value in self._dates)

    def get_idx(self):
        return {"macd": dict(self._native)}

def fake_strength_provider(
    *,
    native_hist,
    native_dif=None,
    htf_hist=None,
    htf_dif=None,
    htf_dates=None,
    htf_known_at=None,
    htf_bucket_keys=None,
    htf_algorithm=None,
):
    native_hist = tuple(native_hist)
    native = {
        "hist": native_hist,
        "dif": tuple(native_dif if native_dif is not None else native_hist),
    }
    htf_by_level = {}
    if htf_hist is not None:
        selected_hist = tuple(htf_hist)
        selected_dif = tuple(htf_dif if htf_dif is not None else selected_hist)
        htf_by_level[0] = {
            "hist": selected_hist,
            "dif": selected_dif,
            "dates": tuple(htf_dates or source_dates(len(selected_hist))),
            "known_at": tuple(htf_known_at or source_dates(len(selected_hist))),
            "bucket_keys": tuple(
                htf_bucket_keys
                if htf_bucket_keys is not None
                else range(len(selected_hist))
            ),
            "algorithm": htf_algorithm or "causal-partial-htf",
        }
    return MacdStrengthProvider(
        FakeCD(
            source_dates(len(native_hist)),
            native=native,
            htf_by_level=htf_by_level,
        )
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
    structural_level: int = 0,
) -> ConstituentUnit:
    dates = source_dates(right + 1)
    if start_tick is None:
        start_tick = 100 if direction == "up" else 110
    if end_tick is None:
        end_tick = 110 if direction == "up" else 100
    available_at = dates[right] + timedelta(minutes=1)
    return ConstituentUnit(
        unit_id=unit_id,
        structural_level=structural_level,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision="test-raw",
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


def test_noncausal_htf_macd_is_rejected():
    with pytest.raises(ValueError, match="causal HTF MACD context"):
        fake_strength_provider(
            native_hist=(9, 9, 9),
            htf_hist=(1, 2, 1),
            htf_algorithm="closed-bucket-htf",
        )


def test_physical_strength_uses_native_same_period_macd():
    provider = fake_strength_provider(
        native_hist=(2, 3, 4),
        htf_hist=(5, -40, 3),
        htf_algorithm="causal-partial-htf",
    )

    snapshot = provider.snapshot(unit_covering_indexes(0, 2, direction="up"))

    assert snapshot.source == "macd"
    assert snapshot.histogram_area == 9


def test_recursive_strength_uses_first_causal_partial_higher_period():
    provider = fake_strength_provider(
        native_hist=(99, 99, 99),
        htf_hist=(5, -40, 3),
        htf_algorithm="causal-partial-htf",
    )

    snapshot = provider.snapshot(
        unit_covering_indexes(0, 2, direction="up", structural_level=1)
    )

    assert snapshot.source == "macd"
    assert snapshot.histogram_area == 8


def test_formal_strength_fails_closed_when_causal_htf_is_unavailable():
    provider = fake_strength_provider(native_hist=(1, 2, 3))

    with pytest.raises(MacdStrengthUnavailable, match="structural level"):
        provider.snapshot(
            unit_covering_indexes(0, 2, direction="up", structural_level=1)
        )


def test_formal_causal_htf_snapshot_is_prefix_stable_after_new_source_bar():
    before = fake_strength_provider(
        native_hist=(1, 2, 1),
        htf_hist=(4, -1, 2),
        htf_algorithm="causal-partial-htf",
    ).snapshot(
        unit_covering_indexes(0, 2, direction="up", structural_level=1)
    )
    after = fake_strength_provider(
        native_hist=(1, 2, 1, 9),
        htf_hist=(4, -1, 2, -999),
        htf_algorithm="causal-partial-htf",
    ).snapshot(
        unit_covering_indexes(0, 2, direction="up", structural_level=1)
    )

    assert before == after
    assert before.source == "macd"


def test_causal_partial_htf_calculator_never_rewrites_a_frozen_prefix():
    calculator = CausalPartialHigherMACDCalculator(
        "1m",
        "a",
        fast=2,
        slow=3,
        signal=2,
    )
    bars = source_klines((100, 102, 101, 105, 90, 110))
    prefix = calculator.update(list(bars[:4]))
    assert prefix is not None
    frozen = {
        key: tuple(prefix[key])
        for key in ("dif", "dea", "hist", "dates", "known_at", "bucket_keys")
    }

    extended = calculator.update(list(bars[:5]))

    assert extended is not None
    assert extended["algorithm"] == "causal-partial-htf"
    for key, values in frozen.items():
        assert tuple(extended[key][: len(values)]) == values


def test_strength_area_uses_only_histogram_bars_in_leg_direction():
    provider = fake_strength_provider(
        native_hist=(-9, 2, 3, -4),
    )

    up = provider.snapshot(unit_covering_indexes(0, 3, direction="up"))
    down = provider.snapshot(unit_covering_indexes(0, 3, direction="down"))

    assert up.histogram_area == 5
    assert up.histogram_peak == 3
    assert down.histogram_area == 13
    assert down.histogram_peak == -9


def test_causal_htf_area_counts_each_target_bucket_once():
    provider = fake_strength_provider(
        native_hist=(99,) * 10,
        htf_hist=(0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 1.4, 1.3, 1.2, 1.1),
        htf_algorithm="causal-partial-htf",
        htf_bucket_keys=(0, 0, 0, 0, 0, 1, 1, 1, 1, 1),
    )

    snapshot = provider.snapshot(
        unit_covering_indexes(0, 9, direction="up", structural_level=1)
    )

    assert snapshot.histogram_area == pytest.approx(2.1)
    assert snapshot.histogram_peak == pytest.approx(1.1)


def test_causal_htf_open_bucket_uses_only_the_unit_endpoint_sample():
    base = fake_strength_provider(
        native_hist=(99,) * 7,
        htf_hist=(0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 1.4),
        htf_algorithm="causal-partial-htf",
        htf_bucket_keys=(0, 0, 0, 0, 0, 1, 1),
    ).snapshot(
        unit_covering_indexes(0, 6, direction="up", structural_level=1)
    )
    extended = fake_strength_provider(
        native_hist=(99,) * 10,
        htf_hist=(0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 1.4, -999, -999, -999),
        htf_algorithm="causal-partial-htf",
        htf_bucket_keys=(0, 0, 0, 0, 0, 1, 1, 1, 1, 1),
    ).snapshot(
        unit_covering_indexes(0, 6, direction="up", structural_level=1)
    )

    assert base == extended
    assert base.histogram_area == pytest.approx(2.4)


def test_same_length_but_misaligned_htf_dates_are_rejected():
    with pytest.raises(ValueError, match="causal HTF MACD context"):
        fake_strength_provider(
            native_hist=(1, 2, 1),
            htf_hist=(9, 9, 9),
            htf_dates=shifted_source_dates(3),
        )


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
            source="macd",
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


def test_three_unit_measurement_preserves_counter_displaced_real_endpoints():
    first = unit_covering_indexes(
        0,
        1,
        direction="down",
        unit_id="entry",
        start_tick=100,
        end_tick=90,
    )
    reverse = unit_covering_indexes(
        1,
        2,
        direction="up",
        unit_id="reverse",
        start_tick=90,
        end_tick=120,
    )
    terminal = unit_covering_indexes(
        2,
        3,
        direction="down",
        unit_id="re-entry",
        start_tick=120,
        end_tick=110,
    )

    measurement = _comparison_measurement((first, reverse, terminal))

    assert isinstance(measurement, ComparisonMeasurement)
    assert measurement.direction == "down"
    assert measurement.start_tick == 100
    assert measurement.end_tick == 110
    assert measurement.low_tick == 90
    assert measurement.high_tick == 120
    assert measurement.child_ids == ("entry", "reverse", "re-entry")
    snapshot = fake_strength_provider(
        native_hist=(-1, -2, -3, -4),
    ).snapshot(measurement)
    assert snapshot.histogram_area == 10


def test_area_decay_alone_is_sufficient_macd_decay_for_formal_divergence():
    earlier, later = same_direction_units_with_new_low()
    provider = TableProvider(
        {
            "earlier": (100, -5, -1),
            "later": (80, -6, -2),
        }
    )
    evidence = compare_divergence(earlier, later, provider, kind="trend")
    assert evidence.is_divergent is True
    assert evidence.histogram_area_decayed is True
    assert evidence.histogram_peak_decayed is False
    assert evidence.dif_extreme_decayed is False
    assert evidence.strength_source == "macd"
    assert evidence.structural_level == 0
    assert evidence.divergence_id
    assert evidence.anchor_at == later.market_end
    assert evidence.confirmed_at == later.confirmed_at


def test_peak_or_dif_decay_can_confirm_divergence_without_area_decay():
    earlier, later = same_direction_units_with_new_low()
    provider = TableProvider(
        {
            "earlier": (100, -5, -2),
            "later": (120, -3, -1),
        }
    )
    evidence = compare_divergence(earlier, later, provider, kind="trend")
    assert evidence.histogram_area_decayed is False
    assert evidence.histogram_peak_decayed is True
    assert evidence.dif_extreme_decayed is True
    assert evidence.is_divergent is True
    assert evidence.strength_decay_count == 2


@pytest.mark.parametrize(
    ("later", "expected_flag"),
    (
        ((80, -6, -2), "histogram_area_decayed"),
        ((120, -3, -2), "histogram_peak_decayed"),
        ((120, -6, -0.5), "dif_extreme_decayed"),
    ),
)
def test_each_macd_indicator_can_independently_confirm_divergence(
    later,
    expected_flag,
):
    earlier, signal = same_direction_units_with_new_low()
    evidence = compare_divergence(
        earlier,
        signal,
        TableProvider(
            {
                "earlier": (100, -5, -1),
                "later": later,
            }
        ),
        kind="trend",
    )

    assert evidence.is_divergent is True
    assert evidence.strength_decay_count == 1
    assert getattr(evidence, expected_flag) is True


def test_strength_rejects_non_finite_macd_bars():
    with pytest.raises(ValueError, match="finite"):
        fake_strength_provider(native_hist=(1, np.nan, 2)).snapshot(
            unit_covering_indexes(0, 2, direction="up")
        )


def test_missing_directional_histogram_keeps_dif_available():
    snapshot = fake_strength_provider(
        native_hist=(-1, -2, -1),
        native_dif=(1, 2, 3),
    ).snapshot(unit_covering_indexes(0, 2, direction="up"))

    assert snapshot.histogram_area is None
    assert snapshot.histogram_peak is None
    assert snapshot.dif_extreme == 3


def test_dif_alone_confirms_when_directional_histogram_is_unavailable():
    earlier, later = same_direction_units_with_new_low()
    evidence = compare_divergence(
        earlier,
        later,
        TableProvider(
            {
                "earlier": (None, None, -2),
                "later": (None, None, -1),
            }
        ),
        kind="trend",
    )

    assert evidence.histogram_area_decayed is False
    assert evidence.histogram_peak_decayed is False
    assert evidence.dif_extreme_decayed is True
    assert evidence.is_divergent is True


def test_strict_strength_matches_directional_native_formula_for_locked_xd_pair():
    frame = (
        pd.read_parquet("tests/fixtures/SH.600519_5m.parquet")[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(1500)
        .reset_index(drop=True)
    )
    config = {
        **strict_base_config(),
        "price_basis_revision": "test-raw",
        "structure_price_quantum": "0.01",
    }
    cd = CL("SH.600519", "5m", config, market="a")
    cd.process_klines(frame)
    raw_lines = tuple(cd.get_xds())
    units = adapt_lines(
        raw_lines,
        0,
        SourceKind.SEGMENT,
        Decimal("0.01"),
        cd.get_src_klines()[-1].date,
        UnitLockRegistry("test-raw"),
    )
    provider = MacdStrengthProvider(cd)
    native = cd.get_idx()["macd"]
    native_dates = tuple(kline.date for kline in cd.get_src_klines())

    def expected_strength(value):
        left = native_dates.index(value.market_start)
        right = native_dates.index(value.market_end) + 1
        hist = np.asarray(native["hist"][left:right])
        dif = np.asarray(native["dif"][left:right])
        if value.direction == "up":
            directional = hist[hist > 0]
            return (
                None if directional.size == 0 else directional.sum(),
                None if directional.size == 0 else directional.max(),
                dif.max(),
            )
        directional = hist[hist < 0]
        return (
            None if directional.size == 0 else abs(directional.sum()),
            None if directional.size == 0 else directional.min(),
            dif.min(),
        )

    compared = False
    for later_index, (later_line, later_unit) in enumerate(zip(raw_lines, units)):
        if not later_unit.locked:
            continue
        for _earlier_line, earlier_unit in zip(
            raw_lines[:later_index], units[:later_index]
        ):
            if (
                not earlier_unit.locked
                or earlier_unit.direction != later_unit.direction
            ):
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
            direction = later_unit.direction
            earlier_area, earlier_peak, earlier_dif = expected_strength(earlier_unit)
            later_area, later_peak, later_dif = expected_strength(later_unit)
            assert earlier_snapshot.source == later_snapshot.source == "macd"
            assert earlier_snapshot.histogram_area == (
                None if earlier_area is None else pytest.approx(earlier_area)
            )
            assert later_snapshot.histogram_area == (
                None if later_area is None else pytest.approx(later_area)
            )
            assert earlier_snapshot.histogram_peak == (
                None if earlier_peak is None else pytest.approx(earlier_peak)
            )
            assert later_snapshot.histogram_peak == (
                None if later_peak is None else pytest.approx(later_peak)
            )
            assert earlier_snapshot.dif_extreme == pytest.approx(earlier_dif)
            assert later_snapshot.dif_extreme == pytest.approx(later_dif)
            assert evidence.price_extreme_confirmed == (
                later_unit.high_tick > earlier_unit.high_tick
                if direction == "up"
                else later_unit.low_tick < earlier_unit.low_tick
            )
            assert evidence.histogram_area_decayed == (
                earlier_area is not None
                and later_area is not None
                and later_area < earlier_area
            )
            assert evidence.histogram_peak_decayed == (
                earlier_peak is not None
                and later_peak is not None
                and (
                    later_peak < earlier_peak
                    if direction == "up"
                    else later_peak > earlier_peak
                )
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
