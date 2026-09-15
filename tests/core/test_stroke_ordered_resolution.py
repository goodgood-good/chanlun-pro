"""Original-rule counterexamples for adjacency, equal extremes and completion evidence."""

import copy
from dataclasses import FrozenInstanceError
import random

import pytest

from chanlun.core.bi_calculator import BiCalculator, fractal_lock_witness
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.stroke_audit import audit_bi_adjacency, audit_bi_ranges
from chanlun.core.xd_calculator import XdCalculator
from tests.core.strict_structure.test_source_stroke_revision import _bars, _calculate
from tests.core.test_stroke_endpoint_candidates import _live_prefixes, _signature
from tests.core.test_bi_source_updates import _frame
from tests.core.strict_structure.real_history import strict_config


THREE_STROKES = [
    (12, 8), (10, 6), (12, 8), (14, 10), (16, 12), (20, 16),
    (18, 14), (17, 13), (16, 12), (14, 10), (16, 12), (18, 14),
    (20, 16), (24, 20), (22, 18),
]

EQUAL_BOTTOMS = [
    (19, 15), (20, 16), (18, 14), (16, 12), (14, 10), (10, 6),
    (11, 7), (12, 8), (14, 10), (12, 8), (11, 7), (10, 6),
    (10.5, 6.5), (11, 7), (11.5, 7.5), (12, 8), (11, 7),
]


def _geometry(calc):
    return [(bi.start.k.index, bi.end.k.index) for bi in calc.bis]


@pytest.mark.parametrize("mirror", [False, True])
def test_extreme_endpoints_do_not_allow_swallowing_three_qualified_strokes(mirror):
    merged, calc = _calculate(_bars(THREE_STROKES, mirror))
    assert _geometry(calc) == [(1, 5), (5, 9), (9, 13)]
    coarse = calc._create_bi(calc.fxs[0], calc.fxs[-1], 0)
    assert audit_bi_ranges([coarse], merged.cl_klines) == []
    violations = audit_bi_adjacency([coarse], calc.fxs, merged.cl_klines)
    assert len(violations) == 1
    assert violations[0].subdivision_centers == (1, 5, 9, 13)
    assert calc.audit_endpoint_adjacency() == []


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_extreme_keeps_first_selected_endpoint_under_user_price_policy(mirror):
    for _,calc in _live_prefixes(_bars(EQUAL_BOTTOMS,mirror)):pass
    assert _geometry(calc)==[(1,5),(5,15)]
    by_center={fx.k.index:fx for fx in calc.fxs}
    assert calc._check_stroke_validity(by_center[5],by_center[15])
    assert not calc.confirmed_bis and not calc.unresolved_endpoint_choices
    assert [b.completion_status for b in calc.bis]==['continued','qualified']


@pytest.mark.parametrize("mirror", [False, True])
def test_retained_equal_extreme_gets_completion_from_the_third_qualified_edge(mirror):
    values=EQUAL_BOTTOMS+[(10,6),(9.5,5.5),(9,5),(10,6)]
    for end,calc in _live_prefixes(_bars(values,mirror)):
        if end==len(EQUAL_BOTTOMS):assert _geometry(calc)==[(1,5),(5,15)]
    assert _geometry(calc)==[(1,5),(5,15),(15,19)]
    assert len(calc.completion_evidence)==1
    assert calc.completion_evidence[0].witnessed_at==_bars(values,mirror)[20].date


@pytest.mark.parametrize("mirror", [False, True])
def test_qualification_and_next_stroke_confirmation_have_distinct_physical_times(mirror):
    prices = [(12, 8), (10, 6), (11, 7), (12, 8), (13, 9), (14, 10),
              (13, 9), (12, 8), (11, 7), (9, 5), (10, 6)]
    bars = _bars(prices, mirror)
    _, calc = _calculate(bars)
    assert len(calc.completion_evidence) == 0
    assert calc.completion_status == "tail_pending"
    assert len(calc.qualification_evidence) == 2
    evidence = calc.qualification_evidence[0]
    nodes = calc._resolver.nodes
    start, end = [nodes[i].fx for i in (evidence.start, evidence.end)]
    assert (start.k.index, end.k.index) == (1, 5)
    assert calc._check_stroke_validity(start, end)
    assert evidence.fractal_visible_at == fractal_lock_witness(end) == bars[6].date
    assert evidence.selected_at == calc.bis[0].selected_at == bars[6].date
    assert calc.bis[0].locked_at is None
    assert calc.continuation_evidence[0].witnessed_at == bars[10].date
    assert calc.continuation_evidence[0].following_end == calc.qualification_evidence[1].end
    assert calc.bis[-1].locked_at is None
    with pytest.raises(FrozenInstanceError):
        evidence.start = -1


def test_corrected_live_right_shoulder_restores_selection_and_qualification_checkpoint():
    prices = [(12, 8), (10, 6), (11, 7), (12, 8), (13, 9), (14, 10),
              (13, 9), (12, 8), (11, 7), (9, 5), (10, 6)]
    bars = _bars(prices)
    merged, live = CL_Kline_Process(), BiCalculator()
    merged.process_cl_klines(bars)
    live.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                   validated_incremental_prefix=True)
    assert len(live.qualification_evidence) == 2
    assert not live.completion_evidence
    assert len(live.continuation_evidence) == 1
    corrected = copy.deepcopy(bars)
    corrected[-1].h, corrected[-1].l = 8, 4
    corrected[-1].o, corrected[-1].c = 4, 8
    merged.process_cl_klines(corrected)
    live.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                   validated_incremental_prefix=True)
    _, cold = _calculate(corrected)
    assert _signature(live) == _signature(cold)
    assert live.completion_evidence == cold.completion_evidence == ()
    assert live.qualification_evidence == cold.qualification_evidence
    assert len(live.qualification_evidence) == 1
    assert _geometry(live) == [(1, 5)]


@pytest.mark.parametrize("mirror", [False, True])
def test_physical_fractals_remain_formed_when_stroke_endpoint_selection_is_pending(mirror):
    _, calc = _calculate(_bars(EQUAL_BOTTOMS, mirror))
    assert len(calc.pending_bis) == 2
    assert calc.confirmed_bis == []
    assert all(fx.done and fx.to_dict()["done"] for fx in calc.fxs)
    assert calc.bis[-1].forming and calc.bis[-1].locked_at is None
    assert calc.bis[-1].end.done


@pytest.mark.parametrize("seed", [0, 7, 29, 150])
def test_ordered_resolution_replays_without_range_adjacency_or_confirmed_history_errors(seed):
    rng = random.Random(seed)
    prices, price = [], 100
    for _ in range(80):
        price += rng.randint(-8, 8)
        prices.append((price + rng.randint(0, 5), price - rng.randint(0, 5)))
    for _, calc in _live_prefixes(_bars(prices)):
        assert all(calc._check_endpoint_geometry(b.start,b.end) for b in calc.bis)
        assert len(calc.completion_evidence) == len(calc.confirmed_bis)


@pytest.mark.parametrize("schema", ["chanlun-analysis-cl-v4", "chanlun-analysis-cl-v5", "chanlun-analysis-cl-v6", "chanlun-analysis-cl-v7", "chanlun-analysis-cl-v8", "chanlun-analysis-cl-v9", "chanlun-analysis-cl-v10", "chanlun-analysis-cl-v11"])
def test_cached_candidate_and_unsupported_confirmation_engines_are_rejected_before_reuse(schema):
    current = CL("TST", "1m", market="a")
    state = current.__getstate__()
    state["_pickle_schema"] = schema
    restored = object.__new__(CL)
    with pytest.raises(ValueError, match="pickle schema is invalid"):
        restored.__setstate__(state)


@pytest.mark.parametrize("mirror", [False, True])
def test_earlier_equal_endpoint_keeps_its_original_selection_time(mirror):
    raw=_bars(EQUAL_BOTTOMS,mirror)
    _,calc=_calculate(raw)
    earlier,following=calc.qualification_evidence
    assert earlier.fractal_visible_at==earlier.selected_at==raw[6].date
    assert following.selected_at==raw[16].date
    assert earlier.selected_by!=following.selected_by
    assert not calc.selection_revisions


@pytest.mark.parametrize("mirror", [False, True])
def test_completed_first_pen_survives_reselection_of_the_pending_segment(mirror):
    values=THREE_STROKES+[(8,4),(16,12),(26,22),(20,16),(16,12),(10,6),(6,2),(8,4)]
    raw=_bars(values,mirror)
    merged,live,xd=CL_Kline_Process(),BiCalculator(),XdCalculator()
    merged.process_cl_klines(raw[:len(THREE_STROKES)])
    live.calculate(merged.cl_klines,source_revision=merged.structure_revision,validated_incremental_prefix=True)
    completed=live.completion_evidence
    assert len(completed)==1 and not xd.calculate(live.bis)[0].is_done()
    merged.process_cl_klines(raw)
    live.calculate(merged.cl_klines,source_revision=merged.structure_revision,validated_incremental_prefix=True)
    assert _geometry(live)==[(1,5),(5,21)]
    assert live.completion_evidence==completed
    assert xd.calculate(live.bis)==[]
    _,batch=_calculate(raw)
    assert _signature(live)==_signature(batch)


@pytest.mark.parametrize("mirror", [False, True])
def test_a_disconnected_candidate_preserves_unchanged_continuous_center_inputs(mirror):
    prices = THREE_STROKES + [(8, 4), (16, 12), (26, 22), (20, 16),
                              (16, 12), (10, 6), (6, 2), (8, 4)]
    frame = _frame(prices, mirror)
    frame["date"] = frame["date"].dt.tz_localize("UTC")
    live = CL("TST", "1m", strict_config(), market="a")
    live.process_klines(frame.head(len(THREE_STROKES)))
    assert len(live.get_xds()) == 1
    live.get_native_centers()
    live.get_strict_structure_levels()
    previous_registry = live._strict_unit_registry
    previous_confirmations = dict(previous_registry._confirmed_at)
    # The initial XD is still pending. Reselecting its constituent tail
    # removes it; registry confirmation records remain unchanged.
    sentinel = object()
    live._strict_center_prefix_cache["unchanged-source-prefix"] = sentinel
    live.process_klines(frame)
    assert live.get_xds() == []
    assert not live.get_stroke_construction_state()["unresolved_regions"]
    assert live._strict_unit_registry is previous_registry
    assert live._strict_unit_registry._confirmed_at == previous_confirmations
    assert live._strict_center_prefix_cache["unchanged-source-prefix"] is sentinel
    assert live._strict_structure_memo == {}
    cold = CL("TST", "1m", strict_config(), market="a")
    cold.process_klines(frame)
    assert live.get_native_centers() == cold.get_native_centers()
    assert live.get_strict_structure_levels() == cold.get_strict_structure_levels()
    assert live._strict_unit_registry is previous_registry

    # A source revision must not silently authorize a new price basis.
    live.config["price_basis_revision"] = "changed-basis"
    with pytest.raises(ValueError, match="price basis changed"):
        live._strict_registry()


def test_new_successor_preserves_unchanged_confirmed_prefix_and_registry():
    prices = THREE_STROKES + [(20, 16), (18, 14), (16, 12), (18, 14)]
    frame = _frame(prices, False)
    frame["date"] = frame["date"].dt.tz_localize("UTC")
    live = CL("TST", "1m", strict_config(), market="a")
    live.process_klines(frame.head(len(THREE_STROKES)))
    first = tuple(live.bi_calculator.confirmed_bis)
    registry = live._strict_registry()
    live.process_klines(frame)
    assert len(live.bi_calculator.confirmed_bis) == len(first) + 1
    assert tuple(live.bi_calculator.confirmed_bis[:len(first)]) == first
    assert live._strict_registry() is registry
