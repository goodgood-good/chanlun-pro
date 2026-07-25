from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import build_event_id, sha256_json
from chanlun.decision_support.event_factory import bind_rule_evaluation
from chanlun.decision_support.models import (
    DecisionEvent,
    LevelSnapshot,
    MarketConstraints,
    SignalSnapshot,
    StrategyTrack,
)
from chanlun.decision_support.risk import QuoteSnapshot, RiskContext
from chanlun.decision_support.rule_cards import EvaluationVerdict, RuleEvaluation


def ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


@pytest.fixture
def make_decision_event():
    def factory(
        *,
        price: float = 10.0,
        level: int = 1,
        stop_below: float | None = 9.0,
        bs_type: str = "3buy",
        big_dir: str = "neutral",
        mid_dir: str = "up",
        live_divergence: bool = False,
        divergence_kind: str | None = None,
        confirmation_bs_type: str | None = None,
        track: StrategyTrack = StrategyTrack.CHANLUN_SOURCE_FAITHFUL,
        market: str = "a",
        code: str = "SH.600519",
        name: str = "贵州茅台",
        board: str = "main",
        limit_pct: float | None = 0.10,
        observed_at: datetime | None = None,
        signal_at: datetime | None = None,
        quote_time: datetime | None = None,
        bar_closed_at: datetime | None = None,
    ) -> DecisionEvent:
        observed_at = observed_at or ts("2026-07-13T10:35:00+08:00")
        bar_closed_at = bar_closed_at or observed_at
        signal = SignalSnapshot(
            bs_type=bs_type,
            signal_at=signal_at or observed_at,
            level=level,
            price=price,
            first_visible_bar=21,
            structural_stop_below=stop_below,
            structural_stop_above=None,
            zs_zd=9.2,
            zs_zg=9.8,
            nest_operable=True,
            nest_depth=2,
            divergence_kind=(
                "qs"
                if live_divergence and divergence_kind is None
                else divergence_kind
            ),
            live_divergence=live_divergence,
            confirmation_bs_type=confirmation_bs_type,
        )
        constraints = MarketConstraints(
            board=board,
            lot=100,
            t_plus=1,
            limit_pct=limit_pct,
            entry_tradable=True,
            exit_tradable=True,
            quote_time=quote_time or observed_at,
        )
        levels = (
            LevelSnapshot("30m", 2, big_dir, True, 8.0, 10.0, 8.8, 9.5),
            LevelSnapshot("5m", level, mid_dir, True, 9.0, 10.0, 9.2, 9.8),
        )
        return DecisionEvent(
            event_id=build_event_id(
                market,
                code,
                "5m",
                observed_at,
                level,
                signal.bs_type,
                sha256_json(signal),
            ),
            market=market,
            code=code,
            name=name,
            observed_at=observed_at,
            bar_closed_at=bar_closed_at,
            strategy_track=track,
            signal=signal,
            levels=levels,
            market_constraints=constraints,
            data_fingerprint=sha256_json({"fixture": "data"}),
            config_fingerprint=sha256_json({"fixture": "config"}),
        )

    return factory


@pytest.fixture
def make_rule_evaluation():
    def factory(
        event: DecisionEvent,
        *,
        verdict: EvaluationVerdict = EvaluationVerdict.CONFIRM,
        safe_to_proceed: bool = True,
    ) -> RuleEvaluation:
        return RuleEvaluation(
            rule_id="chanlun.third_buy",
            rule_card_version=1,
            rule_card_fingerprint="sha256:" + "1" * 64,
            rule_set_fingerprint="sha256:" + "2" * 64,
            corpus_manifest_fingerprint="sha256:" + "3" * 64,
            algorithm_fingerprint="sha256:" + "4" * 64,
            evaluation_input_fingerprint=event.data_fingerprint,
            strategy_track=event.strategy_track,
            level=event.signal.level,
            verdict=verdict,
            candidate_satisfied=True,
            confirmation_satisfied=safe_to_proceed,
            invalidation_triggered=False,
            conflict_triggered=False,
            critical_indeterminate=False,
            safe_to_proceed=safe_to_proceed,
            reasons=(),
            evidence_ids=("lesson-20-main", "lesson-20-counter"),
            supporting_evidence_ids=("lesson-20-main",),
            counterevidence_ids=("lesson-20-counter",),
        )

    return factory


@pytest.fixture
def make_bound_decision_event(make_decision_event, make_rule_evaluation):
    def factory(**kwargs) -> DecisionEvent:
        event = make_decision_event(**kwargs)
        return bind_rule_evaluation(event, make_rule_evaluation(event))

    return factory

@pytest.fixture
def make_risk_context():
    def factory(
        *,
        account_equity: str = "100000",
        day_start_equity: str | None = None,
        available_cash: str = "100000",
        holdings: tuple = (),
        pending_exits: tuple = (),
        day_pnl: str = "0",
        strategy_drawdown: str = "0",
        daily_loss_locked: bool = False,
        drawdown_locked: bool = False,
        quote_code: str = "SH.600519",
        entry_reference: str = "10",
        quote_time: datetime | None = None,
        entry_tradable: bool = True,
        exit_tradable: bool = True,
        limit_up_locked: bool = False,
        limit_down_locked: bool = False,
        asof: datetime | None = None,
    ) -> RiskContext:
        equity = Decimal(account_equity)
        evaluated_at = asof or ts("2026-07-13T10:35:00+08:00")
        return RiskContext(
            account_equity=equity,
            day_start_equity=(
                Decimal(day_start_equity)
                if day_start_equity is not None
                else equity
            ),
            available_cash=Decimal(available_cash),
            holdings=holdings,
            pending_exits=pending_exits,
            day_pnl=Decimal(day_pnl),
            strategy_drawdown=Decimal(strategy_drawdown),
            daily_loss_locked=daily_loss_locked,
            drawdown_locked=drawdown_locked,
            quote=QuoteSnapshot(
                code=quote_code,
                price=Decimal(entry_reference),
                quote_time=quote_time or evaluated_at,
                entry_tradable=entry_tradable,
                exit_tradable=exit_tradable,
                limit_up_locked=limit_up_locked,
                limit_down_locked=limit_down_locked,
            ),
            asof=evaluated_at,
        )

    return factory
