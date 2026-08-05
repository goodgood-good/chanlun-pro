from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from chanlun.decision_support import event_factory as event_factory_module
from chanlun.decision_support.event_factory import (
    bind_rule_evaluation,
    event_from_signal,
    snapshot_levels,
    visible_signals,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.models import (
    DecisionEvent,
    LevelSnapshot,
    MarketConstraints,
    StrategyTrack,
)
from chanlun.decision_support.rule_cards import (
    EvaluationVerdict,
    RuleEvaluation,
)
from chanlun.recursive_bt.engine.engine import Signal
from tests.decision_support.conftest import ts


@dataclass
class _FakeLevel:
    frequency: str
    level: int
    direction: str
    completed: bool
    segment_start: float
    segment_end: float
    zs_zd: float
    zs_zg: float
    mmds: list[str]
    divergences: list[str]


class _FakeCL:
    frequency = "5m"

    def __init__(self) -> None:
        self.levels = [
            _FakeLevel(
                "30m",
                2,
                "neutral",
                True,
                8.0,
                10.0,
                8.8,
                9.5,
                [],
                [],
            ),
            _FakeLevel(
                "5m",
                1,
                "up",
                True,
                9.0,
                10.0,
                9.2,
                9.8,
                ["3buy"],
                ["pz"],
            ),
        ]

    def get_recursive_branch_levels(self):
        return self.levels


@pytest.fixture
def make_factory_input():
    def factory(
        *,
        operation_bar_closed: bool = True,
        close: float = 10.0,
    ) -> dict:
        observed_at = ts("2026-07-13T10:35:00+08:00")
        signal = Signal(
            date=observed_at,
            level=1,
            bs_type="3buy",
            price=close,
            structural_stop_below=9.0,
            zs_zd=9.2,
            zs_zg=9.8,
        )
        constraints = MarketConstraints(
            board="main",
            lot=100,
            t_plus=1,
            limit_pct=0.10,
            entry_tradable=True,
            exit_tradable=True,
            quote_time=observed_at,
        )
        completed_bars = (
            {
                "time": observed_at,
                "open": 9.8,
                "high": 10.1,
                "low": 9.7,
                "close": close,
                "volume": 1_000_000,
            },
        )
        return {
            "market": "a",
            "code": "SH.600519",
            "name": "贵州茅台",
            "frequency": "5m",
            "signal": signal,
            "first_visible_bar": 21,
            "observed_at": observed_at,
            "bar_closed_at": observed_at,
            "operation_bar_closed": operation_bar_closed,
            "cd": _FakeCL(),
            "market_constraints": constraints,
            "completed_bars": completed_bars,
            "config": {"recursive_l0_min_zs_lines": 5},
            "strategy_track": StrategyTrack.CHANLUN_SOURCE_FAITHFUL,
        }

    return factory


def test_event_factory_rejects_unclosed_operation_bar(
    make_factory_input,
) -> None:
    assert event_from_signal(
        **make_factory_input(operation_bar_closed=False)
    ) is None


def test_same_visible_signal_builds_same_event_id(
    make_factory_input,
) -> None:
    data = make_factory_input()

    first = event_from_signal(**data)
    second = event_from_signal(**data)

    assert first is not None
    assert second is not None
    assert first.event_id == second.event_id
    assert first.data_fingerprint == second.data_fingerprint
    assert first.config_fingerprint == second.config_fingerprint


def _rule_evaluation(**changes: object) -> RuleEvaluation:
    values: dict[str, object] = {
        "rule_id": "chanlun.third_buy",
        "rule_card_version": 1,
        "rule_card_fingerprint": "sha256:" + "1" * 64,
        "rule_set_fingerprint": "sha256:" + "2" * 64,
        "corpus_manifest_fingerprint": "sha256:" + "3" * 64,
        "algorithm_fingerprint": "sha256:" + "4" * 64,
        "evaluation_input_fingerprint": "sha256:" + "5" * 64,
        "strategy_track": StrategyTrack.CHANLUN_SOURCE_FAITHFUL,
        "level": 1,
        "verdict": EvaluationVerdict.CONFIRM,
        "candidate_satisfied": True,
        "confirmation_satisfied": True,
        "invalidation_triggered": False,
        "conflict_triggered": False,
        "critical_indeterminate": False,
        "safe_to_proceed": True,
        "reasons": (),
        "evidence_ids": ("lesson-20-main", "lesson-20-counter"),
        "supporting_evidence_ids": ("lesson-20-main",),
        "counterevidence_ids": ("lesson-20-counter",),
    }
    values.update(changes)
    return RuleEvaluation(**values)


def _bind_strategy_run(
    event,
    *,
    run_id: str = "paper-run-" + "a" * 64,
    epoch: int = 1,
    fingerprint: str = "sha256:" + "b" * 64,
):
    binder = getattr(
        event_factory_module,
        "bind_strategy_run_provenance",
        None,
    )
    assert callable(binder), "strategy-run provenance binder is unavailable"
    return binder(
        event,
        strategy_run_id=run_id,
        strategy_run_epoch=epoch,
        strategy_run_fingerprint=fingerprint,
    )


def test_bind_evaluation_builds_stable_provenance_event_id(
    make_factory_input,
) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None
    evaluation = _rule_evaluation()

    first = bind_rule_evaluation(event, evaluation)
    second = bind_rule_evaluation(event, evaluation)

    assert first == second
    assert first.rule_binding_status == "bound"
    assert first.rule_id == evaluation.rule_id
    assert first.rule_set_fingerprint == evaluation.rule_set_fingerprint
    assert first.data_fingerprint == evaluation.evaluation_input_fingerprint
    assert first.event_id.endswith("P" + first.provenance_fingerprint[7:])
    assert first.to_dict()["schema_version"] == 3


def test_bind_strategy_run_builds_schema_v4_provenance_without_changing_data(
    make_factory_input,
) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None
    rule_bound = bind_rule_evaluation(event, _rule_evaluation())

    first = _bind_strategy_run(rule_bound)
    second = _bind_strategy_run(first)

    expected_provenance = sha256_json(
        {
            "schema_version": 1,
            "rule_provenance_fingerprint": (
                rule_bound.provenance_fingerprint
            ),
            "strategy_run": {
                "strategy_run_id": "paper-run-" + "a" * 64,
                "strategy_run_epoch": 1,
                "strategy_run_fingerprint": "sha256:" + "b" * 64,
            },
        }
    )
    assert first is second
    assert first.data_fingerprint == rule_bound.data_fingerprint
    assert first.rule_provenance_fingerprint == (
        rule_bound.provenance_fingerprint
    )
    assert first.provenance_fingerprint == expected_provenance
    assert first.event_id.endswith("P" + expected_provenance[7:])
    assert first.to_dict()["schema_version"] == 4


@pytest.mark.parametrize(
    "changes",
    (
        {"run_id": "paper-run-" + "c" * 64},
        {"epoch": 2},
        {"fingerprint": "sha256:" + "d" * 64},
    ),
)
def test_each_strategy_run_component_changes_event_identity(
    make_factory_input,
    changes: dict[str, object],
) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None
    rule_bound = bind_rule_evaluation(event, _rule_evaluation())

    baseline = _bind_strategy_run(rule_bound)
    changed = _bind_strategy_run(rule_bound, **changes)

    assert changed.event_id != baseline.event_id
    assert changed.provenance_fingerprint != baseline.provenance_fingerprint
    assert changed.data_fingerprint == baseline.data_fingerprint


def test_a_to_b_to_a_strategy_runs_never_share_event_identity(
    make_factory_input,
) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None
    rule_bound = bind_rule_evaluation(event, _rule_evaluation())
    fingerprint_a = "sha256:" + "a" * 64

    first_a = _bind_strategy_run(
        rule_bound,
        run_id="paper-run-" + "1" * 64,
        epoch=1,
        fingerprint=fingerprint_a,
    )
    run_b = _bind_strategy_run(
        rule_bound,
        run_id="paper-run-" + "2" * 64,
        epoch=2,
        fingerprint="sha256:" + "b" * 64,
    )
    second_a = _bind_strategy_run(
        rule_bound,
        run_id="paper-run-" + "3" * 64,
        epoch=3,
        fingerprint=fingerprint_a,
    )

    assert len({first_a.event_id, run_b.event_id, second_a.event_id}) == 3
    assert first_a.data_fingerprint == run_b.data_fingerprint
    assert run_b.data_fingerprint == second_a.data_fingerprint


def test_strategy_run_provenance_binding_is_immutable(
    make_factory_input,
) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None
    bound = _bind_strategy_run(
        bind_rule_evaluation(event, _rule_evaluation())
    )

    with pytest.raises(ValueError, match="different strategy-run binding"):
        _bind_strategy_run(bound, epoch=2)


def test_rule_binding_is_idempotent_after_strategy_run_binding(
    make_factory_input,
) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None
    evaluation = _rule_evaluation()
    bound = _bind_strategy_run(bind_rule_evaluation(event, evaluation))

    assert bind_rule_evaluation(bound, evaluation) is bound


def test_bind_evaluation_keeps_event_identity_but_binds_distinct_inputs(
    make_factory_input,
) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None

    first = bind_rule_evaluation(event, _rule_evaluation())
    second = bind_rule_evaluation(
        event,
        _rule_evaluation(evaluation_input_fingerprint="sha256:" + "6" * 64),
    )

    assert first.event_id == second.event_id
    assert first.data_fingerprint != second.data_fingerprint
    with pytest.raises(ValueError, match="different rule binding"):
        bind_rule_evaluation(
            first,
            _rule_evaluation(
                evaluation_input_fingerprint="sha256:" + "6" * 64
            ),
        )


def test_bind_evaluation_rejects_identity_mismatch(make_factory_input) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None

    with pytest.raises(ValueError, match="signal level mismatch"):
        bind_rule_evaluation(event, _rule_evaluation(level=2))


def test_distinct_rule_or_corpus_binding_creates_distinct_event(
    make_factory_input,
) -> None:
    event = event_from_signal(**make_factory_input())
    assert event is not None

    baseline = bind_rule_evaluation(event, _rule_evaluation())
    changed_rule = bind_rule_evaluation(
        event,
        _rule_evaluation(rule_card_version=2),
    )
    changed_corpus = bind_rule_evaluation(
        event,
        _rule_evaluation(corpus_manifest_fingerprint="sha256:" + "f" * 64),
    )

    assert len({baseline.event_id, changed_rule.event_id, changed_corpus.event_id}) == 3


def test_completed_ohlc_change_changes_data_fingerprint(
    make_factory_input,
) -> None:
    first = event_from_signal(**make_factory_input(close=10.0))
    second = event_from_signal(**make_factory_input(close=10.01))

    assert first is not None
    assert second is not None
    assert first.data_fingerprint != second.data_fingerprint


def test_event_detaches_from_mutable_signal_and_level_sources(
    make_factory_input,
) -> None:
    data = make_factory_input()
    event = event_from_signal(**data)
    assert event is not None
    before = event.to_dict()

    data["signal"].price = 999.0
    data["cd"].levels[1].direction = "down"
    data["cd"].levels[1].mmds.append("1sell")

    assert event.to_dict() == before


def test_event_keeps_physical_source_separate_from_recursive_display_frequency(
    make_factory_input,
) -> None:
    data = make_factory_input()
    level = SimpleNamespace(
        level=1,
        direction="up",
        completed=True,
        segment_start=9.0,
        segment_end=10.0,
        zs_zd=9.2,
        zs_zg=9.8,
        mmds=("3buy",),
        divergences=(),
    )
    cd = SimpleNamespace(
        frequency="1m",
        _UPGRADE_CHAIN={"1m": (("5m", 6), ("30m", 6))},
        get_recursive_branch_levels=lambda: (level,),
    )
    data["cd"] = cd

    event = event_from_signal(**data)

    assert event is not None
    assert len(event.levels) == 1
    assert event.levels[0].frequency == "5m"
    assert event.levels[0].source_frequency == "1m"
    assert event.levels[0].source_bar_closed_at == data["bar_closed_at"]
    assert DecisionEvent.from_dict(event.to_dict()) == event


def test_snapshot_levels_preserves_existing_physical_source_binding() -> None:
    source_closed_at = ts("2026-07-13T10:30:00+08:00")
    snapshot_at = ts("2026-07-13T10:35:00+08:00")
    bound = LevelSnapshot(
        "30m",
        0,
        "neutral",
        True,
        9.0,
        10.0,
        9.2,
        9.8,
        trade_gate_direction="down",
        source_frequency="30m",
        source_bar_closed_at=source_closed_at,
    )
    cd = SimpleNamespace(
        frequency="1m",
        get_recursive_branch_levels=lambda: (bound,),
    )

    [restored] = snapshot_levels(
        cd,
        source_frequency="1m",
        source_bar_closed_at=snapshot_at,
    )

    assert restored.source_frequency == "30m"
    assert restored.source_bar_closed_at == source_closed_at
    assert restored.trade_gate_direction == "down"


def test_one_minute_level_zero_signal_cannot_bind_native_5m_snapshot(
    make_factory_input,
) -> None:
    data = make_factory_input()
    closed_at = data["bar_closed_at"]
    data["signal"] = replace(data["signal"], level=0)
    source_1m = LevelSnapshot(
        "1m",
        0,
        "up",
        True,
        9.0,
        10.0,
        9.2,
        9.8,
        source_frequency="1m",
        source_bar_closed_at=closed_at,
    )
    native_5m = replace(
        source_1m,
        frequency="5m",
        source_frequency="5m",
        trade_gate_direction="up",
    )
    data["cd"] = SimpleNamespace(
        frequency="1m",
        get_recursive_branch_levels=lambda: (source_1m, native_5m),
    )

    event = event_from_signal(**data)

    assert event is None


def test_snapshot_levels_copies_labels_and_structure() -> None:
    cd = _FakeCL()

    levels = snapshot_levels(cd)
    cd.levels[1].mmds.append("1sell")

    assert tuple(level.frequency for level in levels) == ("30m", "5m")
    assert levels[1].direction == "up"
    assert levels[1].mmds == ("3buy",)


def test_visible_signals_deduplicates_all_collectors(
    monkeypatch,
) -> None:
    from chanlun.decision_support import event_factory

    observed_at = ts("2026-07-13T10:35:00+08:00")
    first = Signal(
        observed_at,
        1,
        "3buy",
        10.0,
        structural_stop_below=9.0,
    )
    duplicate = Signal(
        observed_at,
        1,
        "3buy",
        10.0,
        structural_stop_below=9.0,
    )
    reversal = Signal(
        observed_at,
        0,
        "1buy_nest",
        9.8,
        structural_stop_below=8.8,
        divergence_kind="qs",
        live_divergence=True,
        confirmation_bs_type="2buy",
    )
    monkeypatch.setattr(
        event_factory,
        "collect_branch_signals",
        lambda *args, **kwargs: [first],
    )
    monkeypatch.setattr(
        event_factory,
        "collect_nest_cascade_signals",
        lambda *args, **kwargs: [duplicate],
    )
    monkeypatch.setattr(
        event_factory,
        "collect_qs_beichi_candidates",
        lambda *args, **kwargs: [reversal],
    )

    signals = visible_signals(_FakeCL())

    assert signals == (reversal, first)


def test_event_factory_rejects_empty_completed_bar_snapshot(
    make_factory_input,
) -> None:
    data = make_factory_input()
    data["completed_bars"] = ()

    with pytest.raises(ValueError, match="completed_bars cannot be empty"):
        event_from_signal(**data)


def test_event_factory_rejects_bar_after_semantic_close(
    make_factory_input,
) -> None:
    data = make_factory_input()
    future = dict(data["completed_bars"][0])
    future["time"] = data["bar_closed_at"] + timedelta(seconds=1)
    data["completed_bars"] = (*data["completed_bars"], future)

    with pytest.raises(ValueError, match="completed bar cannot be after bar_closed_at"):
        event_from_signal(**data)


def test_event_factory_rejects_signal_after_semantic_close(
    make_factory_input,
) -> None:
    data = make_factory_input()
    data["signal"].date = data["bar_closed_at"] + timedelta(seconds=1)
    data["observed_at"] = data["bar_closed_at"] + timedelta(seconds=2)

    with pytest.raises(ValueError, match="signal date cannot be after bar_closed_at"):
        event_from_signal(**data)


def test_processing_delay_does_not_change_event_identity(
    make_factory_input,
) -> None:
    immediate = make_factory_input()
    delayed = make_factory_input()
    delayed["observed_at"] = delayed["observed_at"] + timedelta(seconds=1)

    first = event_from_signal(**immediate)
    second = event_from_signal(**delayed)

    assert first is not None
    assert second is not None
    assert first.event_id == second.event_id
    assert first.observed_at == immediate["bar_closed_at"]
    assert second.observed_at == delayed["bar_closed_at"]


def test_visible_signals_normalizes_time_and_merges_enriched_metadata(
    monkeypatch,
) -> None:
    from chanlun.decision_support import event_factory

    shanghai_time = ts("2026-07-13T10:35:00+08:00")
    utc_time = ts("2026-07-13T02:35:00+00:00")
    ordinary = Signal(
        shanghai_time,
        0,
        "1buy_nest",
        9.8,
        structural_stop_below=8.8,
    )
    enriched = Signal(
        utc_time,
        0,
        "1buy_nest",
        9.8,
        structural_stop_below=8.8,
        divergence_kind="qs",
        live_divergence=True,
        confirmation_bs_type="2buy",
    )
    monkeypatch.setattr(
        event_factory,
        "collect_branch_signals",
        lambda *args, **kwargs: [ordinary],
    )
    monkeypatch.setattr(
        event_factory,
        "collect_nest_cascade_signals",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        event_factory,
        "collect_qs_beichi_candidates",
        lambda *args, **kwargs: [enriched],
    )

    signals = visible_signals(_FakeCL())

    assert len(signals) == 1
    assert signals[0].date == shanghai_time
    assert signals[0].divergence_kind == "qs"
    assert signals[0].live_divergence is True
    assert signals[0].confirmation_bs_type == "2buy"


def test_visible_signals_rejects_conflicting_structural_metadata(
    monkeypatch,
) -> None:
    from chanlun.decision_support import event_factory

    observed_at = ts("2026-07-13T10:35:00+08:00")
    first = Signal(
        observed_at,
        1,
        "3buy",
        10.0,
        structural_stop_below=9.0,
    )
    conflicting = Signal(
        observed_at,
        1,
        "3buy",
        10.0,
        structural_stop_below=8.0,
    )
    monkeypatch.setattr(
        event_factory,
        "collect_branch_signals",
        lambda *args, **kwargs: [first],
    )
    monkeypatch.setattr(
        event_factory,
        "collect_nest_cascade_signals",
        lambda *args, **kwargs: [conflicting],
    )
    monkeypatch.setattr(
        event_factory,
        "collect_qs_beichi_candidates",
        lambda *args, **kwargs: [],
    )

    with pytest.raises(ValueError, match="conflicting signal metadata"):
        visible_signals(_FakeCL())


@pytest.mark.parametrize(
    ("kind", "is_beichi"),
    (("pz", True), ("qs", False)),
)
def test_qs_collector_rejects_inconsistent_divergence_metadata(
    monkeypatch,
    kind,
    is_beichi,
) -> None:
    from chanlun.recursive_bt.engine import engine

    observed_at = ts("2026-07-13T10:35:00+08:00")
    point = SimpleNamespace(k=SimpleNamespace(date=observed_at))
    segment = SimpleNamespace(type="down", start=point)
    divergence = SimpleNamespace(
        kind=kind,
        is_beichi=is_beichi,
        provisional=True,
        leave_seg=segment,
    )
    zs = SimpleNamespace(dd=8.8, zd=9.0, zg=9.5)
    level = SimpleNamespace(level=0, live_qs_divergence=[(zs, divergence)])
    cd = SimpleNamespace(get_recursive_branch_levels=lambda: [level])
    confirmation = Signal(observed_at, 0, "2buy", 9.8)
    monkeypatch.setattr(
        engine,
        "collect_branch_signals",
        lambda *args, **kwargs: [confirmation],
    )

    assert engine.collect_qs_beichi_candidates(cd) == []


@pytest.mark.parametrize(
    ("signal_offset", "second_price"),
    ((-1, 10.0), (0, 10.1)),
)
def test_distinct_signal_time_or_price_has_distinct_event_identity(
    make_factory_input,
    signal_offset,
    second_price,
) -> None:
    first_data = make_factory_input(close=10.0)
    second_data = make_factory_input(close=10.0)
    second_data["signal"].date = (
        second_data["signal"].date + timedelta(minutes=signal_offset)
    )
    second_data["signal"].price = second_price

    first = event_from_signal(**first_data)
    second = event_from_signal(**second_data)

    assert first is not None
    assert second is not None
    assert first.event_id != second.event_id
    assert first.data_fingerprint != second.data_fingerprint
    assert first.signal.signal_at != second.signal.signal_at or (
        first.signal.price != second.signal.price
    )


def test_completed_bars_require_latest_bar_at_semantic_close(
    make_factory_input,
) -> None:
    data = make_factory_input()
    data["completed_bars"][0]["time"] -= timedelta(minutes=5)

    with pytest.raises(ValueError, match="latest completed bar must match"):
        event_from_signal(**data)


def test_completed_bars_require_strictly_increasing_times(
    make_factory_input,
) -> None:
    data = make_factory_input()
    duplicate = dict(data["completed_bars"][0])
    data["completed_bars"] = (duplicate, duplicate)

    with pytest.raises(ValueError, match="strictly chronological"):
        event_from_signal(**data)


def test_completed_bars_require_ohlcv_fields(make_factory_input) -> None:
    data = make_factory_input()
    incomplete = dict(data["completed_bars"][0])
    incomplete.pop("volume")
    data["completed_bars"] = (incomplete,)

    with pytest.raises(ValueError, match="OHLCV"):
        event_from_signal(**data)


def test_equivalent_completed_bar_time_aliases_have_same_fingerprint(
    make_factory_input,
) -> None:
    time_data = make_factory_input()
    date_data = make_factory_input()
    bar = dict(date_data["completed_bars"][0])
    bar["date"] = bar.pop("time")
    date_data["completed_bars"] = (bar,)

    first = event_from_signal(**time_data)
    second = event_from_signal(**date_data)

    assert first is not None
    assert second is not None
    assert first.data_fingerprint == second.data_fingerprint


def test_visible_signals_rejects_non_boolean_divergence_flag(
    monkeypatch,
) -> None:
    from chanlun.decision_support import event_factory

    observed_at = ts("2026-07-13T10:35:00+08:00")
    invalid = Signal(
        observed_at,
        0,
        "1buy_nest",
        9.8,
        structural_stop_below=8.8,
        divergence_kind="qs",
        live_divergence="false",
    )
    monkeypatch.setattr(
        event_factory,
        "collect_branch_signals",
        lambda *args, **kwargs: [invalid],
    )
    monkeypatch.setattr(
        event_factory,
        "collect_nest_cascade_signals",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        event_factory,
        "collect_qs_beichi_candidates",
        lambda *args, **kwargs: [],
    )

    with pytest.raises(ValueError, match="live_divergence must be boolean"):
        visible_signals(_FakeCL())


def test_qs_confirmation_must_fall_inside_divergence_segment(monkeypatch) -> None:
    from chanlun.recursive_bt.engine import engine

    start = ts("2026-07-13T10:30:00+08:00")
    end = ts("2026-07-13T10:35:00+08:00")
    segment = SimpleNamespace(
        type="down",
        start=SimpleNamespace(k=SimpleNamespace(date=start)),
        end=SimpleNamespace(k=SimpleNamespace(date=end)),
    )
    divergence = SimpleNamespace(
        kind="qs",
        is_beichi=True,
        provisional=True,
        leave_seg=segment,
    )
    zs = SimpleNamespace(dd=8.8, zd=9.0, zg=9.5)
    level = SimpleNamespace(level=0, live_qs_divergence=[(zs, divergence)])
    cd = SimpleNamespace(get_recursive_branch_levels=lambda: [level])
    late = Signal(end + timedelta(minutes=5), 0, "2buy", 9.8)
    monkeypatch.setattr(
        engine,
        "collect_branch_signals",
        lambda *args, **kwargs: [late],
    )

    assert engine.collect_qs_beichi_candidates(cd) == []


def test_qs_confirmation_inside_divergence_segment_is_preserved(
    monkeypatch,
) -> None:
    from chanlun.recursive_bt.engine import engine

    start = ts("2026-07-13T10:30:00+08:00")
    end = ts("2026-07-13T10:35:00+08:00")
    segment = SimpleNamespace(
        type="down",
        start=SimpleNamespace(k=SimpleNamespace(date=start)),
        end=SimpleNamespace(k=SimpleNamespace(date=end)),
    )
    divergence = SimpleNamespace(
        kind="qs",
        is_beichi=True,
        provisional=True,
        leave_seg=segment,
    )
    zs = SimpleNamespace(dd=8.8, zd=9.0, zg=9.5)
    level = SimpleNamespace(level=0, live_qs_divergence=[(zs, divergence)])
    cd = SimpleNamespace(get_recursive_branch_levels=lambda: [level])
    confirmation = Signal(end, 0, "2buy", 9.8)
    monkeypatch.setattr(
        engine,
        "collect_branch_signals",
        lambda *args, **kwargs: [confirmation],
    )

    signals = engine.collect_qs_beichi_candidates(cd)

    assert len(signals) == 1
    assert signals[0].bs_type == "1buy_nest"
    assert signals[0].divergence_kind == "qs"
    assert signals[0].confirmation_bs_type == "2buy"


@pytest.mark.parametrize(
    ("is_beichi", "provisional"),
    (("false", True), (True, "false"), (True, None)),
)
def test_qs_collector_requires_strict_boolean_metadata(
    monkeypatch,
    is_beichi,
    provisional,
) -> None:
    from chanlun.recursive_bt.engine import engine

    start = ts("2026-07-13T10:30:00+08:00")
    end = ts("2026-07-13T10:35:00+08:00")
    segment = SimpleNamespace(
        type="down",
        start=SimpleNamespace(k=SimpleNamespace(date=start)),
        end=SimpleNamespace(k=SimpleNamespace(date=end)),
    )
    divergence = SimpleNamespace(
        kind="qs",
        is_beichi=is_beichi,
        provisional=provisional,
        leave_seg=segment,
    )
    zs = SimpleNamespace(dd=8.8, zd=9.0, zg=9.5)
    level = SimpleNamespace(level=0, live_qs_divergence=[(zs, divergence)])
    cd = SimpleNamespace(get_recursive_branch_levels=lambda: [level])
    confirmation = Signal(end, 0, "2buy", 9.8)
    monkeypatch.setattr(
        engine,
        "collect_branch_signals",
        lambda *args, **kwargs: [confirmation],
    )

    assert engine.collect_qs_beichi_candidates(cd) == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("open", "9.8"),
        ("high", True),
        ("low", None),
        ("close", float("nan")),
        ("close", float("inf")),
        ("open", 0),
        ("volume", -1),
    ),
)
def test_completed_bars_reject_invalid_ohlcv_scalars(
    make_factory_input,
    field,
    value,
) -> None:
    data = make_factory_input()
    bar = dict(data["completed_bars"][0])
    bar[field] = value
    data["completed_bars"] = (bar,)

    with pytest.raises(ValueError, match="completed bar"):
        event_from_signal(**data)


@pytest.mark.parametrize(
    "updates",
    (
        {"low": 10.1, "open": 10.0, "high": 10.5, "close": 10.2},
        {"low": 9.0, "open": 10.6, "high": 10.5, "close": 10.2},
        {"low": 9.0, "open": 10.0, "high": 10.5, "close": 10.6},
        {"low": 10.5, "high": 9.0},
        {"low": -0.1},
    ),
)
def test_completed_bars_reject_invalid_price_ranges(
    make_factory_input,
    updates,
) -> None:
    data = make_factory_input()
    bar = dict(data["completed_bars"][0])
    bar.update(updates)
    data["completed_bars"] = (bar,)

    with pytest.raises(ValueError, match="completed bar price"):
        event_from_signal(**data)


def test_equivalent_integer_and_float_ohlcv_have_same_fingerprint(
    make_factory_input,
) -> None:
    integer_data = make_factory_input()
    float_data = make_factory_input()
    integer_bar = dict(integer_data["completed_bars"][0])
    float_bar = dict(float_data["completed_bars"][0])
    for field, value in {
        "open": 9,
        "high": 11,
        "low": 8,
        "close": 10,
        "volume": 1000,
    }.items():
        integer_bar[field] = value
        float_bar[field] = float(value)
    integer_data["completed_bars"] = (integer_bar,)
    float_data["completed_bars"] = (float_bar,)

    integer_event = event_from_signal(**integer_data)
    float_event = event_from_signal(**float_data)

    assert integer_event is not None
    assert float_event is not None
    assert integer_event.data_fingerprint == float_event.data_fingerprint


@pytest.mark.parametrize("value", (2**53 + 1, 10**400))
def test_signal_price_rejects_lossy_or_overflowing_integer(
    make_factory_input,
    value,
) -> None:
    data = make_factory_input()
    data["signal"] = replace(data["signal"], price=value)

    with pytest.raises(ValueError, match="signal.price must be a finite number"):
        event_from_signal(**data)


@pytest.mark.parametrize("value", (2**53 + 1, 10**400))
def test_completed_bar_volume_rejects_lossy_or_overflowing_integer(
    make_factory_input,
    value,
) -> None:
    data = make_factory_input()
    bar = dict(data["completed_bars"][0])
    bar["volume"] = value
    data["completed_bars"] = (bar,)

    with pytest.raises(
        ValueError,
        match="completed bar volume must be a finite number",
    ):
        event_from_signal(**data)


@pytest.mark.parametrize("value", (2**53 + 1, 10**400))
def test_visible_signals_rejects_lossy_or_overflowing_price(
    monkeypatch,
    value,
) -> None:
    from chanlun.decision_support import event_factory

    signal = Signal(
        ts("2026-07-13T10:35:00+08:00"),
        1,
        "3buy",
        value,
        structural_stop_below=9.0,
    )
    monkeypatch.setattr(
        event_factory,
        "collect_branch_signals",
        lambda cd, **kwargs: (signal,),
    )
    monkeypatch.setattr(
        event_factory,
        "collect_nest_cascade_signals",
        lambda cd: (),
    )
    monkeypatch.setattr(
        event_factory,
        "collect_qs_beichi_candidates",
        lambda cd: (),
    )

    with pytest.raises(ValueError, match="signal.price must be a finite number"):
        visible_signals(_FakeCL())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("direction", "", "direction must"),
        ("completed", "false", "completed must be boolean"),
        ("segment_start", 2**53 + 1, "segment_start must be a finite number"),
        ("zs_zd", 10**400, "zs_zd must be a finite number"),
    ),
)
def test_snapshot_levels_rejects_values_that_would_be_silently_rewritten(
    field,
    value,
    message,
) -> None:
    cd = _FakeCL()
    setattr(cd.levels[0], field, value)

    with pytest.raises(ValueError, match=message):
        snapshot_levels(cd)


def test_visible_signals_accepts_largest_exact_integer_price(monkeypatch) -> None:
    from chanlun.decision_support import event_factory

    signal = Signal(
        ts("2026-07-13T10:35:00+08:00"),
        1,
        "3buy",
        2**53,
        structural_stop_below=9.0,
    )
    monkeypatch.setattr(
        event_factory,
        "collect_branch_signals",
        lambda cd, **kwargs: (signal,),
    )
    monkeypatch.setattr(
        event_factory,
        "collect_nest_cascade_signals",
        lambda cd: (),
    )
    monkeypatch.setattr(
        event_factory,
        "collect_qs_beichi_candidates",
        lambda cd: (),
    )

    result = visible_signals(_FakeCL())

    assert result[0].price == float(2**53)


@pytest.mark.parametrize("field", ("open", "high", "low", "close", "volume"))
def test_each_completed_bar_field_rejects_lossy_integer(
    make_factory_input,
    field,
) -> None:
    data = make_factory_input()
    bar = dict(data["completed_bars"][0])
    bar[field] = 2**53 + 1
    data["completed_bars"] = (bar,)

    with pytest.raises(
        ValueError,
        match=f"completed bar {field} must be a finite number",
    ):
        event_from_signal(**data)


@pytest.mark.parametrize("value", (np.float32(10.0), np.int64(10)))
def test_signal_price_accepts_numpy_real_scalars(
    make_factory_input,
    value,
) -> None:
    data = make_factory_input()
    data["signal"] = replace(data["signal"], price=value)

    event = event_from_signal(**data)

    assert event is not None
    assert event.signal.price == 10.0
