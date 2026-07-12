from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import Lock
from types import SimpleNamespace

import pytest

import chanlun.decision_support.production as production_module
from chanlun.decision_support.certified_runtime import CertifiedCorpusRuntime
from chanlun.decision_support.corpus_loader import CertifiedLessonCorpus
from chanlun.decision_support.corpus_retrieval import CorpusIndex
from chanlun.decision_support.corpus_types import EvidenceUnit, SourceTier
from chanlun.decision_support.event_service import DecisionEventService
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    InvalidEventTransition,
    StoredLLMReview,
)
from chanlun.decision_support.llm_provider import ConfiguredProvider
from chanlun.decision_support.manual_check_workflow import (
    FileManualCheckStore,
    ManualCheckWorkflow,
)
from chanlun.decision_support.models import EventState, StrategyTrack
from chanlun.decision_support.monitor import DecisionSupportRuntime, MonitorConfig
from chanlun.decision_support.paper_adapter import PaperFeeSchedule
from chanlun.decision_support.paper_admission import (
    SQLitePaperLedger,
    TrustedPaperAdmission,
)
from chanlun.decision_support.paper_runtime import (
    ExplicitPaperTradingCalendar,
    PaperExitAnalysisCycle,
    PaperResearchRuntime,
    SQLitePaperRiskState,
    SQLiteTrustedPaperBarStore,
)
from chanlun.decision_support.production import (
    build_production_decision_support,
)
from chanlun.decision_support.review_service import ReviewService
from chanlun.decision_support.rule_cards import (
    AutomationBoundary,
    DataRequirements,
    EvidenceReference,
    Predicate,
    PredicateMode,
    RuleCard,
    RuleSet,
)
from chanlun.decision_support.rule_engine import RuleEngine
from chanlun.decision_support.runtime import (
    LiveDecisionDataProvider,
    PaperRiskAccountProvider,
    RiskAccountSnapshot,
)
from chanlun.decision_support.strategy_run import (
    ActiveStrategyRun,
    SQLiteStrategyRunRegistry,
    StrategyRunIntegrityError,
    build_monitor_policy_fingerprint,
    build_universe_policy_fingerprint,
    read_strategy_run_binding,
)


_MANIFEST = "1" * 64
_SOURCE_PDF = "2" * 64
_CALENDAR_FINGERPRINT = "sha256:" + "e" * 64


def _paper_strategy_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "paper_strategy_registry_path": tmp_path / "strategy-runs.sqlite3",
        "paper_strategy_epoch": 1,
        "paper_strategy_engine_build_fingerprint": "sha256:" + "4" * 64,
        "paper_scanner_algorithm_fingerprint": "sha256:" + "5" * 64,
        "paper_structure_algorithm_fingerprint": "sha256:" + "6" * 64,
        "paper_account_algorithm_fingerprint": "sha256:" + "7" * 64,
        "paper_bar_provider_fingerprint": "sha256:" + "8" * 64,
    }


class _UnsafeCalendar:
    fingerprint = _CALENDAR_FINGERPRINT

    def session_for(self, _trading_day):
        return None


def _calendar() -> ExplicitPaperTradingCalendar:
    return ExplicitPaperTradingCalendar(
        (date(2026, 7, 14),),
        source_id="fixture-calendar",
        source_fingerprint="sha256:" + "d" * 64,
    )


def _unit(evidence_id: str, text: str) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path=f"lessons/{evidence_id}.md",
        title=evidence_id,
        text=text,
        sha256="sha256:" + "3" * 64,
        concepts=("third_buy",),
    )


def _corpus() -> CertifiedLessonCorpus:
    units = (
        _unit("support", "third buy original supporting rule"),
        _unit("counter", "third buy invalidation risk counterexample"),
    )
    return CertifiedLessonCorpus(
        root=Path("certified-fixture"),
        units=units,
        semantic_units=units,
        images=(),
        manifest_sha256=_MANIFEST,
        source_pdf_sha256=_SOURCE_PDF,
    )


class _CorpusRuntime(CertifiedCorpusRuntime):
    def __init__(self) -> None:
        self.fixture_corpus = _corpus()
        self.fixture_index = CorpusIndex.build(self.fixture_corpus.semantic_units)

    def corpus(self) -> CertifiedLessonCorpus:
        return self.fixture_corpus

    def corpus_index(self) -> CorpusIndex:
        return self.fixture_index

    def status(self) -> dict[str, object]:
        return {
            "integrity": "complete",
            "original_integrity": "complete",
            "original_evidence": "available",
        }

    def load_provider_image(self, image):
        raise AssertionError("fixture has no images")


def _rule_set() -> RuleSet:
    card = RuleCard(
        rule_id="fixture.third_buy",
        version=1,
        track=StrategyTrack.TREND_CONTINUATION,
        applicable_levels=(1,),
        algorithm_version="fixture/1",
        concepts=("third_buy",),
        evidence=(),
        counterevidence=(),
        project_fields=(),
        data_requirements=DataRequirements((), ()),
        completed_bar_requirements=(),
        candidate_predicates=(),
        confirmation_predicates=(),
        invalidation_predicates=(),
        conflict_predicates=(),
        automation_boundary=AutomationBoundary((), ()),
    )
    return RuleSet(
        schema_version=1,
        cards=(card,),
        corpus_manifest_sha256=_MANIFEST,
        source_pdf_sha256=_SOURCE_PDF,
    )


def _manual_rule_set() -> RuleSet:
    rules = _rule_set()
    check_id = "chart.fixture_confirmed"
    evidence_id = "support"
    manual = Predicate(
        predicate_id="predicate.fixture_chart",
        mode=PredicateMode.MANUAL,
        evidence_ids=(evidence_id,),
        manual_check_id=check_id,
        prompt="人工核对原文图示结构。",
    )
    card = replace(
        rules.cards[0],
        evidence=(EvidenceReference(evidence_id, 1, (1,)),),
        confirmation_predicates=(manual,),
        automation_boundary=AutomationBoundary((), (check_id,)),
    )
    return replace(rules, cards=(card,))


class _DynamicMonitor:
    market = "a"

    def __init__(self) -> None:
        self._lock = Lock()
        self.states = {"SH.600001": object()}
        self.last_selection_candidates = ()

    def current_universe(self):
        return ("SH.600001",), {"SH.600001": "fixture"}

    def _exchange(self):
        return object()

    def _new_state(self, code, exchange):
        raise AssertionError("analysis state is created only during a scan")


class _CalendarDynamicMonitor(_DynamicMonitor):
    def _exchange(self):
        return SimpleNamespace(paper_trading_calendar=_calendar())


class _PendingStore(DecisionEventStore):
    def __init__(self) -> None:
        super().__init__(lambda: None)
        self._events = (
            SimpleNamespace(event_id="event-pending-1"),
            SimpleNamespace(event_id="event-detected"),
            SimpleNamespace(event_id="event-pending-2"),
        )

    def list_events(self, **_kwargs):
        return self._events

    def get_snapshot(self, event_id: str):
        state = (
            EventState.REVIEW_PENDING
            if event_id.startswith("event-pending")
            else EventState.DETECTED
        )
        return SimpleNamespace(state=state)


class _NoRiskStore(DecisionEventStore):
    def __init__(self) -> None:
        super().__init__(lambda: None)

    def list_risk_snapshots(self, event_id: str):
        return ()


def _account(at: datetime) -> RiskAccountSnapshot:
    return RiskAccountSnapshot(
        account_equity=Decimal("100000"),
        day_start_equity=Decimal("100000"),
        available_cash=Decimal("100000"),
        holdings=(),
        pending_exits=(),
        day_pnl=Decimal("0"),
        strategy_drawdown=Decimal("0"),
        daily_loss_locked=False,
        drawdown_locked=False,
        asof=at,
    )


def _stored_review(secret: str = "fixture-secret") -> StoredLLMReview:
    return StoredLLMReview(
        id=91,
        review_id="llm-review-fixture",
        event_id="event-fixture",
        risk_snapshot_id="risk-snapshot-fixture",
        packet_fingerprint="sha256:" + "5" * 64,
        reviewed_data_fingerprint="sha256:" + "6" * 64,
        provider="siliconflow",
        model="fixture-model",
        prompt_version="chanlun-review-v3",
        fencing_token=4,
        status="provider_failure",
        provider_ok=False,
        verdict="ABSTAIN",
        response_content=f"private response {secret}",
        response_content_bytes=31,
        response_content_sha256="sha256:" + "7" * 64,
        response_content_truncated=False,
        raw_response=f"raw private response {secret}",
        raw_response_bytes=35,
        raw_response_sha256="sha256:" + "8" * 64,
        raw_response_truncated=False,
        parsed_response_json=f'{{"secret":"{secret}"}}',
        validation_errors=("provider_unavailable",),
        attempt_count=1,
        latency_ms=123,
        error_code="missing_credentials",
        error_message=f"credential {secret}",
        error_message_bytes=25,
        error_message_sha256="sha256:" + "9" * 64,
        error_message_truncated=False,
        created_at=datetime(2026, 7, 14, 2, 36, tzinfo=timezone.utc),
    )


def _llm_provider(**changes: object) -> ConfiguredProvider:
    values = {
        "provider": "siliconflow",
        "api_key": "fixture-secret",
        "model": "fixture-model",
        "supports_images": True,
        "supports_json_schema": True,
    }
    values.update(changes)
    return ConfiguredProvider.from_values(**values)


def _build(
    monkeypatch,
    *,
    corpus_runtime: CertifiedCorpusRuntime | None = None,
    rules: RuleSet | None = None,
    store: DecisionEventStore | None = None,
    llm_provider: ConfiguredProvider | None = None,
    account_provider=_account,
    monitor_config=None,
):
    selected_runtime = corpus_runtime or _CorpusRuntime()
    selected_rules = rules or _rule_set()
    monkeypatch.setattr(
        production_module,
        "load_rule_set_file",
        lambda path, *, corpus: selected_rules,
    )
    return build_production_decision_support(
        dynamic_monitor=_CalendarDynamicMonitor(),
        corpus_runtime=selected_runtime,
        rule_set_path=Path("fixture-rules.json"),
        store=store or DecisionEventStore(lambda: None),
        account_provider=account_provider,
        llm_provider=llm_provider or _llm_provider(),
        monitor_config=monitor_config,
    )


def test_factory_requires_explicit_account_provider() -> None:
    with pytest.raises(TypeError, match="account_provider"):
        build_production_decision_support(
            dynamic_monitor=object(),
            corpus_runtime=object(),
            rule_set_path="rules.json",
            store=object(),
            account_provider=None,
            llm_provider=object(),
        )


def test_factory_assembles_disabled_analysis_review_runtime(
    monkeypatch,
) -> None:
    corpus_runtime = _CorpusRuntime()
    rules = _rule_set()
    loaded: dict[str, object] = {}

    def load_rules(path, *, corpus):
        loaded.update(path=path, corpus=corpus)
        return rules

    monkeypatch.setattr(production_module, "load_rule_set_file", load_rules)
    store = DecisionEventStore(lambda: None)
    dynamic_monitor = _DynamicMonitor()
    built = build_production_decision_support(
        dynamic_monitor=dynamic_monitor,
        corpus_runtime=corpus_runtime,
        rule_set_path=Path("fixture-rules.json"),
        store=store,
        account_provider=_account,
        llm_provider=_llm_provider(),
    )

    assert isinstance(built.data_provider, LiveDecisionDataProvider)
    assert isinstance(built.rule_engine, RuleEngine)
    assert isinstance(built.event_service, DecisionEventService)
    assert isinstance(built.review_service, ReviewService)
    assert isinstance(built.runtime, DecisionSupportRuntime)
    assert built.runtime.config.enabled is False
    assert built.runtime.config.auto_order_enabled is False
    assert built.event_service.store is store
    assert built.event_service._bar_clock is built.data_provider
    assert built.data_provider._states is not dynamic_monitor.states
    assert built.rule_set is rules
    assert built.restored_pending_reviews == 0
    assert callable(built.review_provider)
    assert callable(built.promotion_provider)
    assert loaded == {
        "path": Path("fixture-rules.json"),
        "corpus": corpus_runtime.fixture_corpus,
    }


def test_factory_requires_and_wires_manual_check_store_for_manual_rules(
    monkeypatch,
    tmp_path,
) -> None:
    rules = _manual_rule_set()
    monkeypatch.setattr(
        production_module,
        "load_rule_set_file",
        lambda path, *, corpus: rules,
    )
    kwargs = {
        "dynamic_monitor": _DynamicMonitor(),
        "corpus_runtime": _CorpusRuntime(),
        "rule_set_path": Path("fixture-rules.json"),
        "store": DecisionEventStore(lambda: None),
        "account_provider": _account,
        "llm_provider": _llm_provider(),
    }

    with pytest.raises(TypeError, match="manual_check_store is required"):
        build_production_decision_support(**kwargs)

    manual_store = FileManualCheckStore(tmp_path / "manual-checks")
    built = build_production_decision_support(
        **kwargs,
        manual_check_store=manual_store,
    )

    assert isinstance(built.manual_check_workflow, ManualCheckWorkflow)
    assert built.manual_check_workflow.store is manual_store
    assert built.runtime._scanner._manual_check_workflow is (
        built.manual_check_workflow
    )


def test_factory_rejects_corpus_without_certified_original_evidence(
    monkeypatch,
) -> None:
    corpus_runtime = _CorpusRuntime()
    corpus_runtime.status = lambda: {
        "integrity": "incomplete",
        "original_integrity": "incomplete",
        "original_evidence": "unavailable",
    }

    with pytest.raises(ValueError, match="certified original corpus"):
        _build(monkeypatch, corpus_runtime=corpus_runtime)


def test_factory_rejects_empty_ruleset(monkeypatch) -> None:
    empty = RuleSet(
        schema_version=1,
        cards=(),
        corpus_manifest_sha256=_MANIFEST,
        source_pdf_sha256=_SOURCE_PDF,
    )

    with pytest.raises(ValueError, match="non-empty certified RuleSet"):
        _build(monkeypatch, rules=empty)


def test_factory_rejects_ruleset_from_another_corpus(monkeypatch) -> None:
    mismatched = RuleSet(
        schema_version=1,
        cards=_rule_set().cards,
        corpus_manifest_sha256="4" * 64,
        source_pdf_sha256=_SOURCE_PDF,
    )

    with pytest.raises(ValueError, match="RuleSet identity"):
        _build(monkeypatch, rules=mismatched)


@pytest.mark.parametrize(
    "missing_capability",
    ("supports_images", "supports_json_schema"),
)
def test_factory_rejects_missing_model_capability(
    monkeypatch,
    missing_capability: str,
) -> None:
    provider = _llm_provider(**{missing_capability: False})

    with pytest.raises(ValueError, match="model capabilities"):
        _build(monkeypatch, llm_provider=provider)


def test_risk_provider_rejects_account_snapshot_from_another_bar(
    monkeypatch,
) -> None:
    closed_at = datetime(2026, 7, 14, 2, 35, tzinfo=timezone.utc)

    def stale_account(_requested_at: datetime) -> RiskAccountSnapshot:
        return _account(closed_at - timedelta(minutes=5))

    built = _build(monkeypatch, account_provider=stale_account)

    with pytest.raises(RuntimeError, match="not current for requested bar"):
        built.risk_context_provider(
            SimpleNamespace(code="SH.600001"),
            SimpleNamespace(code="SH.600001"),
            closed_at,
        )


def test_review_risk_resolver_never_fabricates_missing_snapshot(
    monkeypatch,
) -> None:
    built = _build(monkeypatch, store=_NoRiskStore())

    with pytest.raises(InvalidEventTransition, match="risk snapshot"):
        built.review_service._risk_resolver(
            SimpleNamespace(event_id="event-without-risk")
        )


def test_enabled_runtime_restores_only_persisted_pending_reviews(
    monkeypatch,
) -> None:
    built = _build(
        monkeypatch,
        store=_PendingStore(),
        monitor_config=MonitorConfig(enabled=True, paper_enabled=False),
    )

    assert built.restored_pending_reviews == 2
    assert built.runtime.health().queue_depth == 2
    assert built.promotion_provider()["state"] == "research"
    assert built.promotion_provider()["paper_gate_pending"] is True
    assert built.promotion_provider()["monitor_enabled"] is True


def test_web_review_provider_validates_request_and_returns_redacted_audit(
    monkeypatch,
) -> None:
    built = _build(monkeypatch)
    calls: list[str] = []

    def review(event_id: str) -> StoredLLMReview:
        calls.append(event_id)
        return _stored_review()

    built.review_service.review_event = review
    payload = built.review_provider("event-fixture", "operator-1", True)

    assert calls == ["event-fixture"]
    assert payload == {
            "review_id": "llm-review-fixture",
            "event_id": "event-fixture",
            "risk_snapshot_id": "risk-snapshot-fixture",
            "packet_fingerprint": "sha256:" + "5" * 64,
        "reviewed_data_fingerprint": "sha256:" + "6" * 64,
        "provider": "siliconflow",
        "model": "fixture-model",
        "prompt_version": "chanlun-review-v3",
        "status": "provider_failure",
        "provider_ok": False,
        "verdict": "ABSTAIN",
        "validation_errors": ["provider_unavailable"],
        "attempt_count": 1,
        "latency_ms": 123,
        "error_code": "missing_credentials",
        "response_content_bytes": 31,
        "response_content_sha256": "sha256:" + "7" * 64,
        "response_content_truncated": False,
        "raw_response_bytes": 35,
        "raw_response_sha256": "sha256:" + "8" * 64,
        "raw_response_truncated": False,
        "error_message_bytes": 25,
        "error_message_sha256": "sha256:" + "9" * 64,
        "error_message_truncated": False,
        "created_at": "2026-07-14T02:36:00+00:00",
        "force_requested": True,
    }
    serialized = json.dumps(payload, sort_keys=True)
    assert "fixture-secret" not in serialized
    assert "private response" not in serialized
    assert "credential fixture-secret" not in serialized

    for user_id in (None, "", " operator-1", "operator-1\n"):
        with pytest.raises((TypeError, ValueError), match="user_id"):
            built.review_provider("event-fixture", user_id, False)
    for force in (0, 1, "true", None):
        with pytest.raises((TypeError, ValueError), match="force"):
            built.review_provider("event-fixture", "operator-1", force)


def test_promotion_provider_is_always_explicitly_research_only(
    monkeypatch,
) -> None:
    built = _build(monkeypatch)

    payload = built.promotion_provider()

    assert payload == {
        "state": "research",
        "promoted": False,
        "paper_gate_pending": True,
        "execution_mode": "decision_support_only",
        "monitor_enabled": False,
        "auto_order_enabled": False,
        "reasons": [
            "paper_observation_gate_pending",
            "paper_executable_event_gate_pending",
            "broker_compliance_confirmation_pending",
            "live_order_execution_not_available",
        ],
    }
    assert "small_cap_manual" not in json.dumps(payload, sort_keys=True)
    assert "auto_live" not in json.dumps(payload, sort_keys=True)


def test_production_composition_has_no_execution_or_legacy_imports() -> None:
    path = Path(production_module.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    attributes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)

    forbidden_modules = {"broker", "order", "trader", "exchange"}
    assert not {
        segment
        for module in modules
        for segment in module.split(".")
        if segment in forbidden_modules
    }
    assert "run_once" not in attributes
    assert "notifier" not in attributes


def test_enabled_factory_wires_restart_safe_research_paper_and_exit_runtime(
    tmp_path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    corpus_runtime = CertifiedCorpusRuntime(
        project_root / "audit" / "chanlun_lesson_corpus_v3"
    )
    manual_store = FileManualCheckStore(tmp_path / "manual")

    built = build_production_decision_support(
        dynamic_monitor=_CalendarDynamicMonitor(),
        corpus_runtime=corpus_runtime,
        rule_set_path=(
            project_root / "config" / "decision_support" / "rule_cards.json"
        ),
        store=_PendingStore(),
        account_provider=None,
        llm_provider=_llm_provider(),
        monitor_config=MonitorConfig(enabled=True, paper_enabled=True),
        manual_check_store=manual_store,
        paper_ledger_path=tmp_path / "paper-ledger.sqlite3",
        trusted_bar_store_path=tmp_path / "paper-bars.sqlite3",
        paper_risk_state_path=tmp_path / "paper-risk.sqlite3",
        exit_evaluation_store_path=tmp_path / "paper-exits.sqlite3",
        exit_evidence_policy_path=(
            project_root
            / "config"
            / "decision_support"
            / "exit_evidence_policy.json"
        ),
        paper_initial_cash=Decimal("1000000"),
        paper_fee_schedule=PaperFeeSchedule(),
        **_paper_strategy_kwargs(tmp_path),
    )

    assert isinstance(built.paper_ledger, SQLitePaperLedger)
    assert isinstance(built.trusted_bar_store, SQLiteTrustedPaperBarStore)
    assert isinstance(built.paper_gateway, TrustedPaperAdmission)
    assert isinstance(built.paper_risk_state, SQLitePaperRiskState)
    assert isinstance(built.paper_exit_cycle, PaperExitAnalysisCycle)
    assert isinstance(built.paper_runtime, PaperResearchRuntime)
    assert built.paper_runtime.health().mode == "research_paper"
    assert built.paper_runtime.health().auto_order_enabled is False
    assert built.paper_runtime.health().live_order_capability is False
    assert built.data_provider._universe_resolver is not None
    assert isinstance(built.risk_context_provider, PaperRiskAccountProvider)
    assert built.risk_context_provider._ledger is built.paper_ledger
    assert built.risk_context_provider._risk_state is built.paper_risk_state
    assert built.paper_gateway._risk_authority_provider is (
        built.risk_context_provider
    )
    assert isinstance(built.strategy_run, ActiveStrategyRun)
    assert built.strategy_run.epoch == 1
    assert built.strategy_run.evidence_scope == "current_epoch_only"
    runtime_universe_fingerprint = build_universe_policy_fingerprint(
        built.runtime._scanner._universe_policy
    )
    assert (
        built.strategy_run.identity.universe_policy_fingerprint
        == runtime_universe_fingerprint
    )
    assert built.strategy_run.identity.monitor_policy_fingerprint == (
        build_monitor_policy_fingerprint(
            config=built.runtime.config,
            max_completed_bars=2_000,
            max_market_age_seconds=(
                built.runtime._scanner._max_market_age_seconds
            ),
            processed_bar_limit=built.runtime._scanner._processed_bar_limit,
            universe_policy_fingerprint=runtime_universe_fingerprint,
        )
    )
    assert built.data_provider._signal_observation_store is built.trusted_bar_store
    assert (
        built.data_provider._signal_observation_strategy_run
        is built.strategy_run
    )
    assert built.data_provider._cycle is None
    strategy_bound_mutation_surfaces = (
        built.paper_ledger,
        built.trusted_bar_store,
        built.paper_risk_state,
        built.exit_evaluation_store,
        built.exit_evaluation_service,
        built.risk_context_provider,
        built.event_service.store,
        built.event_service,
        built.review_service,
        built.manual_check_workflow.store,
        built.manual_check_workflow,
        built.runtime,
    )
    assert all(
        surface._mutation_fence._active is built.strategy_run
        for surface in strategy_bound_mutation_surfaces
    )
    event_gate = built.paper_runtime._event_eligibility_provider
    assert callable(event_gate)
    assert event_gate(
        SimpleNamespace(
            strategy_run_id=built.strategy_run.run_id,
            strategy_run_epoch=built.strategy_run.epoch,
            strategy_run_fingerprint=(
                built.strategy_run.strategy_run_fingerprint
            ),
        )
    ) is True
    assert event_gate(
        SimpleNamespace(
            strategy_run_id=built.strategy_run.run_id,
            strategy_run_epoch=built.strategy_run.epoch + 1,
            strategy_run_fingerprint=(
                built.strategy_run.strategy_run_fingerprint
            ),
        )
    ) is False
    assert event_gate(
        SimpleNamespace(
            observed_at=built.strategy_run.started_at,
            rule_set_fingerprint=built.strategy_run.identity.rule_set_fingerprint,
            corpus_manifest_fingerprint=(
                built.strategy_run.identity.corpus_manifest_fingerprint
            ),
        )
    ) is False
    assert callable(built.runtime._scanner._event_strategy_run_binder)
    for role, path in {
        "ledger": tmp_path / "paper-ledger.sqlite3",
        "bar": tmp_path / "paper-bars.sqlite3",
        "risk": tmp_path / "paper-risk.sqlite3",
        "exit": tmp_path / "paper-exits.sqlite3",
    }.items():
        binding = read_strategy_run_binding(path)
        assert binding is not None
        assert binding.run_id == built.strategy_run.run_id
        assert binding.store_role == role
    with sqlite3.connect(tmp_path / "strategy-runs.sqlite3") as connection:
        constructor_lease_events = connection.execute(
            """
            SELECT operation, event_type
            FROM paper_strategy_run_mutation_lease
            ORDER BY event_sequence
            """
        ).fetchall()
    assert constructor_lease_events == [
        ("paper_admission.bind_execution_policy", "acquire"),
        ("paper_admission.bind_execution_policy", "release"),
        ("decision_support_runtime.restore_pending_reviews", "acquire"),
        ("decision_support_runtime.restore_pending_reviews", "release"),
    ]
    fenced_method_contracts = (
        (built.paper_ledger, ("commit",)),
        (
            built.trusted_bar_store,
            (
                "start_cycle_attempt",
                "fail_cycle_attempt",
                "record_calendar_preflight_failure",
                "record_cycle",
                "complete_cycle",
                "put",
            ),
        ),
        (
            built.paper_risk_state,
            ("mark", "record_exit_coverage", "record_exit_scan_outcome"),
        ),
        (built.exit_evaluation_store, ("persist",)),
        (
            built.exit_evaluation_service,
            ("evaluate_and_persist", "evaluate_and_persist_many"),
        ),
        (
            built.event_service.store,
            (
                "append_event",
                "append_user_decision",
                "append_risk_snapshot",
                "issue_paper_admission_authorization",
                "append_risk_latch_audit",
                "append_transition",
                "append_transition_chain",
                "append_review_application",
                "acquire_llm_review_claim",
                "append_llm_review_attempt",
                "append_llm_review",
            ),
        ),
        (
            built.event_service,
            (
                "register",
                "mark_review_pending",
                "invalidate",
                "expire_stale",
                "apply_review",
            ),
        ),
        (built.review_service, ("review_event",)),
        (
            built.manual_check_workflow.store,
            ("put_if_absent", "append_attempt", "mark_advanced"),
        ),
        (
            built.manual_check_workflow,
            ("capture_candidate", "submit"),
        ),
        (
            built.runtime,
            ("scan_cycle", "review_cycle", "restore_pending_reviews"),
        ),
        (built.risk_context_provider, ("__call__",)),
    )
    assert all(
        hasattr(getattr(type(surface), method_name), "__wrapped__")
        for surface, method_names in fenced_method_contracts
        for method_name in method_names
    )
    raw_surface_calls = (
        (
            "paper_ledger.commit",
            lambda: built.paper_ledger.commit(
                expected_revision=0,
                state=object(),
            ),
        ),
        ("trusted_paper_bar_store.put", lambda: built.trusted_bar_store.put(object())),
        (
            "paper_risk_state.mark",
            lambda: built.paper_risk_state.mark(
                Decimal("1000000"),
                built.strategy_run.started_at,
            ),
        ),
        (
            "exit_evaluation_store.persist",
            lambda: built.exit_evaluation_store.persist(
                object(),
                expected_revision=0,
            ),
        ),
        (
            "exit_evaluation_service.evaluate_and_persist",
            lambda: built.exit_evaluation_service.evaluate_and_persist(object()),
        ),
        (
            "decision_event_store.append_event",
            lambda: built.event_service.store.append_event(object()),
        ),
        (
            "decision_event_service.invalidate",
            lambda: built.event_service.invalidate(
                "",
                "",
                occurred_at=built.strategy_run.started_at,
            ),
        ),
        ("review_service.review_event", lambda: built.review_service.review_event("")),
        (
            "manual_check_store.put_if_absent",
            lambda: built.manual_check_workflow.store.put_if_absent(object()),
        ),
        (
            "manual_check_workflow.submit",
            lambda: built.manual_check_workflow.submit("", ()),
        ),
        (
            "decision_support_runtime.scan_cycle",
            lambda: built.runtime.scan_cycle(object()),
        ),
        (
            "paper_risk_account_provider.evaluate",
            lambda: built.risk_context_provider(
                object(),
                object(),
                built.strategy_run.started_at,
            ),
        ),
    )
    for operation, call in raw_surface_calls:
        if operation == "paper_risk_state.mark":
            call()
        else:
            with pytest.raises(Exception):
                call()
    with sqlite3.connect(tmp_path / "strategy-runs.sqlite3") as connection:
        raw_surface_lease_events = connection.execute(
            """
            SELECT operation, event_type
            FROM paper_strategy_run_mutation_lease
            WHERE event_sequence > 4
            ORDER BY event_sequence
            """
        ).fetchall()
    assert raw_surface_lease_events == [
        (operation, event_type)
        for operation, _call in raw_surface_calls
        for event_type in ("acquire", "release")
    ]
    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_mutation_lease_required",
    ):
        built.paper_ledger._bind_execution_policy(
            fee_schedule_fingerprint="sha256:" + "1" * 64,
            execution_policy_fingerprint="sha256:" + "2" * 64,
            policy_document={},
            capability=object(),
        )
    review_event = SimpleNamespace(
        strategy_run_id=built.strategy_run.run_id,
        strategy_run_epoch=built.strategy_run.epoch,
        strategy_run_fingerprint=(
            built.strategy_run.strategy_run_fingerprint
        ),
    )
    built.event_service.store.get_snapshot = lambda _event_id: SimpleNamespace(
        event=review_event
    )

    def review_under_lease(_event_id: str) -> StoredLLMReview:
        diagnostics = built.strategy_run.mutation_lease_diagnostics()
        assert diagnostics["active_count"] == 1
        return _stored_review()

    built.review_service.review_event = review_under_lease
    assert built.review_provider(
        "event-fixture",
        "operator-1",
        False,
    )["review_id"] == "llm-review-fixture"
    with sqlite3.connect(
        _paper_strategy_kwargs(tmp_path)["paper_strategy_registry_path"]
    ) as connection:
        connection.execute(
            "UPDATE paper_strategy_run_epoch SET status = 'closed', "
            "ended_at = ? WHERE run_id = ?",
            (
                (built.strategy_run.started_at + timedelta(seconds=1)).isoformat(),
                built.strategy_run.run_id,
            ),
        )
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.paper_runtime.bar_cycle(built.strategy_run.started_at)
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.paper_runtime.admission_cycle(built.strategy_run.started_at)
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.paper_gateway.process_bar(object())
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.paper_gateway.admit(
            "old-event",
            object(),
            risk_snapshot_id="old-risk",
        )
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.paper_exit_cycle(built.strategy_run.started_at)
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.paper_exit_cycle.record_scan_outcome(
            built.strategy_run.started_at,
            "scan_complete",
        )
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.review_provider("old-event", "operator-1", False)
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.runtime.restore_pending_reviews()
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.paper_risk_state.mark(
            Decimal("999999"),
            built.strategy_run.started_at + timedelta(seconds=2),
        )
    with pytest.raises(StrategyRunIntegrityError, match="not_active"):
        built.event_service.store.append_event(object())


def test_paper_factory_rejects_wrong_identity_before_strategy_store_writes(
    tmp_path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    corpus_runtime = CertifiedCorpusRuntime(
        project_root / "audit" / "chanlun_lesson_corpus_v3"
    )
    common = {
        "dynamic_monitor": _CalendarDynamicMonitor(),
        "corpus_runtime": corpus_runtime,
        "rule_set_path": (
            project_root / "config" / "decision_support" / "rule_cards.json"
        ),
        "account_provider": None,
        "llm_provider": _llm_provider(),
        "monitor_config": MonitorConfig(enabled=True, paper_enabled=True),
        "exit_evidence_policy_path": (
            project_root
            / "config"
            / "decision_support"
            / "exit_evidence_policy.json"
        ),
        "paper_initial_cash": Decimal("1000000"),
        "paper_fee_schedule": PaperFeeSchedule(),
        "paper_strategy_registry_path": tmp_path / "strategy-runs.sqlite3",
        "paper_strategy_epoch": 1,
        "paper_scanner_algorithm_fingerprint": "sha256:" + "5" * 64,
        "paper_structure_algorithm_fingerprint": "sha256:" + "6" * 64,
        "paper_account_algorithm_fingerprint": "sha256:" + "7" * 64,
        "paper_bar_provider_fingerprint": "sha256:" + "8" * 64,
    }
    first_paths = {
        "paper_ledger_path": tmp_path / "first-ledger.sqlite3",
        "trusted_bar_store_path": tmp_path / "first-bars.sqlite3",
        "paper_risk_state_path": tmp_path / "first-risk.sqlite3",
        "exit_evaluation_store_path": tmp_path / "first-exits.sqlite3",
    }
    build_production_decision_support(
        **common,
        **first_paths,
        store=_PendingStore(),
        manual_check_store=FileManualCheckStore(tmp_path / "manual-first"),
        paper_strategy_engine_build_fingerprint="sha256:" + "4" * 64,
    )
    rejected_paths = {
        "paper_ledger_path": tmp_path / "rejected-ledger.sqlite3",
        "trusted_bar_store_path": tmp_path / "rejected-bars.sqlite3",
        "paper_risk_state_path": tmp_path / "rejected-risk.sqlite3",
        "exit_evaluation_store_path": tmp_path / "rejected-exits.sqlite3",
    }

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_fingerprint_mismatch",
    ):
        build_production_decision_support(
            **common,
            **rejected_paths,
            store=_PendingStore(),
            manual_check_store=FileManualCheckStore(tmp_path / "manual-rejected"),
            paper_strategy_engine_build_fingerprint="sha256:" + "9" * 64,
        )

    assert all(not Path(path).exists() for path in rejected_paths.values())


def test_paper_factory_exact_identity_recovers_after_injected_bootstrap_failure(
    tmp_path,
    monkeypatch,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    kwargs = {
        "dynamic_monitor": _CalendarDynamicMonitor(),
        "corpus_runtime": CertifiedCorpusRuntime(
            project_root / "audit" / "chanlun_lesson_corpus_v3"
        ),
        "rule_set_path": (
            project_root / "config" / "decision_support" / "rule_cards.json"
        ),
        "store": _PendingStore(),
        "account_provider": None,
        "llm_provider": _llm_provider(),
        "monitor_config": MonitorConfig(enabled=True, paper_enabled=True),
        "manual_check_store": FileManualCheckStore(tmp_path / "manual"),
        "paper_ledger_path": tmp_path / "paper-ledger.sqlite3",
        "trusted_bar_store_path": tmp_path / "paper-bars.sqlite3",
        "paper_risk_state_path": tmp_path / "paper-risk.sqlite3",
        "exit_evaluation_store_path": tmp_path / "paper-exits.sqlite3",
        "exit_evidence_policy_path": (
            project_root
            / "config"
            / "decision_support"
            / "exit_evidence_policy.json"
        ),
        "paper_initial_cash": Decimal("1000000"),
        "paper_fee_schedule": PaperFeeSchedule(),
        **_paper_strategy_kwargs(tmp_path),
    }
    real_bar_store = production_module.SQLiteTrustedPaperBarStore
    factory_calls = 0

    def fail_first_bar_factory(*args, **factory_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise RuntimeError("injected-bootstrap-failure")
        return real_bar_store(*args, **factory_kwargs)

    monkeypatch.setattr(
        production_module,
        "SQLiteTrustedPaperBarStore",
        fail_first_bar_factory,
    )
    with pytest.raises(RuntimeError, match="injected-bootstrap-failure"):
        build_production_decision_support(**kwargs)

    pending = SQLiteStrategyRunRegistry(
        kwargs["paper_strategy_registry_path"]
    ).list_epochs()
    assert len(pending) == 1
    assert pending[0].status == "initializing"
    monkeypatch.setattr(
        production_module,
        "SQLiteTrustedPaperBarStore",
        real_bar_store,
    )
    kwargs["store"] = _PendingStore()
    kwargs["manual_check_store"] = FileManualCheckStore(
        tmp_path / "manual-restart"
    )

    recovered = build_production_decision_support(**kwargs)

    assert recovered.strategy_run is not None
    assert recovered.strategy_run.epoch == 1
    assert recovered.strategy_run.status_payload()["state"] == "active"


def test_paper_factory_requires_explicit_strategy_run_identity(tmp_path) -> None:
    with pytest.raises(TypeError, match="paper strategy-run configuration"):
        build_production_decision_support(
            dynamic_monitor=_CalendarDynamicMonitor(),
            corpus_runtime=_CorpusRuntime(),
            rule_set_path=Path("fixture-rules.json"),
            store=DecisionEventStore(lambda: None),
            account_provider=None,
            llm_provider=_llm_provider(),
            monitor_config=MonitorConfig(enabled=True, paper_enabled=True),
            paper_ledger_path=tmp_path / "paper-ledger.sqlite3",
            trusted_bar_store_path=tmp_path / "paper-bars.sqlite3",
            paper_risk_state_path=tmp_path / "paper-risk.sqlite3",
            exit_evaluation_store_path=tmp_path / "paper-exits.sqlite3",
            exit_evidence_policy_path=tmp_path / "exit-policy.json",
            paper_initial_cash=Decimal("1000000"),
            paper_fee_schedule=PaperFeeSchedule(),
        )


def test_paper_factory_invalid_cash_never_reserves_strategy_epoch(tmp_path) -> None:
    paper_paths = {
        "paper_ledger_path": tmp_path / "paper-ledger.sqlite3",
        "trusted_bar_store_path": tmp_path / "paper-bars.sqlite3",
        "paper_risk_state_path": tmp_path / "paper-risk.sqlite3",
        "exit_evaluation_store_path": tmp_path / "paper-exits.sqlite3",
    }
    with pytest.raises(TypeError, match="paper_initial_cash"):
        build_production_decision_support(
            dynamic_monitor=_CalendarDynamicMonitor(),
            corpus_runtime=_CorpusRuntime(),
            rule_set_path=Path("fixture-rules.json"),
            store=DecisionEventStore(lambda: None),
            account_provider=_account,
            llm_provider=_llm_provider(),
            monitor_config=MonitorConfig(enabled=True, paper_enabled=True),
            **paper_paths,
            exit_evidence_policy_path=tmp_path / "exit-policy.json",
            paper_initial_cash=Decimal("0"),
            paper_fee_schedule=PaperFeeSchedule(),
            **_paper_strategy_kwargs(tmp_path),
        )

    assert not (tmp_path / "strategy-runs.sqlite3").exists()
    assert all(not path.exists() for path in paper_paths.values())


def test_paper_factory_fails_closed_without_audited_calendar(tmp_path) -> None:
    paper_paths = {
        "paper_ledger_path": tmp_path / "paper-ledger.sqlite3",
        "trusted_bar_store_path": tmp_path / "paper-bars.sqlite3",
        "paper_risk_state_path": tmp_path / "paper-risk.sqlite3",
        "exit_evaluation_store_path": tmp_path / "paper-exits.sqlite3",
    }
    with pytest.raises(TypeError, match="paper_calendar_provider"):
        build_production_decision_support(
            dynamic_monitor=_DynamicMonitor(),
            corpus_runtime=_CorpusRuntime(),
            rule_set_path=Path("fixture-rules.json"),
            store=DecisionEventStore(lambda: None),
            account_provider=_account,
            llm_provider=_llm_provider(),
            monitor_config=MonitorConfig(enabled=True, paper_enabled=True),
            **paper_paths,
            exit_evidence_policy_path=tmp_path / "exit-policy.json",
            paper_initial_cash=Decimal("1000000"),
            paper_fee_schedule=PaperFeeSchedule(),
            **_paper_strategy_kwargs(tmp_path),
        )

    assert not (tmp_path / "strategy-runs.sqlite3").exists()
    assert all(not path.exists() for path in paper_paths.values())


def test_paper_factory_rejects_protocol_only_calendar_provider(tmp_path) -> None:
    with pytest.raises(TypeError, match="paper_calendar_provider"):
        build_production_decision_support(
            dynamic_monitor=_DynamicMonitor(),
            corpus_runtime=_CorpusRuntime(),
            rule_set_path=Path("fixture-rules.json"),
            store=DecisionEventStore(lambda: None),
            account_provider=_account,
            llm_provider=_llm_provider(),
            monitor_config=MonitorConfig(enabled=True, paper_enabled=True),
            paper_calendar_provider=_UnsafeCalendar(),
            paper_ledger_path=tmp_path / "paper-ledger.sqlite3",
            trusted_bar_store_path=tmp_path / "paper-bars.sqlite3",
            paper_risk_state_path=tmp_path / "paper-risk.sqlite3",
            exit_evaluation_store_path=tmp_path / "paper-exits.sqlite3",
            exit_evidence_policy_path=tmp_path / "exit-policy.json",
            paper_initial_cash=Decimal("1000000"),
            paper_fee_schedule=PaperFeeSchedule(),
            **_paper_strategy_kwargs(tmp_path),
        )
