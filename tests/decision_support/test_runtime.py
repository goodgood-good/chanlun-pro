from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import sqlite3
from threading import Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.runtime import (
    LiveDecisionDataProvider,
    LiveUniverseDefinition,
    PaperRiskAccountProvider,
    PaperRiskAuthorityError,
    RiskAccountSnapshot,
    build_decision_support_runtime,
    live_data_provider_from_dynamic_monitor,
    make_risk_context_provider,
)
from chanlun.decision_support.event_service import DecisionEventService
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.monitor import DecisionSupportRuntime
from chanlun.decision_support.mutation_fence import MutationFenceError
from chanlun.decision_support.paper_adapter import PaperBar, PaperLedgerState
from chanlun.decision_support.paper_admission import (
    PaperAccountSnapshot,
    SQLitePaperLedger,
)
from chanlun.decision_support.paper_runtime import (
    ExplicitPaperTradingCalendar,
    SQLitePaperRiskState,
    SQLiteTrustedPaperBarStore,
)
from chanlun.decision_support.risk import QuoteSnapshot, RiskPolicy
from chanlun.decision_support.scanner import DecisionScanner
from chanlun.decision_support.strategy_run import (
    StrategyRunIdentity,
    establish_strategy_run,
)
from chanlun.decision_support.universe import filter_universe, UniversePolicy
from chanlun.recursive_bt.engine.engine import Signal


CN = ZoneInfo("Asia/Shanghai")


class _PaperRiskLedger:
    def __init__(self, state: object, account: PaperAccountSnapshot) -> None:
        self.state = state
        self.account = account

    def load(self):
        return self.state

    def account_snapshot(self) -> PaperAccountSnapshot:
        return self.account


class _PaperRiskQuotes:
    def __init__(self, prices: dict[str, Decimal]) -> None:
        self.prices = prices

    def quote_for_code(self, code: str, closed_at: datetime) -> QuoteSnapshot:
        return QuoteSnapshot(
            code=code,
            price=self.prices[code],
            quote_time=closed_at,
            entry_tradable=True,
            exit_tradable=True,
            limit_up_locked=False,
            limit_down_locked=False,
        )

    def risk_quote(self, security, closed_at: datetime) -> QuoteSnapshot:
        return self.quote_for_code(security.code, closed_at)

    def paper_bar(self, code: str, closed_at: datetime) -> PaperBar:
        price = self.prices[code]
        return PaperBar(
            code=code,
            opened_at=closed_at - timedelta(minutes=5),
            closed_at=closed_at,
            open_price=price,
            close_price=price,
            previous_close=price,
        )


def _strategy_run_identity() -> StrategyRunIdentity:
    fingerprints = {
        field_name: "sha256:" + character * 64
        for field_name, character in zip(
            (
                "rule_set_fingerprint",
                "corpus_manifest_fingerprint",
                "source_pdf_fingerprint",
                "rule_algorithm_fingerprint",
                "strategy_engine_build_fingerprint",
                "scanner_algorithm_fingerprint",
                "structure_algorithm_fingerprint",
                "universe_policy_fingerprint",
                "monitor_policy_fingerprint",
                "review_schema_fingerprint",
                "review_runtime_policy_fingerprint",
                "execution_policy_fingerprint",
                "fee_schedule_fingerprint",
                "account_algorithm_fingerprint",
                "risk_policy_fingerprint",
                "exit_policy_fingerprint",
                "exit_algorithm_fingerprint",
                "calendar_fingerprint",
                "bar_provider_fingerprint",
                "bar_schema_fingerprint",
            ),
            "123456789abcdef01234",
            strict=True,
        )
    }
    return StrategyRunIdentity(
        **fingerprints,
        review_provider="fixture-provider",
        review_model="fixture-model",
        review_prompt_version="fixture-v1",
        initial_cash=Decimal("100000"),
    )


def _established_paper_risk_domain(tmp_path):
    paths = {
        "ledger": tmp_path / "bound-ledger.sqlite3",
        "bar": tmp_path / "bound-bars.sqlite3",
        "risk": tmp_path / "bound-risk.sqlite3",
        "exit": tmp_path / "bound-exits.sqlite3",
    }
    ledger = SQLitePaperLedger(
        paths["ledger"],
        initial_cash=Decimal("100000"),
    )
    risk_state = SQLitePaperRiskState(
        paths["risk"],
        policy=RiskPolicy.conservative(),
    )
    for role in ("bar", "exit"):
        with sqlite3.connect(paths[role]):
            pass
    strategy_run = establish_strategy_run(
        tmp_path / "strategy-run.sqlite3",
        requested_epoch=1,
        identity=_strategy_run_identity(),
        store_paths=paths,
        now=datetime(2026, 7, 15, 9, 0, tzinfo=CN),
    )
    return ledger, risk_state, strategy_run


def _paper_risk_provider(*, ledger, risk_state) -> PaperRiskAccountProvider:
    return PaperRiskAccountProvider(
        data_provider=_PaperRiskQuotes({"SH.600001": Decimal("10")}),
        ledger=ledger,
        risk_state=risk_state,
    )


@pytest.mark.parametrize("mismatched_role", ("risk", "ledger"))
def test_paper_risk_provider_bind_rejects_dependency_path_outside_strategy_run(
    tmp_path,
    mismatched_role,
) -> None:
    ledger, risk_state, strategy_run = _established_paper_risk_domain(tmp_path)
    if mismatched_role == "ledger":
        ledger = SQLitePaperLedger(
            tmp_path / "wrong-ledger.sqlite3",
            initial_cash=Decimal("100000"),
        )
    else:
        risk_state = SQLitePaperRiskState(
            tmp_path / "wrong-risk.sqlite3",
            policy=RiskPolicy.conservative(),
        )
    provider = _paper_risk_provider(ledger=ledger, risk_state=risk_state)

    with pytest.raises(
        MutationFenceError,
        match="mutation_fence_store_path_mismatch",
    ):
        provider.bind_strategy_run(strategy_run)

    assert provider._mutation_fence._active is None


@pytest.mark.parametrize("mismatched_role", ("risk", "ledger"))
def test_paper_risk_provider_bind_rejects_physical_store_instance_mismatch(
    tmp_path,
    mismatched_role,
) -> None:
    ledger, risk_state, strategy_run = _established_paper_risk_domain(tmp_path)
    forged_bindings = dict(strategy_run.store_bindings)
    forged_bindings[mismatched_role] = replace(
        forged_bindings[mismatched_role],
        store_instance_id=(
            forged_bindings[mismatched_role].store_instance_id + ":forged"
        ),
    )
    forged_run = replace(strategy_run, store_bindings=forged_bindings)
    provider = _paper_risk_provider(ledger=ledger, risk_state=risk_state)

    with pytest.raises(
        MutationFenceError,
        match="mutation_fence_store_instance_mismatch",
    ):
        provider.bind_strategy_run(forged_run)

    assert provider._mutation_fence._active is None


def test_paper_risk_provider_bind_accepts_exact_physical_store_domain(
    tmp_path,
) -> None:
    ledger, risk_state, strategy_run = _established_paper_risk_domain(tmp_path)
    provider = _paper_risk_provider(ledger=ledger, risk_state=risk_state)

    provider.bind_strategy_run(strategy_run)
    provider.bind_strategy_run(strategy_run)

    assert provider._mutation_fence._active is strategy_run
    assert provider._strategy_run_binding == (
        strategy_run.run_id,
        strategy_run.epoch,
        strategy_run.strategy_run_fingerprint,
    )


def test_paper_risk_provider_derives_and_persists_one_account_domain(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    state = SimpleNamespace(
        revision=7,
        lots=(
            SimpleNamespace(
                code="SH.600001",
                shares=100,
                price=Decimal("8"),
                opened_at=closed_at - timedelta(days=1),
            ),
        ),
        intents=(
            SimpleNamespace(
                code="SH.600001",
                side="sell",
                remaining_shares=100,
                status="pending_limit_down",
                reason="exit_blocked_by_limit_down",
            ),
        ),
    )
    ledger = _PaperRiskLedger(
        state,
        PaperAccountSnapshot(
            initial_cash=Decimal("1000"),
            cash_balance=Decimal("500"),
            reserved_buying_power=Decimal("100"),
            available_buying_power=Decimal("400"),
            positions_cost=Decimal("800"),
            cost_basis_equity=Decimal("1300"),
        ),
    )
    risk_state = SQLitePaperRiskState(
        tmp_path / "paper-risk.sqlite3",
        policy=RiskPolicy.conservative(),
    )
    provider = PaperRiskAccountProvider(
        data_provider=_PaperRiskQuotes(
            {"SH.600001": Decimal("10"), "SH.600002": Decimal("20")}
        ),
        ledger=ledger,
        risk_state=risk_state,
    )

    context = provider(
        SimpleNamespace(code="SH.600002"),
        SimpleNamespace(event_id="event-paper-domain", code="SH.600002"),
        closed_at,
    )
    binding = provider.binding_for("event-paper-domain")

    assert context.account_equity == Decimal("1500")
    assert context.available_cash == Decimal("400")
    assert context.holdings[0].code == "SH.600001"
    assert context.holdings[0].average_price == Decimal("8")
    assert context.pending_exits[0].blocked_by_limit is True
    assert binding.ledger_revision == 7
    assert binding.risk_state_revision == 1
    assert binding.account_equity == Decimal("1500")
    assert binding.signal_bar_id == provider._data_provider.paper_bar(
        "SH.600002",
        closed_at,
    ).bar_id
    assert binding.valuation_fingerprint.startswith("sha256:")
    with provider.admission_guard(
        event_id="event-paper-domain",
        evaluated_at=closed_at,
        ledger_revision=7,
        daily_loss_locked=False,
        drawdown_locked=False,
        signal_bar_id=binding.signal_bar_id,
    ) as validate_inside_ledger:
        validate_inside_ledger(7)


def test_paper_risk_authority_rejects_ledger_revision_race(tmp_path) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    ledger = _PaperRiskLedger(
        PaperLedgerState(revision=3),
        PaperAccountSnapshot(
            initial_cash=Decimal("1000"),
            cash_balance=Decimal("1000"),
            reserved_buying_power=Decimal("0"),
            available_buying_power=Decimal("1000"),
            positions_cost=Decimal("0"),
            cost_basis_equity=Decimal("1000"),
        ),
    )
    provider = PaperRiskAccountProvider(
        data_provider=_PaperRiskQuotes({"SH.600001": Decimal("10")}),
        ledger=ledger,
        risk_state=SQLitePaperRiskState(
            tmp_path / "paper-risk.sqlite3",
            policy=RiskPolicy.conservative(),
        ),
    )
    provider(
        SimpleNamespace(code="SH.600001"),
        SimpleNamespace(event_id="event-ledger-race", code="SH.600001"),
        closed_at,
    )

    ledger.state = PaperLedgerState(revision=4)

    with pytest.raises(
        PaperRiskAuthorityError,
        match="paper_risk_ledger_revision_changed",
    ):
        with provider.admission_guard(
            event_id="event-ledger-race",
            evaluated_at=closed_at,
            ledger_revision=ledger.load().revision,
            daily_loss_locked=False,
            drawdown_locked=False,
            signal_bar_id=provider.binding_for("event-ledger-race").signal_bar_id,
        ):
            pass


def test_paper_risk_authority_rejects_latch_revision_race(tmp_path) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    ledger = _PaperRiskLedger(
        PaperLedgerState(revision=0),
        PaperAccountSnapshot(
            initial_cash=Decimal("1000"),
            cash_balance=Decimal("1000"),
            reserved_buying_power=Decimal("0"),
            available_buying_power=Decimal("1000"),
            positions_cost=Decimal("0"),
            cost_basis_equity=Decimal("1000"),
        ),
    )
    risk_state = SQLitePaperRiskState(
        tmp_path / "paper-risk.sqlite3",
        policy=RiskPolicy.conservative(),
    )
    provider = PaperRiskAccountProvider(
        data_provider=_PaperRiskQuotes({"SH.600001": Decimal("10")}),
        ledger=ledger,
        risk_state=risk_state,
    )
    provider(
        SimpleNamespace(code="SH.600001"),
        SimpleNamespace(event_id="event-latch-race", code="SH.600001"),
        closed_at,
    )
    risk_state.mark(Decimal("900"), closed_at + timedelta(minutes=5))

    with pytest.raises(
        PaperRiskAuthorityError,
        match="paper_risk_state_changed",
    ):
        with provider.admission_guard(
            event_id="event-latch-race",
            evaluated_at=closed_at,
            ledger_revision=0,
            daily_loss_locked=False,
            drawdown_locked=False,
            signal_bar_id=provider.binding_for("event-latch-race").signal_bar_id,
        ):
            pass


def test_next_bar_without_latch_requires_fresh_event_risk_binding(tmp_path) -> None:
    first_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    next_at = first_at + timedelta(minutes=5)
    ledger = _PaperRiskLedger(
        PaperLedgerState(revision=0),
        PaperAccountSnapshot(
            initial_cash=Decimal("1000"),
            cash_balance=Decimal("1000"),
            reserved_buying_power=Decimal("0"),
            available_buying_power=Decimal("1000"),
            positions_cost=Decimal("0"),
            cost_basis_equity=Decimal("1000"),
        ),
    )
    provider = PaperRiskAccountProvider(
        data_provider=_PaperRiskQuotes({"SH.600001": Decimal("10")}),
        ledger=ledger,
        risk_state=SQLitePaperRiskState(
            tmp_path / "paper-risk.sqlite3",
            policy=RiskPolicy.conservative(),
        ),
    )
    first = SimpleNamespace(event_id="event-first-bar", code="SH.600001")
    second = SimpleNamespace(event_id="event-next-bar", code="SH.600001")
    provider(first, first, first_at)
    first_binding = provider.binding_for(first.event_id)
    provider(second, second, next_at)
    second_binding = provider.binding_for(second.event_id)

    assert second_binding.risk_state_revision > first_binding.risk_state_revision
    assert second_binding.daily_loss_locked is False
    assert second_binding.drawdown_locked is False
    with pytest.raises(PaperRiskAuthorityError, match="paper_risk_state_changed"):
        with provider.admission_guard(
            event_id=first.event_id,
            evaluated_at=first_at,
            ledger_revision=0,
            daily_loss_locked=False,
            drawdown_locked=False,
            signal_bar_id=first_binding.signal_bar_id,
        ):
            pass
    with provider.admission_guard(
        event_id=second.event_id,
        evaluated_at=next_at,
        ledger_revision=0,
        daily_loss_locked=False,
        drawdown_locked=False,
        signal_bar_id=second_binding.signal_bar_id,
    ) as validate_inside_ledger:
        validate_inside_ledger(0)


def test_paper_risk_lock_order_allows_concurrent_validation_and_scan(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    ledger = SQLitePaperLedger(
        tmp_path / "paper-ledger.sqlite3",
        initial_cash=Decimal("1000"),
    )
    provider = PaperRiskAccountProvider(
        data_provider=_PaperRiskQuotes({"SH.600001": Decimal("10")}),
        ledger=ledger,
        risk_state=SQLitePaperRiskState(
            tmp_path / "paper-risk.sqlite3",
            policy=RiskPolicy.conservative(),
        ),
    )
    first_event = SimpleNamespace(event_id="event-lock-a", code="SH.600001")
    provider(first_event, first_event, closed_at)
    binding = provider.binding_for(first_event.event_id)
    risk_entered = Event()
    release = Event()

    def validate_then_hold_ledger() -> None:
        with provider.admission_guard(
            event_id=first_event.event_id,
            evaluated_at=closed_at,
            ledger_revision=0,
            daily_loss_locked=False,
            drawdown_locked=False,
            signal_bar_id=binding.signal_bar_id,
        ) as validate_inside_ledger:
            with sqlite3.connect(ledger.path, timeout=2) as connection:
                connection.execute("BEGIN IMMEDIATE")
                revision = connection.execute(
                    "SELECT revision FROM paper_ledger WHERE singleton_id = 1"
                ).fetchone()[0]
                validate_inside_ledger(revision)
                risk_entered.set()
                assert release.wait(2)
                connection.rollback()

    def scan_same_domain() -> None:
        assert risk_entered.wait(2)
        second = SimpleNamespace(event_id="event-lock-b", code="SH.600001")
        provider(second, second, closed_at)

    with ThreadPoolExecutor(max_workers=2) as pool:
        validating = pool.submit(validate_then_hold_ledger)
        scanning = pool.submit(scan_same_domain)
        assert risk_entered.wait(2)
        release.set()
        validating.result(timeout=5)
        scanning.result(timeout=5)


@dataclass
class _Kline:
    index: int
    date: datetime
    o: float
    h: float
    l: float
    c: float
    a: float


@dataclass
class _Level:
    frequency: str = "5m"
    level: int = 1
    direction: str = "up"
    completed: bool = True
    segment_start: float = 9.0
    segment_end: float = 11.0
    zs_zd: float = 9.5
    zs_zg: float = 10.5
    mmds: tuple[str, ...] = ("3buy",)
    divergences: tuple[str, ...] = ()


class _CD:
    frequency = "5m"

    def __init__(self, bars: list[_Kline]) -> None:
        self.bars = bars
        self.config = {"recursive_l0_min_zs_lines": 5}
        self.levels = [_Level()]

    def get_src_klines(self):
        return list(self.bars)

    def get_config(self):
        return self.config

    def get_recursive_branch_levels(self):
        return list(self.levels)


class _State:
    op_level = "5m"

    def __init__(
        self,
        bars: list[_Kline],
        signals: tuple[Signal, ...],
        *,
        fail: bool = False,
    ) -> None:
        self.cd_op = _CD(bars)
        self._signals = signals
        self.fail = fail
        self.refresh_calls = 0
        self.last_op = bars[-1].date if bars else None
        self.last_px = bars[-1].c if bars else 0.0
        self.prev_close = 10.0

    def refresh(self):
        self.refresh_calls += 1
        if self.fail:
            raise RuntimeError("feed unavailable")
        return list(self._signals)


def _bars(at: datetime, *, close: float = 11.0, days: int = 60) -> list[_Kline]:
    values: list[_Kline] = []
    for index in range(days):
        day = at.date() - timedelta(days=days - index - 1)
        start = datetime(day.year, day.month, day.day, 10, 30)
        px = close if index == days - 1 else 10.0
        values.append(_Kline(index, start, px, px, px, px, 10_000_000.0))
    return values


def _definition(*, candidates=()):
    return LiveUniverseDefinition(
        market="a",
        codes=("SH.600001",),
        names={"SH.600001": "测试股份"},
        selection_candidates=tuple(candidates),
    )


def test_live_provider_freezes_one_closed_bar_snapshot_and_reuses_it():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    signal = Signal(
        datetime(2026, 7, 14, 10, 30),
        1,
        "3buy",
        11.0,
        structural_stop_below=9.5,
    )
    candidate = SimpleNamespace(
        code="SH.600001",
        bs_type="3buy",
        signal_time="2026-07-14 10:30:00",
        fund_ok=True,
        comparison_ok=True,
    )
    state = _State(_bars(at), (signal,))
    provider = LiveDecisionDataProvider(
        universe_resolver=lambda: _definition(candidates=(candidate,)),
        state_factory=lambda code: state,
    )

    first = provider.universe_provider(at)
    second = provider.universe_provider(at)
    eligible = filter_universe(
        first.securities,
        at,
        UniversePolicy.a_share_short_term(),
    ).included[0]
    structure = provider.structure_provider(eligible, at)

    security = first.securities[0]
    assert first is second
    assert state.refresh_calls == 1
    assert security.listed_days == 60
    assert security.avg_turnover_20d == pytest.approx(100_500_000.0)
    assert security.quote_time == at
    assert security.limit_up_locked is True
    assert eligible.entry_tradable is False
    assert structure.operation_bar_closed is True
    assert structure.first_visible_bar == 59
    assert structure.completed_bars[-1]["closed_at"] == at
    assert structure.signals[0].date == datetime(2026, 7, 14, 10, 30, tzinfo=CN)
    assert structure.fund_ok is True
    assert structure.comparison_ok is True
    assert structure.current_cycle_id is not None
    assert structure.current_cycle_id.startswith("sha256:")
    assert structure.signals_first_observed_at == {}
    assert structure.signal_observation_states == {}

    next_at = at + timedelta(minutes=5)
    state.cd_op.bars.append(
        _Kline(60, at, 11.0, 11.0, 11.0, 11.0, 10_000_000.0)
    )
    provider.universe_provider(next_at)
    next_structure = provider.structure_provider(
        SimpleNamespace(code="SH.600001"),
        next_at,
    )
    assert next_structure.current_cycle_id != structure.current_cycle_id
    assert next_structure.signals_first_observed_at == {}
    assert next_structure.signal_observation_states == {}

    state.cd_op.bars[-1].c = 999.0
    state.cd_op.levels[0].direction = "down"
    assert structure.completed_bars[-1]["close"] == 11.0
    assert structure.cd.get_recursive_branch_levels()[0].direction == "up"


def test_live_provider_builds_attested_paper_bar_from_frozen_source_payload():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    state = _State(_bars(at), ())
    state.cd_op.bars[-1] = _Kline(
        59,
        datetime(2026, 7, 14, 10, 30),
        10.25,
        11.5,
        10.0,
        11.0,
        19_999.0,
    )
    state.prev_close = 10.0
    provider = LiveDecisionDataProvider(
        universe_resolver=_definition,
        state_factory=lambda code: state,
        paper_participation_rate=Decimal("0.01"),
        max_cached_paper_bars=2,
    )

    provider.universe_provider(at)
    bar = provider.paper_bar("SH.600001", at)

    assert isinstance(bar, PaperBar)
    assert bar.opened_at == at - timedelta(minutes=5)
    assert bar.closed_at == at
    assert bar.open_price == Decimal("10.25")
    assert bar.close_price == Decimal("11.0")
    assert bar.previous_close == Decimal("10.0")
    assert bar.max_fill_shares == 100
    assert bar.limit_up_locked is True
    assert bar.limit_down_locked is False
    assert provider.get_bar(bar.bar_id) is bar

    state.cd_op.bars[-1].o = 99.0
    state.cd_op.bars[-1].c = 88.0
    state.prev_close = 99.0
    assert provider.paper_bar("SH.600001", at) is bar
    assert provider.get_bar(bar.bar_id) is bar


def test_live_provider_exposes_current_position_structure_and_quote_by_code():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    state = _State(_bars(at, close=11.0), ())
    provider = LiveDecisionDataProvider(
        universe_resolver=_definition,
        state_factory=lambda code: state,
    )

    provider.universe_provider(at)
    structure = provider.structure_for_code("SH.600001", at)
    quote = provider.quote_for_code("SH.600001", at)

    assert structure.completed_bars[-1]["closed_at"] == at
    assert quote.code == "SH.600001"
    assert quote.price == Decimal("11.0")
    assert quote.quote_time == at
    assert quote.entry_tradable is False
    assert quote.exit_tradable is True

    with pytest.raises(KeyError, match="current position structure"):
        provider.structure_for_code("SZ.000001", at)
    with pytest.raises(KeyError, match="current position quote"):
        provider.quote_for_code("SH.600001", at + timedelta(minutes=5))


def test_live_provider_rejects_paper_bar_without_trusted_previous_close():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    state = _State(_bars(at), ())
    state.prev_close = None
    provider = LiveDecisionDataProvider(
        universe_resolver=_definition,
        state_factory=lambda code: state,
        paper_participation_rate=Decimal("0.01"),
    )

    provider.universe_provider(at)

    assert provider.failures(at) == {"SH.600001": "ValueError"}
    with pytest.raises(KeyError, match="canonical paper bar"):
        provider.paper_bar("SH.600001", at)


def test_live_provider_evicts_old_paper_bar_payloads_at_cache_bound():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    state = _State(_bars(at), ())
    provider = LiveDecisionDataProvider(
        universe_resolver=_definition,
        state_factory=lambda code: state,
        paper_participation_rate=Decimal("0.01"),
        max_cached_paper_bars=1,
    )

    provider.universe_provider(at)
    first = provider.paper_bar("SH.600001", at)
    state.cd_op.bars.append(
        _Kline(60, at, 11.0, 11.0, 11.0, 11.0, 10_000.0)
    )
    provider.universe_provider(at + timedelta(minutes=5))
    second = provider.paper_bar("SH.600001", at + timedelta(minutes=5))

    assert first.bar_id != second.bar_id
    assert provider.get_bar(first.bar_id) is None
    assert provider.get_bar(second.bar_id) is second


def test_live_provider_fails_closed_for_untrusted_or_incomplete_market_data():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    failed = _State(_bars(at), (), fail=True)
    provider = LiveDecisionDataProvider(
        universe_resolver=lambda: LiveUniverseDefinition(
            market="a",
            codes=("SH.600001", "SZ.000001"),
            names={"SH.600001": "测试股份"},
        ),
        state_factory=lambda code: failed if code == "SH.600001" else _State(
            _bars(at, days=19),
            (),
        ),
    )

    snapshot = provider.universe_provider(at)
    result = filter_universe(
        snapshot.securities,
        at,
        UniversePolicy.a_share_short_term(),
    )

    assert result.included == ()
    assert {item.code: item.reason for item in result.excluded} == {
        "SH.600001": "missing_metadata",
        "SZ.000001": "missing_metadata",
    }
    assert provider.failures(at) == {"SH.600001": "RuntimeError"}


def test_live_provider_rejects_future_or_mismatched_snapshot_access():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    future_start = datetime(2026, 7, 14, 10, 35)
    bars = _bars(at)
    bars.append(_Kline(60, future_start, 11.0, 11.0, 11.0, 11.0, 1.0))
    state = _State(bars, ())
    provider = LiveDecisionDataProvider(
        universe_resolver=_definition,
        state_factory=lambda code: state,
    )

    snapshot = provider.universe_provider(at)
    result = filter_universe(
        snapshot.securities,
        at,
        UniversePolicy.a_share_short_term(),
    )

    assert result.included == ()
    assert result.excluded[0].reason == "missing_metadata"
    with pytest.raises(KeyError, match="canonical paper bar"):
        provider.paper_bar("SH.600001", at)
    with pytest.raises(KeyError, match="current eligible structure"):
        provider.structure_provider(
            SimpleNamespace(code="SH.600001"),
            at + timedelta(minutes=5),
        )


def test_runtime_composition_requires_explicit_risk_provider_and_has_no_broker():
    data_provider = SimpleNamespace(
        universe_provider=lambda at: None,
        structure_provider=lambda security, at: None,
    )
    event_service = SimpleNamespace()
    rule_engine = SimpleNamespace()

    with pytest.raises(TypeError, match="risk_context_provider"):
        build_decision_support_runtime(
            data_provider=data_provider,
            risk_context_provider=None,
            event_service=event_service,
            rule_engine=rule_engine,
            reviewer=lambda event_id: None,
        )

    assert "broker" not in build_decision_support_runtime.__code__.co_varnames
    assert "order" not in build_decision_support_runtime.__code__.co_varnames
    assert DecisionScanner is not None


def test_dynamic_monitor_adapter_uses_separate_analysis_states_only():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    state = _State(_bars(at), ())

    class _Exchange:
        def stock_info(self, code):
            return {"name": "测试股份"}

    class _Monitor:
        market = "a"

        def __init__(self):
            from threading import Lock

            self._lock = Lock()
            self.states = {"SH.600001": object()}
            self.last_selection_candidates = ()
            self.new_state_calls = 0
            self.forbidden_calls = 0

        def current_universe(self):
            return ["SH.600001"], {}

        def _exchange(self):
            return _Exchange()

        def _new_state(self, code, exchange):
            self.new_state_calls += 1
            return state

        def _broker(self):
            self.forbidden_calls += 1
            raise AssertionError("execution path must not be touched")

        def run_once(self):
            self.forbidden_calls += 1
            raise AssertionError("legacy notification path must not be touched")

    monitor = _Monitor()
    provider = live_data_provider_from_dynamic_monitor(monitor)

    snapshot = provider.universe_provider(at)

    assert snapshot.securities[0].name == "测试股份"
    assert monitor.new_state_calls == 1
    assert monitor.forbidden_calls == 0
    assert monitor.states["SH.600001"] is not state


def test_dynamic_monitor_adapter_pins_unbound_sell_observations_as_quarantined():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    signal = Signal(
        datetime(2026, 7, 14, 10, 30),
        1,
        "1sell",
        11.0,
    )
    states = {
        "SH.600001": _State(_bars(at), ()),
        "SZ.000001": _State(_bars(at), (signal,)),
    }

    class _Exchange:
        def stock_info(self, code):
            return {"name": {"SH.600001": "A", "SZ.000001": "B"}[code]}

    class _Monitor:
        market = "a"

        def __init__(self):
            from threading import Lock

            self._lock = Lock()
            self.base_codes = ["SH.600001"]
            self.last_selection_candidates = ()
            self.new_state_calls: list[str] = []

        def current_universe(self):
            return list(self.base_codes), {}

        def _exchange(self):
            return _Exchange()

        def _new_state(self, code, exchange):
            self.new_state_calls.append(code)
            return states[code]

    monitor = _Monitor()
    provider = live_data_provider_from_dynamic_monitor(
        monitor,
        pinned_codes_provider=lambda: ("SZ.000001",),
    )

    first_universe = provider.universe_provider(at)
    first = provider.structure_provider(
        SimpleNamespace(code="SZ.000001"),
        at,
    )
    signal_id = sha256_json(first.signals[0])

    monitor.base_codes = []
    states["SZ.000001"].cd_op.bars.append(
        _Kline(60, at, 11.0, 11.0, 11.0, 11.0, 10_000.0)
    )
    second_universe = provider.universe_provider(at + timedelta(minutes=5))
    second_required = provider.required_codes(at + timedelta(minutes=5))
    second = provider.structure_provider(
        SimpleNamespace(code="SZ.000001"),
        at + timedelta(minutes=5),
    )

    assert {item.code for item in first_universe.securities} == {
        "SH.600001",
        "SZ.000001",
    }
    assert tuple(item.code for item in second_universe.securities) == ("SZ.000001",)
    assert second_required == ("SZ.000001",)
    assert first.current_cycle_id != second.current_cycle_id
    assert first.signals_first_observed_at == {}
    assert first.signal_observation_states == {
        signal_id: "quarantined_unknown"
    }
    assert second.signals_first_observed_at == {}
    assert second.signal_observation_states == {
        signal_id: "quarantined_unknown"
    }
    assert monitor.new_state_calls.count("SZ.000001") == 1


def test_live_provider_persists_only_required_sell_signal_observations(
    tmp_path,
) -> None:
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    code = "SH.600001"
    sell = Signal(datetime(2026, 7, 14, 9, 30), 1, "1sell", 11.0)
    buy = Signal(datetime(2026, 7, 14, 9, 30), 1, "3buy", 11.0)
    state = _State(_bars(at), (sell, buy))
    definition = LiveUniverseDefinition(
        market="a",
        codes=(code,),
        names={code: "测试股份"},
        required_codes=(code,),
    )
    calendar = ExplicitPaperTradingCalendar(
        (date(2026, 7, 14),),
        source_id="runtime-signal-observation-fixture",
        source_fingerprint="sha256:" + "a" * 64,
    )
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "runtime-signal-observation.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    run_id = "run:runtime-signal-observation"
    strategy_fingerprint = "sha256:" + "b" * 64
    binding = SimpleNamespace(
        run_id=run_id,
        epoch=1,
        strategy_run_fingerprint=strategy_fingerprint,
        identity_sha256="sha256:" + "c" * 64,
        store_role="bar",
        store_instance_id="store:runtime-signal-observation",
    )
    strategy_run = SimpleNamespace(
        run_id=run_id,
        epoch=1,
        strategy_run_fingerprint=strategy_fingerprint,
        store_bindings={"bar": binding},
        store_paths={"bar": store.path},
        status_payload=lambda: {
            "run_id": run_id,
            "epoch": 1,
            "fingerprint": strategy_fingerprint,
            "state": "active",
            "evidence_scope": "current_epoch_only",
            "store_bindings_complete": True,
        },
    )
    provider = LiveDecisionDataProvider(
        universe_resolver=lambda: definition,
        state_factory=lambda _code: state,
    )
    provider.bind_signal_observation_store(store, strategy_run)

    provider.universe_provider(at)
    before = provider.structure_for_code(code, at)
    sell_fingerprint = sha256_json(
        next(signal for signal in before.signals if signal.bs_type == "1sell")
    )
    buy_fingerprint = sha256_json(
        next(signal for signal in before.signals if signal.bs_type == "3buy")
    )
    assert before.signals_first_observed_at == {}
    assert before.signal_observation_states == {
        sell_fingerprint: "quarantined_unknown"
    }
    store.record_cycle(
        session=calendar.session_for(at.date()),
        bar_closed_at=at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: provider.paper_bar(code, at)},
        optional_failures={},
    )

    prepared = provider.prepare_signal_observation_cycle(at)
    after = provider.structure_for_code(code, at)

    assert prepared.manifests == {code: (sell_fingerprint,)}
    assert buy_fingerprint not in prepared.manifests[code]
    assert after.signal_observation_states == {
        sell_fingerprint: "baseline_not_fresh"
    }
    assert after.signals_first_observed_at == {sell_fingerprint: at}
    assert provider.signal_observation_batch(at) is prepared
    store.complete_cycle(at, signal_observation_batch=prepared)


def test_dynamic_monitor_adapter_fails_closed_when_pinned_codes_are_unavailable():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)

    class _Exchange:
        def stock_info(self, code):
            return {"name": "A"}

    class _Monitor:
        market = "a"

        def __init__(self):
            from threading import Lock

            self._lock = Lock()
            self.last_selection_candidates = ()
            self.new_state_calls = 0

        def current_universe(self):
            return ["SH.600001"], {"SH.600001": "A"}

        def _exchange(self):
            return _Exchange()

        def _new_state(self, code, exchange):
            self.new_state_calls += 1
            return _State(_bars(at), ())

    monitor = _Monitor()

    def unavailable_pins():
        raise RuntimeError("paper positions unavailable")

    provider = live_data_provider_from_dynamic_monitor(
        monitor,
        pinned_codes_provider=unavailable_pins,
    )

    with pytest.raises(RuntimeError, match="paper positions unavailable"):
        provider.universe_provider(at)
    assert monitor.new_state_calls == 0


def test_runtime_composition_builds_scanner_and_disabled_monitor():
    service = object.__new__(DecisionEventService)
    data_provider = SimpleNamespace(
        universe_provider=lambda at: None,
        structure_provider=lambda security, at: None,
    )
    composition = build_decision_support_runtime(
        data_provider=data_provider,
        risk_context_provider=lambda security, event, at: None,
        event_service=service,
        rule_engine=SimpleNamespace(evaluate=lambda event, facts: None),
        reviewer=lambda event_id: None,
    )

    assert isinstance(composition.scanner, DecisionScanner)
    assert isinstance(composition.runtime, DecisionSupportRuntime)
    assert composition.runtime.config.enabled is False


def test_runtime_composition_passes_manual_check_workflow_to_scanner():
    service = object.__new__(DecisionEventService)
    data_provider = SimpleNamespace(
        universe_provider=lambda at: None,
        structure_provider=lambda security, at: None,
    )
    workflow = SimpleNamespace(
        capture_candidate=lambda **candidate: candidate,
    )

    composition = build_decision_support_runtime(
        data_provider=data_provider,
        risk_context_provider=lambda security, event, at: None,
        event_service=service,
        rule_engine=SimpleNamespace(evaluate=lambda event, facts: None),
        reviewer=lambda event_id: None,
        manual_check_workflow=workflow,
    )

    assert composition.scanner._manual_check_workflow is workflow


def test_live_provider_counts_only_actually_observed_closed_bars():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    bars = _bars(at)
    state = _State(bars, ())
    provider = LiveDecisionDataProvider(
        universe_resolver=_definition,
        state_factory=lambda code: state,
    )
    event = SimpleNamespace(
        market="a",
        code="SH.600001",
        signal_frequency="5m",
        observed_at=at,
    )

    provider.universe_provider(at)
    assert provider.count_closed_bars(event, at) == 0

    bars.append(
        _Kline(
            60,
            datetime(2026, 7, 14, 10, 35),
            11.0,
            11.0,
            11.0,
            11.0,
            1.0,
        )
    )
    state.last_op = bars[-1].date
    provider.universe_provider(at + timedelta(minutes=5))

    assert provider.count_closed_bars(event, at + timedelta(minutes=5)) == 1
    with pytest.raises(RuntimeError, match="observed bars unavailable"):
        provider.count_closed_bars(
            SimpleNamespace(
                market="a",
                code="SZ.000001",
                signal_frequency="5m",
                observed_at=at,
            ),
            at + timedelta(minutes=5),
        )


def test_risk_context_adapter_requires_same_cycle_account_and_frozen_quote():
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    state = _State(_bars(at), ())
    data_provider = LiveDecisionDataProvider(
        universe_resolver=_definition,
        state_factory=lambda code: state,
    )
    universe = data_provider.universe_provider(at)
    security = filter_universe(
        universe.securities,
        at,
        UniversePolicy.a_share_short_term(),
    ).included[0]
    account = RiskAccountSnapshot(
        account_equity=Decimal("1000000"),
        day_start_equity=Decimal("1000000"),
        available_cash=Decimal("1000000"),
        holdings=(),
        pending_exits=(),
        day_pnl=Decimal("0"),
        strategy_drawdown=Decimal("0"),
        daily_loss_locked=False,
        drawdown_locked=False,
        asof=at,
    )
    risk_provider = make_risk_context_provider(
        data_provider=data_provider,
        account_provider=lambda closed_at: account,
    )

    context = risk_provider(security, SimpleNamespace(code=security.code), at)

    assert context.asof == at
    assert context.quote.code == "SH.600001"
    assert context.quote.price == Decimal("11.0")
    assert context.quote.quote_time == at
    assert context.quote.entry_tradable is False

    stale_provider = make_risk_context_provider(
        data_provider=data_provider,
        account_provider=lambda closed_at: RiskAccountSnapshot(
            account_equity=Decimal("1000000"),
            day_start_equity=Decimal("1000000"),
            available_cash=Decimal("1000000"),
            holdings=(),
            pending_exits=(),
            day_pnl=Decimal("0"),
            strategy_drawdown=Decimal("0"),
            daily_loss_locked=False,
            drawdown_locked=False,
            asof=at - timedelta(minutes=5),
        ),
    )
    with pytest.raises(RuntimeError, match="account snapshot is not current"):
        stale_provider(security, SimpleNamespace(code=security.code), at)
