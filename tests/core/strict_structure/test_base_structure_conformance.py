"""原文基础结构口径的可执行审计账本。

规则来源（仅标规则，不把结构一致性等同于收益保证）：
- L062：三 K 分型、笔与至少三笔线段。
- L065：方向包含、顺序原则及唯一严格笔口径。
- L067/L071：特征序列包含与有/无缺口的线段破坏。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.base_profile import (
    STRICT_BASE_PROFILE_ID,
    strict_base_config,
    strict_base_config_revision,
)
from chanlun.core.types import CLKline, FX, Kline
from chanlun.core.xd_calculator import _overlap, _process_inclusion


BASE = datetime(2026, 1, 5, 9, 30)


def _raw(index: int, high: float, low: float) -> Kline:
    return Kline(
        index=index,
        date=BASE + timedelta(minutes=index),
        h=high,
        l=low,
        o=low,
        c=high,
        a=1.0,
    )


def _cl(index: int, high: float, low: float) -> CLKline:
    raw = _raw(index, high, low)
    return CLKline(
        k_index=index,
        date=raw.date,
        h=high,
        l=low,
        o=low,
        c=high,
        a=1.0,
        klines=[raw],
        index=index,
        _n=1,
    )


def _fx(kind: str, cl_index: int, source_index: int, value: float) -> FX:
    middle = _cl(source_index, value + 1.0, value - 1.0)
    middle.index = cl_index
    return FX(kind, middle, [middle, middle, middle], value)


def test_l065_directional_inclusion_is_sequential_and_source_preserving():
    up = CL_Kline_Process()
    for bar in (
        _raw(0, 10, 5),
        _raw(1, 12, 7),
        _raw(2, 11, 8),
        _raw(3, 13, 7.5),
    ):
        up._process_one_kline(bar)

    assert [(k.h, k.l) for k in up.cl_klines] == [(10, 5), (13, 8)]
    assert [k.index for k in up.cl_klines[-1].klines] == [1, 2, 3]
    assert up.cl_klines[-1].up_qs == "up"

    down = CL_Kline_Process()
    for bar in (
        _raw(0, 15, 10),
        _raw(1, 13, 8),
        _raw(2, 12, 9),
        _raw(3, 12.5, 7),
    ):
        down._process_one_kline(bar)

    assert [(k.h, k.l) for k in down.cl_klines] == [(15, 10), (12, 7)]
    assert [k.index for k in down.cl_klines[-1].klines] == [1, 2, 3]
    assert down.cl_klines[-1].up_qs == "down"


def test_l062_fractal_requires_both_high_and_low_relationships():
    calc = BiCalculator()
    top = calc._find_fractal(_cl(0, 10, 5), _cl(1, 12, 7), _cl(2, 11, 6))
    bottom = calc._find_fractal(_cl(3, 11, 6), _cl(4, 9, 4), _cl(5, 10, 5))
    high_only = calc._find_fractal(_cl(6, 10, 5), _cl(7, 12, 4), _cl(8, 11, 6))

    assert top is not None and top.type == "ding" and top.val == 12
    assert bottom is not None and bottom.type == "di" and bottom.val == 4
    assert high_only is None


def test_l065_strict_stroke_uses_merged_k_distance_and_directional_endpoint():
    calc = BiCalculator()
    bottom = _fx("di", cl_index=10, source_index=10, value=90)
    too_near_top = _fx("ding", cl_index=13, source_index=13, value=110)
    valid_top = _fx("ding", cl_index=14, source_index=14, value=111)

    assert calc._check_stroke_validity(bottom, too_near_top) is False
    assert calc._check_stroke_validity(bottom, valid_top) is True

    endpoints = calc._build_endpoint_stack(
        [bottom, too_near_top, valid_top], incremental=False
    )
    assert endpoints == [bottom, valid_top]


@pytest.mark.parametrize(
    (
        "start_kind",
        "near_kind",
        "start_value",
        "near_value",
        "middle_value",
        "later_value",
    ),
    (
        ("ding", "di", 609.127, 591.101, 600.114, 597.153),
        ("di", "ding", 90.0, 110.0, 95.0, 105.0),
    ),
)
def test_l065_secondary_fractal_may_form_after_more_extreme_near_fractal(
    start_kind,
    near_kind,
    start_value,
    near_value,
    middle_value,
    later_value,
):
    """距离不足的反向分型不成笔，也不约束后续次高/次低端点。"""

    calc = BiCalculator()
    start = _fx(start_kind, cl_index=8, source_index=8, value=start_value)
    too_near = _fx(near_kind, cl_index=11, source_index=11, value=near_value)
    weaker_same_as_start = _fx(
        start_kind,
        cl_index=13,
        source_index=13,
        value=middle_value,
    )
    later_secondary = _fx(
        near_kind,
        cl_index=14,
        source_index=14,
        value=later_value,
    )
    all_fxs = [start, too_near, weaker_same_as_start, later_secondary]

    endpoints = calc._build_endpoint_stack(all_fxs, incremental=False)

    assert endpoints == [start, later_secondary]
    assert too_near in all_fxs


def test_l065_equal_same_type_fractal_keeps_earlier_endpoint():
    calc = BiCalculator()
    earlier = _fx("ding", cl_index=8, source_index=8, value=100)
    equal_later = _fx("ding", cl_index=12, source_index=12, value=100)
    bottom = _fx("di", cl_index=16, source_index=16, value=90)

    endpoints = calc._build_endpoint_stack(
        [earlier, equal_later, bottom], incremental=False
    )

    assert endpoints == [earlier, bottom]


def test_direct_bi_replay_detects_historical_change_when_last_k_is_unchanged():
    values = (
        (10, 8),
        (12, 10),
        (11, 9),
        (10, 8),
        (9, 7),
        (8, 6),
        (9, 7),
        (10, 8),
        (11, 9),
        (13, 11),
        (12, 10),
    )
    original = [_cl(index, high, low) for index, (high, low) in enumerate(values)]
    changed = copy.deepcopy(original)
    changed[5].h = 10.5
    changed[5].l = 8.5

    reused = BiCalculator()
    reused.calculate(copy.deepcopy(original))
    original_signature = [
        (bi.start.k.index, bi.end.k.index, bi.type) for bi in reused.bis
    ]
    reused.calculate(changed)

    fresh = BiCalculator()
    fresh.calculate(copy.deepcopy(changed))
    changed_signature = [
        (bi.start.k.index, bi.end.k.index, bi.type) for bi in reused.bis
    ]

    assert changed_signature != original_signature
    assert changed_signature == [
        (bi.start.k.index, bi.end.k.index, bi.type) for bi in fresh.bis
    ]


def test_cl_kline_structure_revision_is_monotonic_and_old_state_safe():
    processor = CL_Kline_Process()

    assert processor.structure_revision == 0
    processor.process_cl_klines([_raw(0, 10, 8)])
    assert processor.structure_revision == 1
    processor.process_cl_klines([_raw(0, 11, 9), _raw(1, 12, 10)])
    assert processor.structure_revision == 2

    del processor._structure_revision
    assert processor.structure_revision == 0
    processor.process_cl_klines([_raw(0, 11, 9), _raw(1, 13, 11)])
    assert processor.structure_revision == 1


def test_l067_feature_sequence_inclusion_is_directional_and_ordered():
    elems = [
        {"bi": SimpleNamespace(index=1), "high": 12, "low": 7},
        {"bi": SimpleNamespace(index=3), "high": 11, "low": 8},
        {"bi": SimpleNamespace(index=5), "high": 13, "low": 7.5},
    ]

    merged = _process_inclusion(elems, "up")

    assert len(merged) == 1
    assert (merged[0]["high"], merged[0]["low"]) == (13, 8)
    assert [bi.index for bi in merged[0]["merged_bis"]] == [1, 3, 5]


def test_l067_l071_gap_classification_uses_first_two_feature_elements():
    first = {"high": 12, "low": 8}
    no_gap_second = {"high": 10, "low": 7}
    gap_second = {"high": 7, "low": 5}

    assert _overlap(first, no_gap_second) is True
    assert _overlap(first, gap_second) is False


def test_strict_base_profile_contains_only_current_production_rules():
    config = dict(strict_base_config())

    assert STRICT_BASE_PROFILE_ID == "chanlun-source-faithful-base"
    assert config["strict_base_profile_id"] == STRICT_BASE_PROFILE_ID
    assert config["stroke_rule"] == "strict-cl-k-distance"
    assert config["stroke_secondary_fractal_rule"] == "allowed"
    assert config["stroke_endpoint_range_rule"] == (
        "fractal-endpoints-not-interval-extremes"
    )
    assert config["stroke_near_opposite_rule"] == (
        "retain-fractal-ignore-as-endpoint"
    )
    assert config["stroke_same_type_rule"] == "replace-only-if-more-extreme"
    assert config["stroke_equal_extreme_rule"] == "keep-earlier-fractal"
    assert config["stroke_lock_rule"] == (
        "next-valid-endpoint-physical-witness-no-rollback"
    )
    assert config["segment_rule"] == "feature-sequence"
    assert config["segment_gap_rule"] == "second-feature-sequence-fractal"
    assert config["strict_macd_source"] == (
        "same-physical-source-native-all-recursive-levels"
    )
    assert (
        config["strict_macd_level_policy"]
        == "exact-unit-source-interval"
    )
    assert config["strict_macd_area"] == "same_sign_magnitude"
    assert config["strict_macd_decay_rule"] == "area-or-peak-or-dif"
    assert config["center_seed_rule"] == (
        "physical-entry-middle-three-core-independent-leave-five-overlap;"
        "recursive-three-completed-trend-types"
    )
    assert config["center_lifecycle_rule"] == (
        "external-departure-first-outside-return-third-class"
    )
    assert config["center_scan_rule"] == (
        "five-role-physical-seed-causal-lifecycle-owner"
    )
    assert config["trend_divergence_rule"] == (
        "entry-width-matched-one-or-three-price-extreme-any-macd-decay;"
        "single-unit-exit-three-segment-nonextending-reversal-confirmation"
    )
    assert config["decomposition_rule"] == (
        "trend-or-consolidation-divergence-terminal-prefix-partition;"
        "centerless-three-segment-reversal-prefix-and-tail-movement-partition;"
        "causal-completion-ledger-current-recomposition-and-tail-extension"
    )
    assert config["first_class_rule"] == (
        "trend-or-consolidation-divergence-reversal"
    )
    assert config["second_class_rule"] == (
        "same-or-lower-first-adjacent-rebound-first-pullback"
    )
    assert not any(
        key.startswith(("zs_", "chart_", "recursive_")) or "mmd" in key
        for key in config
    )
    assert strict_base_config_revision().startswith("sha256:")
    assert strict_base_config_revision() == strict_base_config_revision()


def test_cl_uses_the_fixed_profile_without_a_runtime_structure_switch():
    cd = CL("TST", "1m", dict(strict_base_config()), market="a")

    assert cd.get_config() == strict_base_config()


@pytest.mark.parametrize(
    "unsupported_key",
    (
        "kline_type",
        "kline_qk",
        "fx_qy",
        "fx_qj",
        "fx_bh",
        "bi_type",
        "bi_mode",
        "bi_bzh",
        "bi_qj",
        "bi_fx_cgd",
        "xd_qj",
        "xd_bzh",
        "xd_bi_pohuai",
        "use_macd_ld",
        "macd_ld_use_htf",
    ),
)
def test_cl_rejects_unsupported_structure_config_fields(unsupported_key):
    with pytest.raises(ValueError, match="unsupported CL configuration fields"):
        CL("TST", "1m", {unsupported_key: "unsupported"}, market="a")
