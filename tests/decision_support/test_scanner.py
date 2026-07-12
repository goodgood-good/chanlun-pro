from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionReview,
    TableByDecisionTransition,
    TableByRiskSnapshot,
)
from chanlun.decision_support.event_factory import (
    bind_rule_evaluation,
    bind_strategy_run_provenance,
)
from chanlun.decision_support.event_service import DecisionEventService
from chanlun.decision_support.event_store import DecisionEventStore
from chanlun.decision_support.models import EventState, StrategyTrack
from chanlun.decision_support.rule_cards import EvaluationVerdict
from chanlun.decision_support.rule_context import RuleRuntimeFacts
from chanlun.decision_support.scanner import (
    DecisionScanner,
    SymbolStructureSnapshot,
    UniverseSnapshot,
)
from chanlun.decision_support.universe import SecuritySnapshot
from chanlun.recursive_bt.engine.engine import Signal
from tests.decision_support.conftest import ts


@dataclass
class _Level:
    frequency: str
    level: int
    direction: str
    completed: bool
    segment_start: float
    segment_end: float
    zs_zd: float
    zs_zg: float
    mmds: tuple[str, ...] = ()
    divergences: tuple[str, ...] = ()


class _CL:
    frequency = "5m"

    def __init__(self) -> None:
        self.levels = (
            _Level("30m", 2, "neutral", True, 8.0, 10.0, 8.8, 9.5),
            _Level("5m", 1, "up", True, 9.0, 10.0, 9.2, 9.8),
        )

    def get_recursive_branch_levels(self):
        return self.levels


class _Clock:
    def count_closed_bars(self, event, asof) -> int:
        return 0


class _RuleEngine:
    def __init__(
        self,
        evaluation_factory,
        *,
        verdict: EvaluationVerdict = EvaluationVerdict.CONFIRM,
        safe_to_proceed: bool = True,
    ) -> None:
        self._evaluation_factory = evaluation_factory
        self._verdict = verdict
        self._safe_to_proceed = safe_to_proceed
        self.calls = []

    def evaluate(self, event, runtime_facts):
        assert isinstance(runtime_facts, RuleRuntimeFacts)
        self.calls.append((event, runtime_facts))
        evaluation = self._evaluation_factory(
            event,
            verdict=self._verdict,
            safe_to_proceed=self._safe_to_proceed,
        )
        return bind_rule_evaluation(event, evaluation), evaluation


class _ManualCheckWorkflow:
    def __init__(self) -> None:
        self.calls = []

    def capture_candidate(self, *, event, runtime_facts, evaluation):
        self.calls.append((event, runtime_facts, evaluation))


def _security(code: str, at, **changes) -> SecuritySnapshot:
    values = {
        "market": "a",
        "code": code,
        "name": code,
        "listed_days": 1000,
        "suspended": False,
        "delisting": False,
        "avg_turnover_20d": 200_000_000.0,
        "quote_time": at,
        "limit_up_locked": False,
        "limit_down_locked": False,
    }
    return SecuritySnapshot(**(values | changes))


def _structure(at, *signals: Signal) -> SymbolStructureSnapshot:
    return SymbolStructureSnapshot(
        frequency="5m",
        cd=_CL(),
        signals=signals,
        first_visible_bar=21,
        completed_bars=(
            {
                "time": at,
                "open": 9.8,
                "high": 10.1,
                "low": 9.7,
                "close": 10.0,
                "volume": 1_000_000,
            },
        ),
        config={"recursive_l0_min_zs_lines": 5},
        operation_bar_closed=True,
        fund_ok=True,
        comparison_ok=True,
    )


@pytest.fixture
def scanner_parts(make_risk_context, make_rule_evaluation):
    at = ts("2026-07-13T10:35:00+08:00")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TableByDecisionEvent.__table__.create(engine)
    TableByDecisionTransition.__table__.create(engine)
    TableByDecisionReview.__table__.create(engine)
    TableByRiskSnapshot.__table__.create(engine)
    store = DecisionEventStore(
        sessionmaker(bind=engine, expire_on_commit=False)
    )
    service = DecisionEventService(store, _Clock())
    rule_engine = _RuleEngine(make_rule_evaluation)
    securities = [_security("SH.600001", at)]
    structures = {}
    calls = []

    def universe_provider(closed_at):
        return UniverseSnapshot(at, tuple(securities))

    def structure_provider(security, closed_at):
        calls.append(security.code)
        value = structures[security.code]
        if isinstance(value, BaseException):
            raise value
        return value

    def risk_context_provider(security, event, closed_at):
        return make_risk_context(
            quote_code=security.code,
            entry_reference=str(event.signal.price),
            quote_time=closed_at,
            entry_tradable=security.entry_tradable,
            exit_tradable=security.exit_tradable,
            limit_up_locked=bool(security.security.limit_up_locked),
            limit_down_locked=bool(security.security.limit_down_locked),
            asof=closed_at,
        )

    try:
        yield {
            "at": at,
            "service": service,
            "securities": securities,
            "structures": structures,
            "calls": calls,
            "universe_provider": universe_provider,
            "structure_provider": structure_provider,
            "risk_context_provider": risk_context_provider,
            "rule_engine": rule_engine,
            "make_rule_evaluation": make_rule_evaluation,
        }
    finally:
        engine.dispose()


def _scanner(parts, **changes) -> DecisionScanner:
    values = {
        "universe_provider": parts["universe_provider"],
        "structure_provider": parts["structure_provider"],
        "risk_context_provider": parts["risk_context_provider"],
        "event_service": parts["service"],
        "rule_engine": parts["rule_engine"],
    }
    return DecisionScanner(**(values | changes))


def test_scanner_ignores_duplicate_same_closed_bar(scanner_parts):
    at = scanner_parts["at"]
    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )
    scanner = _scanner(scanner_parts)

    first = scanner.scan_closed_bar(at)
    second = scanner.scan_closed_bar(at)

    assert first.created_count == 1
    assert second.created_count == 0
    assert second.code == "duplicate_bar"
    assert scanner_parts["calls"] == ["SH.600001"]


def test_scanner_keeps_tracks_separate(scanner_parts):
    at = scanner_parts["at"]
    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
        Signal(
            at,
            1,
            "1buy_nest",
            9.8,
            structural_stop_below=8.8,
            divergence_kind="qs",
            live_divergence=True,
            confirmation_bs_type="2buy",
        ),
    )
    scanner = _scanner(scanner_parts)

    result = scanner.scan_closed_bar(at)

    assert {item.strategy_track for item in result.trend_candidates} == {
        StrategyTrack.TREND_CONTINUATION
    }
    assert {item.strategy_track for item in result.reversal_candidates} == {
        StrategyTrack.BOTTOM_REVERSAL
    }
    assert result.created_count == 2


def test_strategy_run_binder_precedes_event_identity_lookup_and_registration(
    scanner_parts,
):
    at = scanner_parts["at"]
    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )
    bound_event_ids: list[str] = []

    def bind_strategy_run(event):
        bound = bind_strategy_run_provenance(
            event,
            strategy_run_id="paper-run-" + "a" * 64,
            strategy_run_epoch=7,
            strategy_run_fingerprint="sha256:" + "b" * 64,
        )
        bound_event_ids.append(bound.event_id)
        assert scanner_parts["service"].store.count_events(bound.event_id) == 0
        return bound

    scanner = _scanner(
        scanner_parts,
        event_strategy_run_binder=bind_strategy_run,
    )

    result = scanner.scan_closed_bar(at)

    candidate = result.trend_candidates[0]
    assert bound_event_ids == [candidate.event.event_id]
    assert candidate.event.strategy_run_id == "paper-run-" + "a" * 64
    assert candidate.event.strategy_run_epoch == 7
    assert candidate.event.strategy_run_fingerprint == "sha256:" + "b" * 64
    assert scanner_parts["service"].get(candidate.event.event_id).event == (
        candidate.event
    )


def test_stage_one_exclusion_skips_structure_computation(scanner_parts):
    at = scanner_parts["at"]
    scanner_parts["securities"][0] = _security(
        "SH.600001",
        at,
        name="*ST Example",
    )
    scanner = _scanner(scanner_parts)

    result = scanner.scan_closed_bar(at)

    assert result.created_count == 0
    assert result.exclusions[0].reason == "st"
    assert scanner_parts["calls"] == []


def test_stale_market_snapshot_prevents_all_new_events(scanner_parts):
    at = scanner_parts["at"]
    stale_at = at - timedelta(minutes=6)

    def stale_universe_provider(closed_at):
        return UniverseSnapshot(
            stale_at,
            (_security("SH.600001", stale_at),),
        )

    scanner = _scanner(
        scanner_parts,
        universe_provider=stale_universe_provider,
    )

    result = scanner.scan_closed_bar(at)

    assert result.code == "stale_market_data"
    assert result.created_count == 0
    assert scanner_parts["calls"] == []


def test_corrupt_symbol_does_not_abort_other_symbols(scanner_parts):
    at = scanner_parts["at"]
    scanner_parts["securities"][:] = [
        _security("SH.600001", at),
        _security("SH.600002", at),
    ]
    scanner_parts["structures"]["SH.600001"] = ValueError("corrupt cache")
    scanner_parts["structures"]["SH.600002"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )
    scanner = _scanner(scanner_parts)

    result = scanner.scan_closed_bar(at)

    assert result.created_count == 1
    assert result.code == "partial_failure"
    assert [(item.code, item.reason) for item in result.failures] == [
        ("SH.600001", "ValueError")
    ]
    assert [item.event.code for item in result.trend_candidates] == [
        "SH.600002"
    ]

    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )
    retried = scanner.scan_closed_bar(at)
    duplicate = scanner.scan_closed_bar(at)

    assert retried.code == "ok"
    assert retried.created_count == 1
    assert retried.failures == ()
    assert duplicate.code == "duplicate_bar"


def test_processed_bar_identity_cache_is_bounded(scanner_parts):
    at = scanner_parts["at"]
    calls: list[object] = []

    def universe_provider(closed_at):
        return UniverseSnapshot(
            closed_at,
            (_security("SH.600001", closed_at),),
        )

    def structure_provider(security, closed_at):
        calls.append((security.code, closed_at))
        return _structure(closed_at)

    scanner = _scanner(
        scanner_parts,
        universe_provider=universe_provider,
        structure_provider=structure_provider,
        processed_bar_limit=2,
    )
    bars = tuple(at + timedelta(minutes=offset) for offset in (0, 5, 10))
    for bar in bars:
        assert scanner.scan_closed_bar(bar).code == "ok"

    retried_oldest = scanner.scan_closed_bar(bars[0])

    assert retried_oldest.code == "ok"
    assert len(calls) == 4


def test_scanner_requires_aligned_complete_five_minute_bar(scanner_parts):
    scanner = _scanner(scanner_parts)

    with pytest.raises(ValueError, match="closed 5-minute bar"):
        scanner.scan_closed_bar(
            scanner_parts["at"] + timedelta(minutes=1)
        )


def test_limit_up_candidate_is_persisted_but_risk_cannot_enter_review(
    scanner_parts,
):
    at = scanner_parts["at"]
    scanner_parts["securities"][0] = _security(
        "SH.600001",
        at,
        limit_up_locked=True,
    )
    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )
    scanner = _scanner(scanner_parts)

    result = scanner.scan_closed_bar(at)

    event_id = result.trend_candidates[0].event.event_id
    assert result.created_count == 1
    assert scanner_parts["service"].get(event_id).state is EventState.RISK_CHECKED


def test_scanner_persists_bound_event_and_exposes_rule_evaluation(
    scanner_parts,
):
    at = scanner_parts["at"]
    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )

    result = _scanner(scanner_parts).scan_closed_bar(at)

    candidate = result.trend_candidates[0]
    event = candidate.event
    evaluation = candidate.rule_evaluation
    facts = scanner_parts["rule_engine"].calls[0][1]
    assert result.failures == ()
    assert event.rule_binding_status == "bound"
    assert evaluation is not None
    assert event.rule_id == evaluation.rule_id
    assert scanner_parts["service"].get(event.event_id).event == event
    assert facts.fundamental_ok is True
    assert facts.comparison_ok is True
    assert facts.market_liquid is True
    assert facts.risk_allowed is None
    assert facts.latest_price == 10.0
    assert facts.level_facts[0].frequency == "5m"
    assert facts.level_facts[0].completed_bar_count == 1
    assert facts.level_facts[0].latest_bar_closed is True


def test_scanner_does_not_publish_or_persist_rule_reject(scanner_parts):
    at = scanner_parts["at"]
    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )
    rule_engine = _RuleEngine(
        scanner_parts["make_rule_evaluation"],
        verdict=EvaluationVerdict.REJECT,
        safe_to_proceed=False,
    )

    result = _scanner(
        scanner_parts,
        rule_engine=rule_engine,
    ).scan_closed_bar(at)

    assert result.created_count == 0
    assert result.trend_candidates == ()
    assert result.failures == ()
    assert scanner_parts["service"].store.list_events() == ()


def test_scanner_persists_watch_without_entering_review(scanner_parts):
    at = scanner_parts["at"]
    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )
    rule_engine = _RuleEngine(
        scanner_parts["make_rule_evaluation"],
        verdict=EvaluationVerdict.WATCH,
        safe_to_proceed=False,
    )

    result = _scanner(
        scanner_parts,
        rule_engine=rule_engine,
    ).scan_closed_bar(at)

    candidate = result.trend_candidates[0]
    assert result.created_count == 1
    assert candidate.rule_evaluation.verdict is EvaluationVerdict.WATCH
    assert candidate.accepted is False
    assert candidate.observation is True
    assert (
        scanner_parts["service"].get(candidate.event.event_id).state
        is EventState.RISK_CHECKED
    )


def test_scanner_persists_watch_candidate_for_manual_chart_check(
    scanner_parts,
) -> None:
    at = scanner_parts["at"]
    scanner_parts["structures"]["SH.600001"] = _structure(
        at,
        Signal(at, 1, "3buy", 10.0, structural_stop_below=9.0),
    )
    rule_engine = _RuleEngine(
        scanner_parts["make_rule_evaluation"],
        verdict=EvaluationVerdict.WATCH,
        safe_to_proceed=False,
    )
    workflow = _ManualCheckWorkflow()

    result = _scanner(
        scanner_parts,
        rule_engine=rule_engine,
        manual_check_workflow=workflow,
    ).scan_closed_bar(at)

    candidate = result.trend_candidates[0]
    assert len(workflow.calls) == 1
    event, facts, evaluation = workflow.calls[0]
    assert event == candidate.event
    assert evaluation == candidate.rule_evaluation
    assert facts.manual_checks == ()
    assert facts.level_facts[0].completed_bar_count == 1
    assert (
        scanner_parts["service"].get(event.event_id).state
        is EventState.RISK_CHECKED
    )
