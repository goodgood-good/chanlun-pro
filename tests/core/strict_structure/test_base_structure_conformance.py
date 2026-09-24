"""原文基础结构口径的可执行审计账本。

规则来源（仅标规则，不把结构一致性等同于收益保证）：
- L062：三 K 分型、笔与至少三笔线段。
- L065/L077：方向包含、顺序及顶底价格关系；旧笔保留独立的合并 K 线。
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


def test_old_stroke_requires_an_independent_merged_bar():
    calc = BiCalculator()
    bottom = _fx("di", cl_index=10, source_index=10, value=90)
    too_near_top = _fx("ding", cl_index=13, source_index=13, value=110)
    valid_top = _fx("ding", cl_index=14, source_index=14, value=111)

    assert calc._check_endpoint_geometry(bottom, too_near_top) is False
    assert calc._check_endpoint_geometry(bottom, valid_top) is True
    assert calc._check_stroke_validity(bottom, valid_top) is False  # no full interval evidence

    with pytest.raises(ValueError, match="physical fractal requires visibility evidence"):
        calc._build_endpoint_stack([bottom, too_near_top, valid_top], incremental=False)


def test_l077_equal_same_type_fractal_keeps_earlier_endpoint():
    values = [(12, 8), (10, 6), (11, 7), (12, 8), (13, 9), (14, 10),
              (13, 9), (12, 8), (13, 9), (14, 10), (13, 9)]
    calc = BiCalculator()
    calc.calculate([_cl(i, h, l) for i, (h, l) in enumerate(values)])
    assert [(bi.start.k.index, bi.end.k.index) for bi in calc.bis] == [(1, 5)]


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
    changed[5].klines[0].h = 10.5
    changed[5].klines[0].l = 8.5

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
    assert config["center_seed_rule"] == "physical-entry-middle-three-core-independent-leave-five-overlap"
    assert config["chart_structure_rule"] == "native-segment-centers-v2"
    assert config["stroke_rule"] == "old-user-path-b-completed-prefix-v14"
    assert config["stroke_distance_rule"] == "merged-centers-at-least-four"
    assert config["stroke_secondary_fractal_rule"] == "retained-facts-without-internal-extreme-veto"
    assert config["stroke_endpoint_range_rule"] == (
        "symmetric-center-ranges-user-P-C-policy"
    )
    assert config["stroke_near_opposite_rule"] == (
        "path-b-tail-reselection-with-completed-prefix-guard"
    )
    assert config["stroke_same_type_rule"] == "strict-extreme-replacement-of-mutable-tail"
    assert config["stroke_equal_extreme_rule"] == "keep-earlier-fractal"
    assert config["stroke_lock_rule"] == (
        "third-qualified-edge-with-closed-source-witness"
    )
    assert config["inclusion_initial_context_rule"] == "common-price-geometry-with-separate-source-provenance-v2"
    assert config["stroke_path_rule"] == "causal-selected-chain-without-discarded-path-restoration"
    assert config["stroke_downstream_rule"] == "continuous-formal-input-and-isolated-conditional-observations-v2"
    assert config["stroke_completion_scope"] == "qualified-continued-and-final-completion-distinguished"
    assert config["segment_rule"] == "feature-sequence"
    assert config["segment_gap_rule"] == (
        "second-feature-fractal-with-physical-turn-continuation-v4"
    )
    assert config["segment_equal_extreme_rule"] == (
        "earliest-middle-feature-boundary-source-with-separate-stem-provenance-v2"
    )
    assert config["center_lifecycle_rule"] == (
        "external-departure-first-outside-return-third-class"
    )
    assert config["center_scan_rule"] == (
        "five-role-physical-seed-causal-lifecycle-owner"
    )
    assert not any(
        key.startswith(("zs_", "recursive_")) or "mmd" in key
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
