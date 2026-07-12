from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionReview,
    TableByDecisionTransition,
    TableByRiskSnapshot,
)
from chanlun.decision_support.event_factory import bind_rule_evaluation
from chanlun.decision_support.event_service import DecisionEventService
from chanlun.decision_support.event_store import DecisionEventStore
from chanlun.decision_support.models import EventState
from chanlun.decision_support.rule_cards import (
    EvaluationVerdict,
    RuleEvaluation,
)
from chanlun.decision_support.replay import (
    ReplayBar,
    ReplayFeed,
    ReplayInput,
    compare_event_streams,
    replay_symbol,
)
from chanlun.decision_support.risk import QuoteSnapshot, RiskContext
from chanlun.decision_support.scanner import (
    DecisionScanner,
    InvalidationNotice,
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

    def get_recursive_branch_levels(self):
        return (
            _Level("30m", 2, "neutral", True, 8.0, 10.0, 8.8, 9.5),
            _Level("5m", 1, "up", True, 9.0, 10.0, 9.2, 9.8),
        )


class _Clock:
    def count_closed_bars(self, event, asof) -> int:
        return 0


class _RuleEngine:
    def evaluate(self, event, runtime_facts):
        evaluation = RuleEvaluation(
            rule_id="chanlun.third_buy",
            rule_card_version=1,
            rule_card_fingerprint="sha256:" + "1" * 64,
            rule_set_fingerprint="sha256:" + "2" * 64,
            corpus_manifest_fingerprint="sha256:" + "3" * 64,
            algorithm_fingerprint="sha256:" + "4" * 64,
            evaluation_input_fingerprint="sha256:" + "5" * 64,
            strategy_track=event.strategy_track,
            level=event.signal.level,
            verdict=EvaluationVerdict.CONFIRM,
            candidate_satisfied=True,
            confirmation_satisfied=True,
            invalidation_triggered=False,
            conflict_triggered=False,
            critical_indeterminate=False,
            safe_to_proceed=True,
            reasons=(),
            evidence_ids=("lesson-20-main", "lesson-20-counter"),
            supporting_evidence_ids=("lesson-20-main",),
            counterevidence_ids=("lesson-20-counter",),
        )
        return bind_rule_evaluation(event, evaluation), evaluation


def _bars(count: int = 22) -> tuple[ReplayBar, ...]:
    start = ts("2026-07-13T09:05:00+08:00")
    values = []
    for index in range(count):
        closed_at = start + timedelta(minutes=5 * index)
        values.append(
            ReplayBar(
                frequency="5m",
                closed_at=closed_at,
                available_at=closed_at,
                payload={
                    "time": closed_at,
                    "open": 9.8,
                    "high": 10.1,
                    "low": 9.7,
                    "close": 10.0,
                    "volume": 1_000_000,
                },
            )
        )
    values.append(
        ReplayBar(
            frequency="30m",
            closed_at=ts("2026-07-13T10:30:00+08:00"),
            available_at=ts("2026-07-13T10:30:00+08:00"),
            payload={"time": ts("2026-07-13T10:30:00+08:00"), "close": 10.0},
        )
    )
    return tuple(values)


def _risk_context(security, event, closed_at) -> RiskContext:
    return RiskContext(
        account_equity=Decimal("100000"),
        day_start_equity=Decimal("100000"),
        available_cash=Decimal("100000"),
        holdings=(),
        pending_exits=(),
        day_pnl=Decimal("0"),
        strategy_drawdown=Decimal("0"),
        daily_loss_locked=False,
        drawdown_locked=False,
        quote=QuoteSnapshot(
            code=security.code,
            price=Decimal(str(event.signal.price)),
            quote_time=closed_at,
            entry_tradable=security.entry_tradable,
            exit_tradable=security.exit_tradable,
            limit_up_locked=bool(security.security.limit_up_locked),
            limit_down_locked=bool(security.security.limit_down_locked),
        ),
        asof=closed_at,
    )


def _replay_input(*, invalidate_on_bar: int | None = None) -> ReplayInput:
    def scanner_factory(feed: ReplayFeed) -> DecisionScanner:
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

        def universe_provider(closed_at):
            security = SecuritySnapshot(
                market="a",
                code="SH.600001",
                name="Example",
                listed_days=1000,
                suspended=False,
                delisting=False,
                avg_turnover_20d=200_000_000.0,
                quote_time=closed_at,
                limit_up_locked=False,
                limit_down_locked=False,
            )
            return UniverseSnapshot(closed_at, (security,))

        def structure_provider(security, closed_at):
            operation_bars = feed.bars("5m")
            index = len(operation_bars)
            signals = ()
            invalidations = ()
            if index == 21:
                signals = (
                    Signal(
                        closed_at,
                        1,
                        "3buy",
                        10.0,
                        structural_stop_below=9.0,
                    ),
                )
            if invalidate_on_bar == index:
                events = service.store.list_events(code=security.code)
                if events:
                    invalidations = (
                        InvalidationNotice(
                            events[0].event_id,
                            "structure_repainted",
                        ),
                    )
            return SymbolStructureSnapshot(
                frequency="5m",
                cd=_CL(),
                signals=signals,
                first_visible_bar=index,
                completed_bars=operation_bars,
                config={"recursive_l0_min_zs_lines": 5},
                operation_bar_closed=True,
                fund_ok=True,
                comparison_ok=True,
                invalidations=invalidations,
            )

        return DecisionScanner(
            universe_provider=universe_provider,
            structure_provider=structure_provider,
            risk_context_provider=_risk_context,
            event_service=service,
            rule_engine=_RuleEngine(),
        )

    return ReplayInput(
        bars=_bars(),
        scanner_factory=scanner_factory,
        operation_frequency="5m",
    )


def test_replay_does_not_emit_event_before_first_visible_bar():
    replay_input = _replay_input()

    before = replay_symbol(replay_input, until_bar=20)
    visible = replay_symbol(replay_input, until_bar=21)

    assert before.events == ()
    assert len(visible.events) == 1
    assert visible.events[0].signal.first_visible_bar == 21


def test_replay_feeds_higher_frequency_state_only_when_available():
    replay_input = _replay_input()
    feed = ReplayFeed()
    seen = []
    scanner = replay_input.scanner_factory(feed)
    original_provider = scanner._structure_provider

    def recording_provider(security, closed_at):
        seen.append((closed_at, len(feed.bars("30m"))))
        return original_provider(security, closed_at)

    scanner._structure_provider = recording_provider
    for bar in sorted(
        replay_input.bars,
        key=lambda item: (item.available_at, item.frequency),
    ):
        feed.append(bar)
        if bar.frequency == "5m":
            scanner.scan_closed_bar(bar.closed_at)

    before_1030 = [count for at, count in seen if at < ts("2026-07-13T10:30:00+08:00")]
    at_or_after_1030 = [count for at, count in seen if at >= ts("2026-07-13T10:30:00+08:00")]
    assert set(before_1030) == {0}
    assert set(at_or_after_1030) == {1}


def test_replay_and_sequential_live_scans_have_byte_parity():
    replay_input = _replay_input()
    replayed = replay_symbol(replay_input)
    feed = ReplayFeed()
    scanner = replay_input.scanner_factory(feed)
    for bar in sorted(
        replay_input.bars,
        key=lambda item: (item.available_at, item.frequency),
    ):
        feed.append(bar)
        if bar.frequency == replay_input.operation_frequency:
            scanner.scan_closed_bar(bar.closed_at)
    live_events = scanner.event_service.store.list_events()

    comparison = compare_event_streams(replayed.events, live_events)

    assert comparison.matches is True
    assert comparison.missing_event_ids == ()
    assert comparison.unexpected_event_ids == ()
    assert comparison.differing_event_ids == ()


def test_repaint_appends_invalidation_without_moving_original_event():
    replay_input = _replay_input(invalidate_on_bar=22)

    result = replay_symbol(replay_input)

    assert len(result.events) == 1
    event = result.events[0]
    view = result.views[0]
    assert event.signal.first_visible_bar == 21
    assert view.event.observed_at == event.observed_at
    assert view.state is EventState.INVALIDATED
    assert view.transitions[-1].reason == "structure_repainted"


def test_compare_event_streams_reports_payload_difference():
    replay_input = _replay_input()
    expected = replay_symbol(replay_input, until_bar=21).events
    changed = replay_symbol(_replay_input(), until_bar=21).events[0]
    object.__setattr__(changed.signal, "price", 10.1)

    comparison = compare_event_streams(expected, (changed,))

    assert comparison.matches is False
    assert comparison.differing_event_ids == (expected[0].event_id,)
