from __future__ import annotations

from dataclasses import replace
import gc
import weakref

import pytest

from chanlun.decision_support.models import LevelSnapshot, StrategyTrack
from chanlun.decision_support.strategies import (
    evaluate_bottom_reversal,
    evaluate_trend_continuation,
    reversal_rank_key,
    trend_rank_key,
)
from tests.decision_support.conftest import ts

import pickle

import pandas as pd

import chanlun.decision_support.strategies as decision_strategies
from chanlun.recursive_bt.engine.engine import Signal
from chanlun.recursive_bt.select.chanlun_selector import (
    ASelectionConfig,
    FundamentalSnapshot,
    OriginalChanlunASelector,
    SelectionCandidate,
)


def _write_legacy_selector_fixture(tmp_path):
    dates = list(
        pd.date_range(
            "2026-07-13 09:30",
            periods=4,
            freq="5min",
            tz="Asia/Shanghai",
        )
    )
    cases = (
        ("SH.600001", "3buy", True),
        ("SH.600002", "2buy", False),
        ("SH.600003", "1buy", False),
        ("SH.600004", "3buy_nest", False),
        ("SH.600005", "1buy_nest", False),
    )
    for code, bs_type, daily_resonance in cases:
        signal = Signal(
            date=dates[-1],
            level=1,
            bs_type=bs_type,
            price=10.0,
            structural_stop_below=9.0,
        )
        data = {
            "name": code,
            "dates": dates,
            "close": [10.0] * len(dates),
            "small_by_bar": {len(dates) - 1: [signal]},
            "big_dir_at": ["neutral"] * len(dates),
            "mid_dir_at": ["up"] * len(dates),
            "d3_ok": [False, False, False, daily_resonance],
        }
        with (tmp_path / f"{code}.pkl").open("wb") as stream:
            pickle.dump(data, stream)

    return ASelectionConfig(
        bt_data=str(tmp_path),
        min_bars=len(dates),
        lookback_bars=len(dates),
        require_three_systems=False,
    )


def test_legacy_selector_default_and_evaluator_delegate_have_trend_code_parity(
    tmp_path,
    monkeypatch,
    make_decision_event,
):
    config = _write_legacy_selector_fixture(tmp_path)
    legacy_candidates = OriginalChanlunASelector(config).select()
    legacy_codes = [
        candidate.code
        for candidate in legacy_candidates
        if candidate.bs_type in decision_strategies.TREND_BUYS
    ]
    evaluator_calls = []
    adapter_calls = []
    evaluate = decision_strategies.evaluate_trend_continuation

    def adapter(candidate, data, signal):
        adapter_calls.append((candidate, data, signal))
        signal_at = pd.Timestamp(candidate.signal_time).to_pydatetime()
        observed_at = (
            pd.Timestamp(data["dates"][-1]) + pd.Timedelta(minutes=5)
        ).to_pydatetime()
        return make_decision_event(
            code=candidate.code,
            name=candidate.name,
            bs_type=candidate.bs_type,
            price=candidate.price,
            stop_below=signal.structural_stop_below,
            big_dir=data["big_dir_at"][-1],
            mid_dir=data["mid_dir_at"][-1],
            signal_at=signal_at,
            observed_at=observed_at,
        )

    def spy(*args, **kwargs):
        evaluator_calls.append((args, kwargs))
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(decision_strategies, "evaluate_trend_continuation", spy)
    delegated_codes = [
        candidate.code
        for candidate in OriginalChanlunASelector(
            config,
            trend_event_adapter=adapter,
        ).select()
    ]

    assert legacy_codes == ["SH.600001", "SH.600004", "SH.600002"]
    assert delegated_codes == legacy_codes
    assert len(adapter_calls) == len(legacy_candidates)
    assert len(evaluator_calls) == len(legacy_candidates)


def test_legacy_selector_rejects_malformed_delegated_event(tmp_path):
    config = _write_legacy_selector_fixture(tmp_path)

    with pytest.raises(TypeError, match="trend event adapter returned malformed event"):
        OriginalChanlunASelector(
            config,
            trend_event_adapter=lambda candidate, data, signal: object(),
        ).select()


def test_legacy_selector_candidate_helper_keeps_selection_candidate_return_type(tmp_path):
    config = _write_legacy_selector_fixture(tmp_path)
    with (tmp_path / "SH.600001.pkl").open("rb") as stream:
        data = pickle.load(stream)

    candidate = OriginalChanlunASelector(config)._candidate_from_symbol(
        "SH.600001",
        data,
    )

    assert isinstance(candidate, SelectionCandidate)


@pytest.mark.parametrize(
    "mismatch",
    ("big_direction", "mid_direction", "bar_closed_at"),
)
def test_legacy_selector_rejects_event_that_mismatches_cache_facts(
    tmp_path,
    make_decision_event,
    mismatch,
):
    config = _write_legacy_selector_fixture(tmp_path)

    def adapter(candidate, data, signal):
        signal_at = pd.Timestamp(candidate.signal_time).to_pydatetime()
        observed_at = (
            pd.Timestamp(data["dates"][-1]) + pd.Timedelta(minutes=5)
        ).to_pydatetime()
        values = {
            "code": candidate.code,
            "name": candidate.name,
            "bs_type": candidate.bs_type,
            "price": candidate.price,
            "stop_below": signal.structural_stop_below,
            "big_dir": data["big_dir_at"][-1],
            "mid_dir": data["mid_dir_at"][-1],
            "signal_at": signal_at,
            "observed_at": observed_at,
        }
        if mismatch == "big_direction":
            values["big_dir"] = "up"
        elif mismatch == "mid_direction":
            values["mid_dir"] = "neutral"
        else:
            values["bar_closed_at"] = observed_at + pd.Timedelta(minutes=5)
            values["observed_at"] = values["bar_closed_at"]
        return make_decision_event(**values)

    with pytest.raises(TypeError, match="trend event adapter returned malformed event"):
        OriginalChanlunASelector(
            config,
            trend_event_adapter=adapter,
        ).select()


def test_legacy_selector_delegate_preserves_max_codes_after_filtering(
    tmp_path,
    make_decision_event,
):
    config = replace(_write_legacy_selector_fixture(tmp_path), max_codes=1)

    def adapter(candidate, data, signal):
        signal_at = pd.Timestamp(candidate.signal_time).to_pydatetime()
        observed_at = (
            pd.Timestamp(data["dates"][-1]) + pd.Timedelta(minutes=5)
        ).to_pydatetime()
        return make_decision_event(
            code=candidate.code,
            name=candidate.name,
            bs_type=candidate.bs_type,
            price=candidate.price,
            stop_below=signal.structural_stop_below,
            big_dir=data["big_dir_at"][-1],
            mid_dir=data["mid_dir_at"][-1],
            signal_at=signal_at,
            observed_at=observed_at,
        )

    candidates = OriginalChanlunASelector(
        config,
        trend_event_adapter=adapter,
    ).select()

    assert [candidate.code for candidate in candidates] == ["SH.600001"]


def test_legacy_selector_delegate_requires_both_cached_directions(
    tmp_path,
) -> None:
    config = _write_legacy_selector_fixture(tmp_path)
    path = tmp_path / "SH.600001.pkl"
    with path.open("rb") as stream:
        data = pickle.load(stream)
    data.pop("mid_dir_at")
    with path.open("wb") as stream:
        pickle.dump(data, stream)
    adapter_calls = []

    with pytest.raises(TypeError, match="complete cache facts"):
        OriginalChanlunASelector(
            config,
            trend_event_adapter=lambda *args: adapter_calls.append(args),
        ).select()

    assert adapter_calls == []


def test_legacy_selector_freezes_cache_facts_before_adapter_call(
    tmp_path,
    make_decision_event,
) -> None:
    config = _write_legacy_selector_fixture(tmp_path)

    def adapter(candidate, data, signal):
        data["big_dir_at"][-1] = "up"
        signal.structural_stop_below = 8.5
        signal_at = pd.Timestamp(candidate.signal_time).to_pydatetime()
        observed_at = (
            pd.Timestamp(data["dates"][-1]) + pd.Timedelta(minutes=5)
        ).to_pydatetime()
        return make_decision_event(
            code=candidate.code,
            name=candidate.name,
            bs_type=candidate.bs_type,
            price=candidate.price,
            stop_below=signal.structural_stop_below,
            big_dir=data["big_dir_at"][-1],
            mid_dir=data["mid_dir_at"][-1],
            signal_at=signal_at,
            observed_at=observed_at,
        )

    with pytest.raises(TypeError, match="malformed event"):
        OriginalChanlunASelector(
            config,
            trend_event_adapter=adapter,
        ).select()


def test_legacy_selector_default_path_does_not_retain_full_cache_rows(
    monkeypatch,
) -> None:
    class _TrackedCache(dict):
        pass

    selector = OriginalChanlunASelector(
        ASelectionConfig(
            bt_data="unused",
            min_bars=1,
            require_three_systems=False,
        )
    )
    refs = []
    monkeypatch.setattr(
        selector,
        "_cache_files",
        lambda: ["SH.600001.pkl", "SH.600002.pkl", "SH.600003.pkl"],
    )

    def load(path):
        data = _TrackedCache(path=path)
        refs.append(weakref.ref(data))
        return data

    monkeypatch.setattr(selector, "_load", load)
    monkeypatch.setattr(
        selector,
        "_fundamental_snapshot",
        lambda code, data: FundamentalSnapshot(),
    )
    monkeypatch.setattr(
        selector,
        "_candidate_with_signal_from_symbol",
        lambda code, data: (
            SelectionCandidate(code, "3buy", "2026-07-13", 10.0, "up", 1),
            object(),
        ),
    )
    sort_key = selector._sort_key

    def assert_released(candidate):
        gc.collect()
        assert sum(reference() is not None for reference in refs) <= 1
        return sort_key(candidate)

    monkeypatch.setattr(selector, "_sort_key", assert_released)

    assert len(selector.select()) == 3


def test_trend_accepts_completed_three_buy_with_higher_levels_not_down(
    make_decision_event,
):
    event = make_decision_event(
        bs_type="3buy",
        big_dir="neutral",
        mid_dir="up",
        stop_below=9.0,
    )

    result = evaluate_trend_continuation(
        event,
        fund_ok=True,
        comparison_ok=True,
    )

    assert result.accepted is True
    assert result.observation is False
    assert result.track is StrategyTrack.TREND_CONTINUATION
    assert result.reasons == ()


def test_trend_rejects_current_big_down_even_if_signal_was_valid(
    make_decision_event,
):
    event = make_decision_event(
        bs_type="3buy",
        big_dir="down",
        mid_dir="up",
        stop_below=9.0,
    )

    result = evaluate_trend_continuation(
        event,
        fund_ok=True,
        comparison_ok=True,
    )

    assert result.accepted is False
    assert result.observation is False
    assert "big_level_down" in result.reasons


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"bs_type": "1buy"}, "unsupported_signal"),
        ({"mid_dir": "down"}, "mid_level_down"),
        ({"stop_below": None}, "missing_structural_stop"),
        ({"stop_below": 10.0}, "structural_stop_invalidated"),
    ),
)
def test_trend_rejects_each_technical_gate(
    make_decision_event,
    changes,
    reason,
):
    event = make_decision_event(**changes)

    result = evaluate_trend_continuation(
        event,
        fund_ok=True,
        comparison_ok=True,
    )

    assert result.accepted is False
    assert reason in result.reasons


@pytest.mark.parametrize(
    ("fund_ok", "comparison_ok", "reason"),
    (
        (False, True, "fundamental_gate_failed"),
        (True, False, "comparison_gate_failed"),
    ),
)
def test_trend_rejects_each_three_system_gate(
    make_decision_event,
    fund_ok,
    comparison_ok,
    reason,
):
    event = make_decision_event()

    result = evaluate_trend_continuation(
        event,
        fund_ok=fund_ok,
        comparison_ok=comparison_ok,
    )

    assert result.accepted is False
    assert reason in result.reasons


def test_reversal_lone_one_buy_stays_observation(make_decision_event):
    event = make_decision_event(
        bs_type="1buy",
        live_divergence=True,
        confirmation_bs_type=None,
        track=StrategyTrack.BOTTOM_REVERSAL,
    )

    result = evaluate_bottom_reversal(event)

    assert result.accepted is False
    assert result.observation is True
    assert result.track is StrategyTrack.BOTTOM_REVERSAL
    assert "missing_confirmation" in result.reasons


def test_reversal_accepts_live_divergence_with_sublevel_second_buy(
    make_decision_event,
):
    event = make_decision_event(
        bs_type="1buy_nest",
        live_divergence=True,
        confirmation_bs_type="2buy",
        stop_below=8.8,
        track=StrategyTrack.BOTTOM_REVERSAL,
    )

    result = evaluate_bottom_reversal(event)

    assert result.accepted is True
    assert result.observation is False
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("changes", "reason", "observation"),
    (
        ({"live_divergence": False}, "missing_live_divergence", True),
        ({"confirmation_bs_type": "1buy"}, "invalid_confirmation", True),
        ({"big_dir": "down"}, "big_level_down", True),
        ({"stop_below": 10.0}, "structural_stop_invalidated", False),
    ),
)
def test_reversal_execution_gates_are_explicit(
    make_decision_event,
    changes,
    reason,
    observation,
):
    defaults = {
        "bs_type": "1buy_nest",
        "live_divergence": True,
        "confirmation_bs_type": "2buy",
        "stop_below": 8.8,
        "track": StrategyTrack.BOTTOM_REVERSAL,
    }
    bypass_live_divergence = changes.get("live_divergence") is False
    construction_changes = {
        key: value
        for key, value in changes.items()
        if key != "live_divergence"
    }
    event = make_decision_event(**(defaults | construction_changes))
    if bypass_live_divergence:
        object.__setattr__(event.signal, "live_divergence", False)

    result = evaluate_bottom_reversal(event)

    assert result.accepted is False
    assert result.observation is observation
    assert reason in result.reasons


def test_trend_rank_is_daily_resonance_then_signal_freshness_and_code(
    make_decision_event,
):
    daily = LevelSnapshot(
        "1d",
        3,
        "up",
        True,
        8.0,
        10.0,
        8.8,
        9.5,
        mmds=("3buy",),
    )
    resonant_event = make_decision_event(code="SH.600003", bs_type="2buy")
    resonant_event = replace(
        resonant_event,
        levels=(*resonant_event.levels, daily),
    )
    fresh_three_buy = make_decision_event(
        code="SH.600002",
        bs_type="3buy",
        observed_at=ts("2026-07-13T10:40:00+08:00"),
    )
    old_three_buy = make_decision_event(
        code="SH.600001",
        bs_type="3buy_nest",
        observed_at=ts("2026-07-13T10:35:00+08:00"),
    )
    decisions = [
        evaluate_trend_continuation(
            event,
            fund_ok=True,
            comparison_ok=True,
        )
        for event in (old_three_buy, fresh_three_buy, resonant_event)
    ]

    ordered = sorted(decisions, key=trend_rank_key)

    assert [item.event.code for item in ordered] == [
        "SH.600003",
        "SH.600002",
        "SH.600001",
    ]


def test_reversal_rank_is_independent_and_rejects_trend_candidate(
    make_decision_event,
):
    second_buy = evaluate_bottom_reversal(
        make_decision_event(
            code="SH.600002",
            bs_type="1buy_nest",
            live_divergence=True,
            confirmation_bs_type="2buy",
            track=StrategyTrack.BOTTOM_REVERSAL,
        )
    )
    third_buy = evaluate_bottom_reversal(
        make_decision_event(
            code="SH.600001",
            bs_type="1buy_nest",
            live_divergence=True,
            confirmation_bs_type="3buy",
            track=StrategyTrack.BOTTOM_REVERSAL,
        )
    )
    trend = evaluate_trend_continuation(
        make_decision_event(),
        fund_ok=True,
        comparison_ok=True,
    )

    assert [
        item.event.code
        for item in sorted((second_buy, third_buy), key=reversal_rank_key)
    ] == ["SH.600001", "SH.600002"]
    with pytest.raises(ValueError, match="bottom-reversal"):
        reversal_rank_key(trend)


def test_trend_evaluator_fails_closed_on_unknown_direction(
    make_decision_event,
) -> None:
    event = make_decision_event(bs_type="3buy")
    object.__setattr__(event.levels[0], "direction", "DOWN")

    result = evaluate_trend_continuation(
        event,
        fund_ok=True,
        comparison_ok=True,
    )

    assert result.accepted is False
    assert "invalid_big_direction" in result.reasons


def test_reversal_evaluator_requires_qs_divergence_defensively(
    make_decision_event,
) -> None:
    event = make_decision_event(
        bs_type="1buy_nest",
        live_divergence=True,
        confirmation_bs_type="2buy",
        track=StrategyTrack.BOTTOM_REVERSAL,
    )
    object.__setattr__(event.signal, "divergence_kind", "pz")

    result = evaluate_bottom_reversal(event)

    assert result.accepted is False
    assert "invalid_divergence_kind" in result.reasons


def test_trend_evaluator_fails_closed_on_unknown_direction_in_any_level(
    make_decision_event,
) -> None:
    daily = LevelSnapshot(
        "1d",
        0,
        "up",
        True,
        8.0,
        10.0,
        8.8,
        9.5,
        mmds=("3buy",),
    )
    object.__setattr__(daily, "direction", "SIDEWAYS")
    event = replace(
        make_decision_event(bs_type="3buy"),
        levels=(*make_decision_event(bs_type="3buy").levels, daily),
    )

    result = evaluate_trend_continuation(
        event,
        fund_ok=True,
        comparison_ok=True,
    )

    assert result.accepted is False
    assert result.daily_resonance is False
    assert "invalid_level_direction" in result.reasons


def test_reversal_evaluator_fails_closed_on_unknown_direction_in_any_level(
    make_decision_event,
) -> None:
    extra = LevelSnapshot("1d", 0, "neutral", True, 8.0, 10.0, 8.8, 9.5)
    object.__setattr__(extra, "direction", "SIDEWAYS")
    base = make_decision_event(
        bs_type="1buy_nest",
        live_divergence=True,
        confirmation_bs_type="2buy",
        track=StrategyTrack.BOTTOM_REVERSAL,
    )
    event = replace(base, levels=(*base.levels, extra))

    result = evaluate_bottom_reversal(event)

    assert result.accepted is False
    assert result.observation is False
    assert "invalid_level_direction" in result.reasons
