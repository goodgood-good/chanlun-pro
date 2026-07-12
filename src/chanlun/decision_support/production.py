"""Fail-closed production composition for decision-support analysis.

This module only composes analysis, persistence, risk evaluation, and LLM
review dependencies.  It deliberately exposes no trade-execution capability.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .certified_runtime import CertifiedCorpusRuntime
from .evidence import RuleEvidenceBinding
from .event_factory import bind_strategy_run_provenance
from .event_service import DecisionEventService
from .event_store import (
    DecisionEventStore,
    EventNotFoundError,
    InvalidEventTransition,
    StoredLLMReview,
)
from .exit_entry_authority import PaperLedgerEntryAuthorityResolver
from .exit_evaluation_store import (
    ExitEvaluationService,
    SQLiteExitEvaluationStore,
)
from .exit_evidence_policy import load_exit_evidence_policy_file
from .exit_runtime import EXIT_ALGORITHM_VERSION, EXIT_EVALUATION_VERSION
from .fingerprints import sha256_json
from .llm_provider import ConfiguredProvider
from .manual_check_workflow import (
    FileManualCheckStore,
    ManualCheckWorkflow,
)
from .models import DecisionEvent, EventState
from .monitor import DecisionSupportRuntime, MonitorConfig
from .paper_adapter import PaperFeeSchedule
from .paper_admission import (
    SQLitePaperLedger,
    TrustedPaperAdmission,
    paper_execution_policy_fingerprint,
)
from .paper_runtime import (
    ExplicitPaperTradingCalendar,
    PaperExitAnalysisCycle,
    PaperResearchRuntime,
    SQLitePaperRiskState,
    SQLiteTrustedPaperBarStore,
    make_paper_pinned_codes_provider,
)
from .review_service import ReviewService
from .review_prompt import PROMPT_VERSION, provider_response_format
from .risk import RiskPolicy
from .risk_snapshot import RiskSnapshot
from .rule_cards import RuleSet, load_rule_set_file
from .rule_engine import RuleEngine
from .runtime import (
    LiveDecisionDataProvider,
    PaperRiskAccountProvider,
    build_decision_support_runtime,
    live_data_provider_from_dynamic_monitor,
    make_risk_context_provider,
)
from .strategy_run import (
    ActiveStrategyRun,
    StrategyRunBootstrapReservation,
    StrategyRunIdentity,
    build_monitor_policy_fingerprint,
    build_review_runtime_policy_fingerprint,
    build_rule_algorithm_fingerprint,
    build_universe_policy_fingerprint,
    reserve_strategy_run_bootstrap,
    trusted_bar_schema_fingerprint,
)
from .universe import UniversePolicy


@dataclass(frozen=True, slots=True)
class ProductionDecisionSupportComposition:
    data_provider: LiveDecisionDataProvider
    risk_context_provider: Callable[..., object]
    rule_set: RuleSet
    rule_engine: RuleEngine
    event_service: DecisionEventService
    review_service: ReviewService
    manual_check_workflow: ManualCheckWorkflow | None
    runtime: DecisionSupportRuntime
    review_provider: Callable[[str, str, bool], dict[str, object]]
    promotion_provider: Callable[[], dict[str, object]]
    rule_evidence_resolver: Callable[[DecisionEvent], RuleEvidenceBinding]
    restored_pending_reviews: int
    paper_ledger: SQLitePaperLedger | None = None
    trusted_bar_store: SQLiteTrustedPaperBarStore | None = None
    paper_gateway: TrustedPaperAdmission | None = None
    paper_risk_state: SQLitePaperRiskState | None = None
    exit_evaluation_store: SQLiteExitEvaluationStore | None = None
    exit_evaluation_service: ExitEvaluationService | None = None
    paper_exit_cycle: PaperExitAnalysisCycle | None = None
    paper_runtime: PaperResearchRuntime | None = None
    strategy_run: ActiveStrategyRun | None = None


def _risk_resolver(
    store: DecisionEventStore,
) -> Callable[[object], RiskSnapshot]:
    def resolve(event: object) -> RiskSnapshot:
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, str) or not event_id:
            raise TypeError("risk resolution requires an event identity")
        snapshots = store.list_risk_snapshots(event_id)
        if not snapshots:
            raise InvalidEventTransition(
                "an event-bound risk snapshot is required for review"
            )
        latest = snapshots[-1]
        if not isinstance(latest, RiskSnapshot):
            raise TypeError("risk snapshot persistence returned an invalid value")
        return latest

    return resolve


def _pending_review_loader(
    store: DecisionEventStore,
    *,
    event_eligibility_provider: Callable[[object], bool] | None = None,
) -> Callable[[], tuple[str, ...]]:
    if event_eligibility_provider is not None and not callable(
        event_eligibility_provider
    ):
        raise TypeError("event_eligibility_provider must be callable")

    def load() -> tuple[str, ...]:
        pending: list[str] = []
        for event in store.list_current_strategy_events():
            snapshot = store.get_snapshot(event.event_id)
            eligible = (
                True
                if event_eligibility_provider is None
                else event_eligibility_provider(event)
            )
            if type(eligible) is not bool:
                raise TypeError(
                    "event eligibility provider must return boolean"
                )
            if eligible and snapshot.state is EventState.REVIEW_PENDING:
                pending.append(event.event_id)
        return tuple(pending)

    return load


def _event_belongs_to_strategy_run(
    event: object,
    strategy_run: ActiveStrategyRun,
) -> bool:
    return (
        getattr(event, "strategy_run_id", None) == strategy_run.run_id
        and getattr(event, "strategy_run_epoch", None) == strategy_run.epoch
        and getattr(event, "strategy_run_fingerprint", None)
        == strategy_run.strategy_run_fingerprint
    )


def _rule_evidence_resolver(
    rule_set: RuleSet,
) -> Callable[[DecisionEvent], RuleEvidenceBinding]:
    def resolve(event: DecisionEvent) -> RuleEvidenceBinding:
        rule_id = getattr(event, "rule_id", None)
        version = getattr(event, "rule_card_version", None)
        fingerprint = getattr(event, "rule_card_fingerprint", None)
        cards = tuple(
            card
            for card in rule_set.cards
            if card.rule_id == rule_id
            and card.version == version
            and card.fingerprint == fingerprint
        )
        if len(cards) != 1:
            raise InvalidEventTransition(
                "event RuleCard identity is not present in the production RuleSet"
            )
        return RuleEvidenceBinding.from_rule_card(cards[0], rule_set)

    return resolve


def build_production_decision_support(
    *,
    dynamic_monitor: object,
    corpus_runtime: CertifiedCorpusRuntime,
    rule_set_path: str | Path,
    store: DecisionEventStore,
    account_provider: Callable[..., object] | None,
    llm_provider: ConfiguredProvider,
    monitor_config: MonitorConfig | None = None,
    risk_policy: RiskPolicy | None = None,
    clock: Callable[[], datetime] | None = None,
    max_completed_bars: int = 2_000,
    review_timeout: tuple[float, float] = (10, 180),
    max_evidence_units: int = 8,
    manual_check_store: FileManualCheckStore | None = None,
    paper_ledger_path: str | Path | None = None,
    trusted_bar_store_path: str | Path | None = None,
    paper_risk_state_path: str | Path | None = None,
    exit_evaluation_store_path: str | Path | None = None,
    exit_evidence_policy_path: str | Path | None = None,
    paper_initial_cash: Decimal | None = None,
    paper_fee_schedule: PaperFeeSchedule | None = None,
    paper_calendar_provider: object | None = None,
    paper_strategy_registry_path: str | Path | None = None,
    paper_strategy_epoch: int | None = None,
    paper_strategy_engine_build_fingerprint: str | None = None,
    paper_scanner_algorithm_fingerprint: str | None = None,
    paper_structure_algorithm_fingerprint: str | None = None,
    paper_account_algorithm_fingerprint: str | None = None,
    paper_bar_provider_fingerprint: str | None = None,
) -> ProductionDecisionSupportComposition:
    paper_requested = (
        type(monitor_config) is MonitorConfig
        and monitor_config.enabled
        and monitor_config.paper_enabled
    )
    if not paper_requested and not callable(account_provider):
        raise TypeError("account_provider must be an explicit callable")
    if not isinstance(corpus_runtime, CertifiedCorpusRuntime):
        raise TypeError("corpus_runtime must be CertifiedCorpusRuntime")
    if not isinstance(store, DecisionEventStore):
        raise TypeError("store must be DecisionEventStore")
    if not isinstance(llm_provider, ConfiguredProvider):
        raise TypeError("llm_provider must be ConfiguredProvider")
    capabilities = llm_provider.capabilities
    if not (
        capabilities.supports_images
        and capabilities.supports_json_schema
    ):
        raise ValueError(
            "required LLM model capabilities are unavailable"
        )
    if monitor_config is not None and type(monitor_config) is not MonitorConfig:
        raise TypeError("monitor_config must be MonitorConfig")
    if risk_policy is not None and not isinstance(risk_policy, RiskPolicy):
        raise TypeError("risk_policy must be RiskPolicy")
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable")
    effective_monitor_config = monitor_config or MonitorConfig()
    effective_risk_policy = risk_policy or RiskPolicy.conservative()
    workflow_clock = clock or (lambda: datetime.now(timezone.utc))
    resolved_paper_calendar = None
    if paper_requested:
        required_paths = {
            "paper_ledger_path": paper_ledger_path,
            "trusted_bar_store_path": trusted_bar_store_path,
            "paper_risk_state_path": paper_risk_state_path,
            "exit_evaluation_store_path": exit_evaluation_store_path,
            "exit_evidence_policy_path": exit_evidence_policy_path,
        }
        missing_paths = tuple(
            name for name, value in required_paths.items() if value is None
        )
        if missing_paths:
            raise TypeError(
                "research paper runtime paths are required: "
                + ",".join(missing_paths)
            )
        if (
            not isinstance(paper_initial_cash, Decimal)
            or not paper_initial_cash.is_finite()
            or paper_initial_cash <= 0
        ):
            raise TypeError(
                "paper_initial_cash must be an explicit positive Decimal"
            )
        if not isinstance(paper_fee_schedule, PaperFeeSchedule):
            raise TypeError("paper_fee_schedule must be explicit")
        strategy_fingerprints = {
            "paper_strategy_engine_build_fingerprint": (
                paper_strategy_engine_build_fingerprint
            ),
            "paper_scanner_algorithm_fingerprint": (
                paper_scanner_algorithm_fingerprint
            ),
            "paper_structure_algorithm_fingerprint": (
                paper_structure_algorithm_fingerprint
            ),
            "paper_account_algorithm_fingerprint": (
                paper_account_algorithm_fingerprint
            ),
            "paper_bar_provider_fingerprint": paper_bar_provider_fingerprint,
        }
        strategy_configuration_missing = (
            paper_strategy_registry_path is None
            or isinstance(paper_strategy_epoch, bool)
            or not isinstance(paper_strategy_epoch, int)
            or paper_strategy_epoch <= 0
            or any(value is None for value in strategy_fingerprints.values())
        )
        if strategy_configuration_missing:
            raise TypeError(
                "paper strategy-run configuration must be explicit"
            )
        for field_name, value in strategy_fingerprints.items():
            if (
                not isinstance(value, str)
                or len(value) != 71
                or not value.startswith("sha256:")
                or any(
                    character not in "0123456789abcdef"
                    for character in value[7:]
                )
            ):
                raise TypeError(
                    "paper strategy-run configuration contains an invalid "
                    + field_name
                )
        resolved_paper_calendar = paper_calendar_provider
        if resolved_paper_calendar is None:
            exchange_getter = getattr(dynamic_monitor, "_exchange", None)
            if callable(exchange_getter):
                monitor_lock = getattr(dynamic_monitor, "_lock", None)
                if monitor_lock is None:
                    exchange = exchange_getter()
                else:
                    monitor_lock.acquire()
                    try:
                        exchange = exchange_getter()
                    finally:
                        monitor_lock.release()
                resolved_paper_calendar = getattr(
                    exchange,
                    "paper_trading_calendar",
                    None,
                )
        if isinstance(resolved_paper_calendar, (str, Path)):
            resolved_paper_calendar = (
                ExplicitPaperTradingCalendar.from_json_file(
                    resolved_paper_calendar
                )
            )
        if (
            resolved_paper_calendar.__class__
            is not ExplicitPaperTradingCalendar
        ):
            raise TypeError(
                "paper_calendar_provider must be an "
                "ExplicitPaperTradingCalendar or its audited JSON path"
            )
        calendar_fingerprint = getattr(
            resolved_paper_calendar,
            "fingerprint",
            None,
        )
        if (
            not isinstance(calendar_fingerprint, str)
            or not calendar_fingerprint.startswith("sha256:")
            or len(calendar_fingerprint) != 71
            or any(
                character not in "0123456789abcdef"
                for character in calendar_fingerprint[7:]
            )
            or not callable(
                getattr(resolved_paper_calendar, "session_for", None)
            )
        ):
            raise TypeError(
                "paper_calendar_provider must be explicit or supplied by "
                "the configured exchange as paper_trading_calendar"
            )

    corpus_status = corpus_runtime.status()
    if not isinstance(corpus_status, Mapping) or (
        corpus_status.get(
            "original_integrity",
            corpus_status.get("integrity"),
        )
        != "complete"
        or corpus_status.get("original_evidence") != "available"
    ):
        raise ValueError("certified original corpus is not review eligible")
    corpus = corpus_runtime.corpus()
    rules = load_rule_set_file(Path(rule_set_path), corpus=corpus)
    if not isinstance(rules, RuleSet) or not rules.cards:
        raise ValueError("a non-empty certified RuleSet is required")
    if (
        rules.corpus_manifest_sha256 != corpus.manifest_sha256
        or rules.source_pdf_sha256 != corpus.source_pdf_sha256
    ):
        raise ValueError("RuleSet identity does not match the certified corpus")

    manual_checks_required = any(
        card.automation_boundary.manual_check_ids for card in rules.cards
    )
    if manual_checks_required and not isinstance(
        manual_check_store,
        FileManualCheckStore,
    ):
        raise TypeError(
            "manual_check_store is required by the production RuleSet"
        )
    if manual_check_store is not None and not isinstance(
        manual_check_store,
        FileManualCheckStore,
    ):
        raise TypeError("manual_check_store must be FileManualCheckStore")

    scanner_universe_policy = UniversePolicy.a_share_short_term()
    scanner_max_market_age_seconds = 300
    scanner_processed_bar_limit = 2_048
    exit_policy = None
    strategy_run_identity: StrategyRunIdentity | None = None
    bootstrap_claim = None
    bootstrap: StrategyRunBootstrapReservation | None = None
    if paper_requested:
        if paper_fee_schedule is None or paper_initial_cash is None:
            raise AssertionError("paper identity inputs disappeared")
        if resolved_paper_calendar is None:
            raise AssertionError("paper calendar disappeared")
        exit_policy = load_exit_evidence_policy_file(
            exit_evidence_policy_path,
            corpus=corpus,
        )
        universe_policy_fingerprint = build_universe_policy_fingerprint(
            scanner_universe_policy
        )
        strategy_run_identity = StrategyRunIdentity(
            rule_set_fingerprint=rules.fingerprint,
            corpus_manifest_fingerprint=(
                "sha256:" + rules.corpus_manifest_sha256
            ),
            source_pdf_fingerprint="sha256:" + rules.source_pdf_sha256,
            rule_algorithm_fingerprint=build_rule_algorithm_fingerprint(
                rules
            ),
            strategy_engine_build_fingerprint=(
                paper_strategy_engine_build_fingerprint
            ),
            scanner_algorithm_fingerprint=(
                paper_scanner_algorithm_fingerprint
            ),
            structure_algorithm_fingerprint=(
                paper_structure_algorithm_fingerprint
            ),
            universe_policy_fingerprint=universe_policy_fingerprint,
            monitor_policy_fingerprint=build_monitor_policy_fingerprint(
                config=effective_monitor_config,
                max_completed_bars=max_completed_bars,
                max_market_age_seconds=scanner_max_market_age_seconds,
                processed_bar_limit=scanner_processed_bar_limit,
                universe_policy_fingerprint=universe_policy_fingerprint,
            ),
            review_provider=llm_provider.provider,
            review_model=llm_provider.model,
            review_prompt_version=PROMPT_VERSION,
            review_schema_fingerprint=sha256_json(
                provider_response_format()
            ),
            review_runtime_policy_fingerprint=(
                build_review_runtime_policy_fingerprint(
                    max_evidence_units=max_evidence_units,
                    timeout=review_timeout,
                )
            ),
            execution_policy_fingerprint=(
                paper_execution_policy_fingerprint(paper_fee_schedule)
            ),
            fee_schedule_fingerprint=paper_fee_schedule.fingerprint,
            initial_cash=paper_initial_cash,
            account_algorithm_fingerprint=(
                paper_account_algorithm_fingerprint
            ),
            risk_policy_fingerprint=sha256_json(effective_risk_policy),
            exit_policy_fingerprint=exit_policy.fingerprint,
            exit_algorithm_fingerprint=sha256_json(
                {
                    "schema_version": 1,
                    "algorithm_version": EXIT_ALGORITHM_VERSION,
                    "evaluation_version": EXIT_EVALUATION_VERSION,
                }
            ),
            calendar_fingerprint=resolved_paper_calendar.fingerprint,
            bar_provider_fingerprint=paper_bar_provider_fingerprint,
            bar_schema_fingerprint=trusted_bar_schema_fingerprint(),
        )
        bootstrap_claim = reserve_strategy_run_bootstrap(
            paper_strategy_registry_path,
            requested_epoch=paper_strategy_epoch,
            identity=strategy_run_identity,
            store_paths={
                "ledger": paper_ledger_path,
                "bar": trusted_bar_store_path,
                "risk": paper_risk_state_path,
                "exit": exit_evaluation_store_path,
            },
            now=workflow_clock(),
        )
        bootstrap = bootstrap_claim.__enter__()

    paper_ledger = (
        bootstrap.initialize_store(
            "ledger",
            paper_ledger_path,
            lambda: SQLitePaperLedger(
                paper_ledger_path,
                initial_cash=paper_initial_cash,
            ),
        )
        if paper_requested
        else None
    )
    trusted_bar_store = (
        bootstrap.initialize_store(
            "bar",
            trusted_bar_store_path,
            lambda: SQLiteTrustedPaperBarStore(
                trusted_bar_store_path,
                calendar_fingerprint=resolved_paper_calendar.fingerprint,
            ),
        )
        if paper_requested
        else None
    )
    paper_risk_state = (
        bootstrap.initialize_store(
            "risk",
            paper_risk_state_path,
            lambda: SQLitePaperRiskState(
                paper_risk_state_path,
                policy=effective_risk_policy,
            ),
        )
        if paper_requested
        else None
    )
    strategy_run: ActiveStrategyRun | None = None

    def paper_event_eligibility(event: object) -> bool:
        if strategy_run is None:
            return False
        return _event_belongs_to_strategy_run(event, strategy_run)

    def bind_current_strategy_run(event: DecisionEvent) -> DecisionEvent:
        if strategy_run is None:
            raise InvalidEventTransition("paper strategy run is unavailable")
        return bind_strategy_run_provenance(
            event,
            strategy_run_id=strategy_run.run_id,
            strategy_run_epoch=strategy_run.epoch,
            strategy_run_fingerprint=(
                strategy_run.strategy_run_fingerprint
            ),
        )

    pinned_codes_provider = (
        make_paper_pinned_codes_provider(
            paper_ledger,
            store,
            event_eligibility_provider=paper_event_eligibility,
        )
        if paper_ledger is not None
        else None
    )
    data_provider = live_data_provider_from_dynamic_monitor(
        dynamic_monitor,
        max_completed_bars=max_completed_bars,
        pinned_codes_provider=pinned_codes_provider,
    )
    rule_engine = RuleEngine(rules)
    event_service = DecisionEventService(
        store,
        data_provider,
        risk_policy=effective_risk_policy,
        clock=clock,
    )
    risk_context_provider = (
        PaperRiskAccountProvider(
            data_provider=data_provider,
            ledger=paper_ledger,
            risk_state=paper_risk_state,
        )
        if paper_requested
        else make_risk_context_provider(
            data_provider=data_provider,
            account_provider=account_provider,
        )
    )
    manual_check_workflow = (
        ManualCheckWorkflow(
            event_service=event_service,
            rule_engine=rule_engine,
            store=manual_check_store,
            clock=workflow_clock,
        )
        if manual_check_store is not None
        else None
    )
    rule_evidence_resolver = _rule_evidence_resolver(rules)
    review_service = ReviewService(
        event_service,
        corpus_runtime.corpus_index(),
        _risk_resolver(store),
        llm_provider,
        timeout=review_timeout,
        max_units=max_evidence_units,
        image_loader=corpus_runtime.load_provider_image,
        rule_evidence_resolver=rule_evidence_resolver,
        clock=clock,
    )
    composed = build_decision_support_runtime(
        data_provider=data_provider,
        risk_context_provider=risk_context_provider,
        event_service=event_service,
        rule_engine=rule_engine,
        reviewer=review_service.review_event,
        monitor_config=effective_monitor_config,
        pending_review_loader=_pending_review_loader(
            store,
            event_eligibility_provider=(
                paper_event_eligibility if paper_requested else None
            ),
        ),
        manual_check_workflow=manual_check_workflow,
        event_strategy_run_binder=(
            bind_current_strategy_run if paper_requested else None
        ),
        universe_policy=scanner_universe_policy,
        max_market_age_seconds=scanner_max_market_age_seconds,
        processed_bar_limit=scanner_processed_bar_limit,
    )

    paper_gateway: TrustedPaperAdmission | None = None
    exit_evaluation_store: SQLiteExitEvaluationStore | None = None
    exit_evaluation_service: ExitEvaluationService | None = None
    paper_exit_cycle: PaperExitAnalysisCycle | None = None
    paper_runtime: PaperResearchRuntime | None = None
    if paper_requested:
        if (
            paper_ledger is None
            or trusted_bar_store is None
            or paper_risk_state is None
        ):
            raise AssertionError("paper composition initialization is incomplete")
        if paper_fee_schedule is None:
            raise AssertionError("paper fee schedule disappeared")
        if (
            exit_policy is None
            or strategy_run_identity is None
            or bootstrap is None
            or bootstrap_claim is None
        ):
            raise AssertionError("paper bootstrap reservation disappeared")

        def resolve_entry_event(event_id: str) -> DecisionEvent | None:
            try:
                return store.get_snapshot(event_id).event
            except EventNotFoundError:
                return None

        entry_resolver = PaperLedgerEntryAuthorityResolver(
            paper_ledger,
            event_resolver=resolve_entry_event,
        )
        exit_evaluation_store = bootstrap.initialize_store(
            "exit",
            exit_evaluation_store_path,
            lambda: SQLiteExitEvaluationStore(exit_evaluation_store_path),
        )
        exit_evaluation_service = ExitEvaluationService(
            exit_evaluation_store,
            evidence_policy=exit_policy,
            entry_ledger_resolver=entry_resolver,
        )
        if (
            paper_risk_state.policy_fingerprint
            != strategy_run_identity.risk_policy_fingerprint
            or build_universe_policy_fingerprint(
                composed.scanner._universe_policy
            )
            != strategy_run_identity.universe_policy_fingerprint
            or composed.scanner._max_market_age_seconds
            != scanner_max_market_age_seconds
            or composed.scanner._processed_bar_limit
            != scanner_processed_bar_limit
        ):
            raise RuntimeError("paper strategy identity drifted during bootstrap")
        strategy_run = bootstrap.activate(now=workflow_clock())
        paper_ledger.bind_strategy_run(strategy_run)
        trusted_bar_store.bind_strategy_run(strategy_run)
        paper_risk_state.bind_strategy_run(strategy_run)
        exit_evaluation_store.bind_strategy_run(strategy_run)
        store.bind_strategy_run(strategy_run)
        risk_context_provider.bind_strategy_run(strategy_run)
        event_service.bind_strategy_run(strategy_run)
        review_service.bind_strategy_run(strategy_run)
        composed.runtime.bind_strategy_run(strategy_run)
        exit_evaluation_service.bind_strategy_run(strategy_run)
        if manual_check_store is not None:
            manual_check_store.bind_strategy_run(strategy_run)
        if manual_check_workflow is not None:
            manual_check_workflow.bind_strategy_run(strategy_run)
        data_provider.bind_signal_observation_store(
            trusted_bar_store,
            strategy_run,
        )
        bootstrap_claim.__exit__(None, None, None)
        bootstrap_claim = None
        paper_gateway = TrustedPaperAdmission(
            event_service,
            paper_ledger,
            evidence_packet_provider=review_service.evidence_packet,
            fee_schedule=paper_fee_schedule,
            bar_source=trusted_bar_store,
            manual_check_store=manual_check_store,
            risk_authority_provider=risk_context_provider,
            clock=clock,
            strategy_run=strategy_run,
            event_eligibility_provider=paper_event_eligibility,
        )
        if (
            paper_gateway.execution_policy_fingerprint
            != strategy_run.identity.execution_policy_fingerprint
            or paper_gateway.fee_schedule_fingerprint
            != strategy_run.identity.fee_schedule_fingerprint
        ):
            raise RuntimeError("paper execution policy identity drifted")
        paper_exit_cycle = PaperExitAnalysisCycle(
            ledger=paper_ledger,
            data_provider=data_provider,
            entry_resolver=entry_resolver,
            risk_state=paper_risk_state,
            exit_service=exit_evaluation_service,
            strategy_run=strategy_run,
        )
        paper_runtime = PaperResearchRuntime(
            data_provider=data_provider,
            analysis_runtime=composed.runtime,
            bar_store=trusted_bar_store,
            paper_gateway=paper_gateway,
            event_store=store,
            trading_calendar=resolved_paper_calendar,
            exit_cycle=paper_exit_cycle,
            admitted_event_ids_provider=lambda: tuple(
                intent.event_id for intent in paper_ledger.load().intents
            ),
            event_eligibility_provider=paper_event_eligibility,
            strategy_run=strategy_run,
        )

    def request_review(
        event_id: str,
        user_id: str,
        force: bool,
    ) -> dict[str, object]:
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 255
            or event_id != event_id.strip()
            or not event_id.isprintable()
        ):
            raise ValueError("event_id must be a valid non-empty string")
        if (
            not isinstance(user_id, str)
            or not user_id
            or len(user_id) > 191
            or user_id != user_id.strip()
            or not user_id.isprintable()
        ):
            raise ValueError("user_id must be a valid non-empty string")
        if type(force) is not bool:
            raise TypeError("force must be boolean")
        lease = (
            strategy_run.mutation_lease("paper_review.request")
            if strategy_run is not None
            else nullcontext()
        )
        with lease:
            if strategy_run is not None:
                strategy_run.status_payload()
                event = store.get_snapshot(event_id).event
                if not paper_event_eligibility(event):
                    raise InvalidEventTransition(
                        "event is outside the current paper strategy run"
                    )
            stored = review_service.review_event(event_id)
            if not isinstance(stored, StoredLLMReview):
                raise TypeError("ReviewService returned an invalid audit record")
            if stored.event_id != event_id:
                raise RuntimeError("review audit event identity mismatch")
            return {
                "review_id": stored.review_id,
                "event_id": stored.event_id,
                "risk_snapshot_id": stored.risk_snapshot_id,
                "packet_fingerprint": stored.packet_fingerprint,
                "reviewed_data_fingerprint": stored.reviewed_data_fingerprint,
                "provider": stored.provider,
                "model": stored.model,
                "prompt_version": stored.prompt_version,
                "status": stored.status,
                "provider_ok": stored.provider_ok,
                "verdict": stored.verdict,
                "validation_errors": list(stored.validation_errors),
                "attempt_count": stored.attempt_count,
                "latency_ms": stored.latency_ms,
                "error_code": stored.error_code,
                "response_content_bytes": stored.response_content_bytes,
                "response_content_sha256": stored.response_content_sha256,
                "response_content_truncated": stored.response_content_truncated,
                "raw_response_bytes": stored.raw_response_bytes,
                "raw_response_sha256": stored.raw_response_sha256,
                "raw_response_truncated": stored.raw_response_truncated,
                "error_message_bytes": stored.error_message_bytes,
                "error_message_sha256": stored.error_message_sha256,
                "error_message_truncated": stored.error_message_truncated,
                "created_at": stored.created_at.isoformat(),
                "force_requested": force,
            }

    def promotion_status() -> dict[str, object]:
        return {
            "state": "research",
            "promoted": False,
            "paper_gate_pending": True,
            "execution_mode": "decision_support_only",
            "monitor_enabled": composed.runtime.config.enabled,
            "auto_order_enabled": False,
            "reasons": [
                "paper_observation_gate_pending",
                "paper_executable_event_gate_pending",
                "broker_compliance_confirmation_pending",
                "live_order_execution_not_available",
            ],
        }

    restored = composed.runtime.restore_pending_reviews()
    return ProductionDecisionSupportComposition(
        data_provider=data_provider,
        risk_context_provider=risk_context_provider,
        rule_set=rules,
        rule_engine=rule_engine,
        event_service=event_service,
        review_service=review_service,
        manual_check_workflow=manual_check_workflow,
        runtime=composed.runtime,
        review_provider=request_review,
        promotion_provider=promotion_status,
        rule_evidence_resolver=rule_evidence_resolver,
        restored_pending_reviews=restored,
        paper_ledger=paper_ledger,
        trusted_bar_store=trusted_bar_store,
        paper_gateway=paper_gateway,
        paper_risk_state=paper_risk_state,
        exit_evaluation_store=exit_evaluation_store,
        exit_evaluation_service=exit_evaluation_service,
        paper_exit_cycle=paper_exit_cycle,
        paper_runtime=paper_runtime,
        strategy_run=strategy_run,
    )
