from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from threading import Event, Lock

import pytest
import chanlun.decision_support.review_service as review_service_module
from sqlalchemy import create_engine, event as sqlalchemy_event, func, inspect, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByLLMReview,
    TableByLLMReviewAttempt,
    TableByLLMReviewClaim,
    TableByDecisionReview,
    TableByDecisionTransition,
    TableByRiskSnapshot,
)
from chanlun.decision_support.corpus_retrieval import CorpusIndex
from chanlun.decision_support.corpus_types import (
    EvidenceUnit,
    ImageEvidence,
    SourceTier,
)
from chanlun.decision_support.evidence import (
    ModelCapabilities,
    RuleEvidenceBinding,
    build_evidence_packet,
)
from chanlun.decision_support.event_service import DecisionEventService
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    EventConflictError,
    InvalidEventTransition,
    LLMReviewClaimLostError,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.llm_provider import (
    LLMProvider,
    OfflineAbstainProvider,
    ProviderResponse,
)
from chanlun.decision_support.models import EventState
from chanlun.decision_support.review_prompt import PROMPT_VERSION
from chanlun.decision_support.review_schema import parse_review
from chanlun.decision_support.review_service import ReviewService
from chanlun.decision_support.risk_snapshot import RiskSnapshot


class _ClosedBarClock:
    def __init__(self) -> None:
        self.closed_at = ()

    def count_closed_bars(self, event, asof) -> int:
        return sum(
            event.observed_at < closed_at <= asof
            for closed_at in self.closed_at
        )


class _StrategyRunCapability:
    run_id = "paper-run-" + "a" * 64
    epoch = 7
    strategy_run_fingerprint = "sha256:" + "b" * 64

    def __init__(self) -> None:
        self._depth = 0

    @contextmanager
    def mutation_lease(self, _operation):
        self._depth += 1
        try:
            yield object()
        finally:
            self._depth -= 1

    def require_current_mutation_lease(self) -> None:
        if self._depth <= 0:
            raise RuntimeError("test mutation lease missing")


class _FakeProvider:
    provider = "fake"
    model = "model-v1"
    capabilities = ModelCapabilities(
        supports_images=False,
        supports_json_schema=True,
    )

    def __init__(
        self,
        responses: tuple[ProviderResponse, ...],
        *,
        entered: Event | None = None,
        release: Event | None = None,
        after_response=None,
    ) -> None:
        self._responses = responses
        self._entered = entered
        self._release = release
        self._after_response = after_response
        self._lock = Lock()
        self.call_count = 0

    def complete(self, messages, images, timeout) -> ProviderResponse:
        with self._lock:
            index = self.call_count
            self.call_count += 1
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            assert self._release.wait(timeout=10)
        if not self._responses:
            raise AssertionError("provider must not be called")
        response = self._responses[min(index, len(self._responses) - 1)]
        if self._after_response is not None:
            self._after_response()
        return response


@dataclass(frozen=True)
class _Bundle:
    engine: object
    event_service: DecisionEventService
    store: DecisionEventStore
    bar_clock: _ClosedBarClock
    event: object
    risk_snapshot: RiskSnapshot
    index: CorpusIndex
    reviewed_at: object
    coordination_now: list[object]


def _unit(evidence_id: str, text: str) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path=f"{evidence_id}.md",
        title=f"3buy {evidence_id}",
        text=text,
        sha256="sha256:" + "7" * 64,
    )


def _corpus_index() -> CorpusIndex:
    return CorpusIndex.build(
        (
            _unit("original-support", "三类买点原文规则与结构条件。"),
            _unit("original-counter", "三类买点失效、跌破与风险反例。"),
        )
    )


def _rule_evidence_binding(bundle: _Bundle) -> RuleEvidenceBinding:
    event = bundle.event
    return RuleEvidenceBinding(
        rule_id=event.rule_id,
        rule_card_version=event.rule_card_version,
        rule_card_fingerprint=event.rule_card_fingerprint,
        rule_set_fingerprint=event.rule_set_fingerprint,
        corpus_manifest_fingerprint=event.corpus_manifest_fingerprint,
        algorithm_fingerprint=event.algorithm_fingerprint,
        supporting_evidence_ids=("original-support",),
        counterevidence_ids=("original-counter",),
        image_ids=(),
    )


@pytest.fixture
def review_bundle(
    tmp_path,
    make_bound_decision_event,
    make_rule_evaluation,
    make_risk_context,
):
    database = tmp_path / "llm-review.sqlite3"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False},
    )
    TableByDecisionEvent.__table__.create(engine)
    TableByDecisionTransition.__table__.create(engine)
    TableByDecisionReview.__table__.create(engine)
    TableByLLMReviewClaim.__table__.create(engine)
    TableByLLMReviewAttempt.__table__.create(engine)
    TableByLLMReview.__table__.create(engine)
    TableByRiskSnapshot.__table__.create(engine)
    coordination_now = [None]
    store = DecisionEventStore(
        sessionmaker(bind=engine, expire_on_commit=False),
        clock=lambda: coordination_now[0],
    )
    event = make_bound_decision_event(bs_type="3buy")
    reviewed_at = event.observed_at + timedelta(minutes=1)
    coordination_now[0] = reviewed_at
    bar_clock = _ClosedBarClock()
    event_service = DecisionEventService(
        store,
        bar_clock,
        clock=lambda: reviewed_at + timedelta(days=1),
    )
    registration = event_service.register(
        event,
        make_risk_context(asof=event.observed_at),
        rule_evaluation=make_rule_evaluation(event),
    )
    assert registration.risk is not None
    assert registration.risk.allowed is True
    risk_snapshots = store.list_risk_snapshots(event.event_id)
    assert len(risk_snapshots) == 1
    try:
        yield _Bundle(
            engine=engine,
            event_service=event_service,
            store=store,
            bar_clock=bar_clock,
            event=event,
            risk_snapshot=risk_snapshots[0],
            index=_corpus_index(),
            reviewed_at=reviewed_at,
            coordination_now=coordination_now,
        )
    finally:
        engine.dispose()


def _base_packet(
    bundle: _Bundle,
    provider: _FakeProvider,
    *,
    index=None,
    snapshot: RiskSnapshot | None = None,
):
    resolved = snapshot or bundle.risk_snapshot
    return build_evidence_packet(
        bundle.event,
        resolved.decision,
        bundle.index if index is None else index,
        provider.capabilities,
        rule_evidence_binding=_rule_evidence_binding(bundle),
    )


def _packet(
    bundle: _Bundle,
    provider: _FakeProvider,
    *,
    index=None,
    snapshot: RiskSnapshot | None = None,
):
    resolved = snapshot or bundle.risk_snapshot
    packet = _base_packet(
        bundle,
        provider,
        index=index,
        snapshot=resolved,
    )
    return replace(
        packet,
        packet_fingerprint=sha256_json(
            {
                "evidence_packet_fingerprint": packet.packet_fingerprint,
                "risk_snapshot_id": resolved.snapshot_id,
            }
        ),
    )


def _claim(packet, text: str, *, counter: bool = False) -> dict:
    if counter:
        units = packet.counter_evidence
    else:
        original = next(
            unit
            for unit in packet.supporting
            if unit.source_tier is SourceTier.LESSON_ORIGINAL
        )
        project = next(
            unit
            for unit in packet.supporting
            if unit.source_tier is SourceTier.PROJECT_IMPLEMENTATION
        )
        units = (original, project)
    return {
        "text": text,
        "evidence_ids": [unit.evidence_id for unit in units],
        "source_labels": [unit.source_tier.value for unit in units],
        "supports": not counter,
    }


def _valid_raw(packet, verdict: str = "CONFIRM") -> str:
    payload = {
        "verdict": verdict,
        "strategy_track": packet.event.strategy_track.value,
        "summary": _claim(packet, "结构事实与所引原文规则一致。"),
        "structure_read": [_claim(packet, "结构快照已经完成并可复核。")],
        "bull_case": {
            "claims": [_claim(packet, "支持情形仍然成立。")],
            "conditions": ["所引支持条件仍然存在。"],
            "rank": 1,
        },
        "bear_case": {
            "claims": [_claim(packet, "不利情形需要持续观察。")],
            "conditions": ["所引不利条件需要持续观察。"],
            "rank": 2,
        },
        "invalidation_checks": [_claim(packet, "结构止损是失效边界。")],
        "counter_evidence": [_claim(packet, "原文同时给出失效条件。", counter=True)],
        "risk_acknowledged": True,
        "missing_evidence": [],
        "reviewed_event_id": packet.event.event_id,
        "reviewed_data_fingerprint": packet.event.data_fingerprint,
        "reviewed_packet_fingerprint": packet.packet_fingerprint,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _success(content: str, *, latency_ms: int = 12) -> ProviderResponse:
    return ProviderResponse(
        ok=True,
        provider="fake",
        model="model-v1",
        content=content,
        raw_response=content,
        error_code=None,
        error_message=None,
        retryable=False,
        latency_ms=latency_ms,
        finish_reason="stop",
    )


def _failure(*, retryable: bool = False) -> ProviderResponse:
    return ProviderResponse(
        ok=False,
        provider="fake",
        model="model-v1",
        content=None,
        raw_response="safe provider failure",
        error_code="network_timeout" if retryable else "http_error",
        error_message="bounded safe failure",
        retryable=retryable,
        latency_ms=7,
    )


_DEFAULT_RULE_EVIDENCE_RESOLVER = object()


def _service(
    bundle: _Bundle,
    provider: LLMProvider,
    *,
    index: CorpusIndex | None = None,
    clock=None,
    snapshot: RiskSnapshot | None = None,
    rule_evidence_resolver=_DEFAULT_RULE_EVIDENCE_RESOLVER,
) -> ReviewService:
    resolved = snapshot or bundle.risk_snapshot
    return ReviewService(
        bundle.event_service,
        bundle.index if index is None else index,
        lambda event: resolved,
        provider,
        rule_evidence_resolver=(
            (lambda event: _rule_evidence_binding(bundle))
            if rule_evidence_resolver is _DEFAULT_RULE_EVIDENCE_RESOLVER
            else rule_evidence_resolver
        ),
        clock=clock or (lambda: bundle.reviewed_at),
    )


def _claim_count(bundle: _Bundle) -> int:
    with bundle.engine.connect() as connection:
        return int(
            connection.scalar(
                select(func.count()).select_from(TableByLLMReviewClaim)
            )
            or 0
        )


def _tampered_snapshot(
    snapshot: RiskSnapshot,
    field_name: str,
    value: object,
) -> RiskSnapshot:
    tampered = replace(snapshot)
    object.__setattr__(tampered, field_name, value)
    return tampered


def test_legacy_event_is_rejected_before_risk_evidence_or_provider_calls(
    review_bundle,
    make_decision_event,
) -> None:
    legacy = make_decision_event()
    review_bundle.store.append_event(legacy)
    provider = _FakeProvider(())

    def forbidden_risk(_event):
        raise AssertionError("legacy event must be rejected before risk resolution")

    service = ReviewService(
        review_bundle.event_service,
        review_bundle.index,
        forbidden_risk,
        provider,
        clock=lambda: review_bundle.reviewed_at,
    )

    with pytest.raises(InvalidEventTransition, match="legacy-unbound"):
        service.review_event(legacy.event_id)

    assert provider.call_count == 0


def test_review_service_rejects_other_strategy_run_before_risk_or_provider(
    review_bundle,
) -> None:
    provider = _FakeProvider(())

    def forbidden_risk(_event):
        raise AssertionError("wrong-run event must not reach risk resolution")

    service = ReviewService(
        review_bundle.event_service,
        review_bundle.index,
        forbidden_risk,
        provider,
        clock=lambda: review_bundle.reviewed_at,
    )
    active = _StrategyRunCapability()
    service.bind_strategy_run(active)

    with pytest.raises(InvalidEventTransition, match="strategy run"):
        service.review_event(review_bundle.event.event_id)

    assert provider.call_count == 0
    assert _claim_count(review_bundle) == 0


def test_bare_risk_decision_is_rejected_before_claim_or_provider(
    review_bundle,
) -> None:
    provider = _FakeProvider(())
    service = ReviewService(
        review_bundle.event_service,
        review_bundle.index,
        lambda event: review_bundle.risk_snapshot.decision,
        provider,
        clock=lambda: review_bundle.reviewed_at,
    )

    with pytest.raises(TypeError, match="risk_resolver must return RiskSnapshot"):
        service.review_event(review_bundle.event.event_id)

    assert provider.call_count == 0
    assert _claim_count(review_bundle) == 0
    assert review_bundle.store.count_llm_reviews(review_bundle.event.event_id) == 0


@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    (
        ("event_id", "wrong:event", "event_id_mismatch"),
        (
            "event_data_fingerprint",
            "sha256:" + "a" * 64,
            "event_data_fingerprint_mismatch",
        ),
        (
            "evaluation_input_fingerprint",
            "sha256:" + "b" * 64,
            "evaluation_input_fingerprint_mismatch",
        ),
        ("rule_id", "chanlun.wrong", "event_rule_id_mismatch"),
        (
            "rule_card_version",
            999,
            "event_rule_card_version_mismatch",
        ),
        (
            "rule_card_fingerprint",
            "sha256:" + "c" * 64,
            "event_rule_card_fingerprint_mismatch",
        ),
        (
            "rule_set_fingerprint",
            "sha256:" + "d" * 64,
            "event_rule_set_fingerprint_mismatch",
        ),
        (
            "corpus_manifest_fingerprint",
            "sha256:" + "e" * 64,
            "event_corpus_manifest_fingerprint_mismatch",
        ),
        (
            "algorithm_fingerprint",
            "sha256:" + "f" * 64,
            "event_algorithm_fingerprint_mismatch",
        ),
    ),
)
def test_snapshot_binding_mismatch_fails_before_claim_or_provider(
    review_bundle,
    field_name,
    value,
    reason,
) -> None:
    provider = _FakeProvider(())
    snapshot = _tampered_snapshot(
        review_bundle.risk_snapshot,
        field_name,
        value,
    )

    with pytest.raises(InvalidEventTransition, match=reason):
        _service(
            review_bundle,
            provider,
            snapshot=snapshot,
        ).review_event(review_bundle.event.event_id)

    assert provider.call_count == 0
    assert _claim_count(review_bundle) == 0
    assert review_bundle.store.count_llm_reviews(review_bundle.event.event_id) == 0
    assert (
        review_bundle.event_service.get(review_bundle.event.event_id).state
        is EventState.REVIEW_PENDING
    )


@pytest.mark.parametrize(
    ("failure", "reason"),
    (
        ("expired", "risk_snapshot_expired"),
        ("not_yet_effective", "risk_snapshot_not_yet_effective"),
        ("not_allowed", "risk_decision_not_allowed"),
    ),
)
def test_unusable_snapshot_fails_before_claim_or_provider(
    review_bundle,
    failure,
    reason,
) -> None:
    snapshot = review_bundle.risk_snapshot
    if failure == "expired":
        snapshot = replace(snapshot, expires_at=review_bundle.reviewed_at)
    elif failure == "not_yet_effective":
        evaluated_at = review_bundle.reviewed_at + timedelta(minutes=1)
        decision = replace(snapshot.decision, evaluated_at=evaluated_at)
        snapshot = RiskSnapshot.capture(
            event=review_bundle.event,
            evaluation_input_fingerprint=review_bundle.event.data_fingerprint,
            decision=decision,
            observed_at=review_bundle.event.observed_at,
            expires_at=evaluated_at + timedelta(minutes=5),
        )
    else:
        decision = replace(
            snapshot.decision,
            allowed=False,
            shares=0,
            planned_risk_cash=Decimal("0"),
            reasons=("risk_policy_blocked",),
        )
        snapshot = replace(snapshot, decision=decision)
    provider = _FakeProvider(())

    with pytest.raises(InvalidEventTransition, match=reason):
        _service(
            review_bundle,
            provider,
            snapshot=snapshot,
        ).review_event(review_bundle.event.event_id)

    assert provider.call_count == 0
    assert _claim_count(review_bundle) == 0
    assert review_bundle.store.count_llm_reviews(review_bundle.event.event_id) == 0


def test_valid_snapshot_passes_only_decision_and_binds_snapshot_identity(
    review_bundle,
    monkeypatch,
) -> None:
    snapshot = review_bundle.risk_snapshot
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider, snapshot=snapshot)
    provider = _FakeProvider((_success(_valid_raw(packet)),))
    original = review_service_module.build_evidence_packet
    captured_risks = []

    def recording_builder(
        event,
        risk,
        index,
        capabilities,
        *,
        max_units,
        rule_evidence_binding,
    ):
        captured_risks.append(risk)
        return original(
            event,
            risk,
            index,
            capabilities,
            max_units=max_units,
            rule_evidence_binding=rule_evidence_binding,
        )

    monkeypatch.setattr(
        review_service_module,
        "build_evidence_packet",
        recording_builder,
    )

    stored = _service(review_bundle, provider, snapshot=snapshot).review_event(
        review_bundle.event.event_id
    )
    base_packet = _base_packet(review_bundle, empty_provider, snapshot=snapshot)
    expected_identity = sha256_json(
        {
            "evidence_packet_fingerprint": base_packet.packet_fingerprint,
            "risk_snapshot_id": snapshot.snapshot_id,
        }
    )

    assert captured_risks == [snapshot.decision]
    assert stored.risk_snapshot_id == snapshot.snapshot_id
    assert stored.packet_fingerprint == expected_identity
    assert stored.packet_fingerprint != base_packet.packet_fingerprint
    assert provider.call_count == 1


def test_existing_review_replay_revalidates_latest_snapshot(
    review_bundle,
) -> None:
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider)
    provider = _FakeProvider((_success(_valid_raw(packet)),))
    service = _service(review_bundle, provider)
    stored = service.review_event(review_bundle.event.event_id)
    claim_count = _claim_count(review_bundle)
    expired_at = review_bundle.reviewed_at + timedelta(minutes=1)
    expired = replace(
        review_bundle.risk_snapshot,
        expires_at=expired_at,
    )

    with pytest.raises(InvalidEventTransition, match="risk_snapshot_expired"):
        _service(
            review_bundle,
            provider,
            snapshot=expired,
            clock=lambda: expired_at,
        ).review_event(review_bundle.event.event_id)

    assert provider.call_count == 1
    assert _claim_count(review_bundle) == claim_count
    assert review_bundle.store.count_llm_reviews(stored.event_id) == 1


def _manual_claim(
    bundle: _Bundle,
    packet,
    *,
    review_id: str,
    owner_token: str,
) -> object:
    claim = bundle.store.acquire_llm_review_claim(
        review_id=review_id,
        event_id=bundle.event.event_id,
        packet_fingerprint=packet.packet_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        owner_token=owner_token,
        now=bundle.reviewed_at,
        lease_expires_at=bundle.reviewed_at + timedelta(minutes=10),
    )
    assert claim.acquired is True
    return claim


def test_same_event_packet_and_model_is_reviewed_once(review_bundle) -> None:
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider)
    provider = _FakeProvider((_success(_valid_raw(packet)),))
    service = _service(review_bundle, provider)

    first = service.review_event(review_bundle.event.event_id)
    second = service.review_event(review_bundle.event.event_id)

    assert first.review_id == second.review_id
    assert first.status == "validated"
    assert first.ok is True
    assert provider.call_count == 1
    assert review_bundle.store.count_llm_reviews(review_bundle.event.event_id) == 1
    assert review_bundle.store.count_llm_review_attempts(first.review_id) == 1
    assert review_bundle.event_service.get(first.event_id).state is EventState.CONFIRMED


def test_provider_failure_is_persisted_and_event_not_confirmed(review_bundle) -> None:
    provider = _FakeProvider((_failure(),))
    service = _service(review_bundle, provider)

    review = service.review_event(review_bundle.event.event_id)

    assert review.ok is False
    assert review.status == "provider_failed"
    assert review.verdict == "ABSTAIN"
    assert review.raw_response == "safe provider failure"
    assert review.error_code == "http_error"
    assert review_bundle.event_service.get(review.event_id).state is EventState.ABSTAINED


def test_offline_review_persists_abstain_without_confirm_transition(
    review_bundle,
) -> None:
    review = _service(
        review_bundle,
        OfflineAbstainProvider(),
    ).review_event(review_bundle.event.event_id)

    assert review.status == "provider_failed"
    assert review.verdict == "ABSTAIN"
    assert review.error_code == "offline_review_mode"
    assert review.attempt_count == 1
    assert review_bundle.store.count_llm_review_attempts(review.review_id) == 1
    assert (
        review_bundle.event_service.get(review.event_id).state
        is EventState.ABSTAINED
    )


def test_retryable_provider_failure_retries_only_once(review_bundle) -> None:
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider)
    provider = _FakeProvider(
        (_failure(retryable=True), _success(_valid_raw(packet)))
    )

    review = _service(review_bundle, provider).review_event(
        review_bundle.event.event_id
    )

    assert review.status == "validated"
    assert review.attempt_count == 2
    assert review.latency_ms == 19
    assert provider.call_count == 2
    attempts = review_bundle.store.list_llm_review_attempts(review.review_id)
    assert len(attempts) == 2
    assert attempts[0].ok is False
    assert attempts[1].ok is True


def test_retryable_provider_failure_stops_after_second_failure(
    review_bundle,
) -> None:
    provider = _FakeProvider((_failure(retryable=True),))

    review = _service(review_bundle, provider).review_event(
        review_bundle.event.event_id
    )

    assert review.status == "provider_failed"
    assert review.attempt_count == 2
    assert review.latency_ms == 14
    assert provider.call_count == 2
    assert review_bundle.store.count_llm_review_attempts(review.review_id) == 2


def test_blocked_packet_abstains_without_calling_provider(review_bundle) -> None:
    provider = _FakeProvider(())
    empty_index = CorpusIndex.build(())

    review = _service(
        review_bundle,
        provider,
        index=empty_index,
    ).review_event(review_bundle.event.event_id)

    assert provider.call_count == 0
    assert review.status == "local_abstain"
    assert review.verdict == "ABSTAIN"
    assert review.error_code == "packet_blocked"
    assert "missing_original_evidence" in review.validation_errors
    assert review_bundle.event_service.get(review.event_id).state is EventState.ABSTAINED


def test_bound_event_without_rule_evidence_resolver_abstains_locally(
    review_bundle,
) -> None:
    provider = _FakeProvider(())

    review = _service(
        review_bundle,
        provider,
        rule_evidence_resolver=None,
    ).review_event(review_bundle.event.event_id)

    assert provider.call_count == 0
    assert review.status == "local_abstain"
    assert review.verdict == "ABSTAIN"
    assert "missing_rule_evidence_binding" in review.validation_errors
    assert review_bundle.event_service.get(review.event_id).state is EventState.ABSTAINED


def test_image_hash_mismatch_abstains_before_provider_call(
    review_bundle,
    tmp_path,
) -> None:
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"tampered-chart")
    expected_payload = b"trusted-chart"
    image = ImageEvidence(
        image_id="chart-1",
        source_tier=SourceTier.LESSON_CHART,
        source_path=str(image_path),
        sha256="sha256:" + hashlib.sha256(expected_payload).hexdigest(),
        media_type="image/png",
        width=20,
        height=20,
    )
    support = EvidenceUnit(
        evidence_id="original-image-support",
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path="image-support.md",
        title="3buy chart support",
        text="三类买点原文图表规则。",
        sha256="sha256:" + "8" * 64,
        image_ids=(image.image_id,),
    )
    index = CorpusIndex.build(
        (
            support,
            _unit("original-counter", "三类买点失效与风险反例。"),
        ),
        (image,),
    )
    provider = _FakeProvider(())
    provider.capabilities = ModelCapabilities(
        supports_images=True,
        supports_json_schema=True,
    )
    binding = replace(
        _rule_evidence_binding(review_bundle),
        supporting_evidence_ids=(support.evidence_id,),
        image_ids=(image.image_id,),
    )

    review = _service(
        review_bundle,
        provider,
        index=index,
        rule_evidence_resolver=lambda event: binding,
    ).review_event(review_bundle.event.event_id)

    assert provider.call_count == 0
    assert review.status == "local_abstain"
    assert review.error_code == "image_load_failed"
    assert review.validation_errors == ("image_evidence_unavailable",)
    assert review_bundle.event_service.get(review.event_id).state is EventState.ABSTAINED


def test_invalid_json_retains_raw_response_and_abstains(review_bundle) -> None:
    provider = _FakeProvider((_success("{not-json"),))

    review = _service(review_bundle, provider).review_event(
        review_bundle.event.event_id
    )

    assert review.status == "validation_failed"
    assert review.raw_response == "{not-json"
    assert review.parsed_response_json is None
    assert "invalid_json" in review.validation_errors
    assert review_bundle.event_service.get(review.event_id).state is EventState.ABSTAINED


def test_provider_content_and_raw_envelope_are_both_audited(
    review_bundle,
) -> None:
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider)
    content = _valid_raw(packet)
    response = ProviderResponse(
        ok=True,
        provider="fake",
        model="model-v1",
        content=content,
        raw_response="provider-envelope",
        error_code=None,
        error_message=None,
        retryable=False,
        latency_ms=3,
        finish_reason="stop",
    )

    review = _service(
        review_bundle,
        _FakeProvider((response,)),
    ).review_event(review_bundle.event.event_id)

    assert review.response_content == content
    assert review.raw_response == "provider-envelope"
    attempt = review_bundle.store.list_llm_review_attempts(review.review_id)[0]
    assert attempt.response_content == content
    assert attempt.raw_response == "provider-envelope"
    assert review_bundle.event_service.get(review.event_id).state is EventState.CONFIRMED


def test_stale_response_is_audit_only(review_bundle) -> None:
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider)
    payload = json.loads(_valid_raw(packet))
    payload["reviewed_data_fingerprint"] = "sha256:" + "f" * 64
    provider = _FakeProvider(
        (_success(json.dumps(payload, ensure_ascii=False)),)
    )

    review = _service(review_bundle, provider).review_event(
        review_bundle.event.event_id
    )

    assert review.status == "validation_failed"
    assert "stale_data_fingerprint" in review.validation_errors
    assert review_bundle.event_service.get(review.event_id).state is EventState.REVIEW_PENDING
    assert review_bundle.store.count_reviews(review.event_id) == 0
    assert review_bundle.store.count_llm_reviews(review.event_id) == 1


def test_slow_provider_crossing_freshness_boundary_cannot_confirm(
    review_bundle,
) -> None:
    empty_provider = _FakeProvider(())
    snapshot = replace(
        review_bundle.risk_snapshot,
        expires_at=review_bundle.event.observed_at + timedelta(minutes=30),
    )
    packet = _packet(review_bundle, empty_provider, snapshot=snapshot)
    current = [review_bundle.reviewed_at]
    review_bundle.bar_clock.closed_at = tuple(
        review_bundle.event.observed_at + timedelta(minutes=minutes)
        for minutes in (5, 10, 15)
    )

    def finish_after_boundary() -> None:
        current[0] = review_bundle.event.observed_at + timedelta(minutes=16)

    provider = _FakeProvider(
        (_success(_valid_raw(packet)),),
        after_response=finish_after_boundary,
    )

    review = _service(
        review_bundle,
        provider,
        clock=lambda: current[0],
        snapshot=snapshot,
    ).review_event(review_bundle.event.event_id)

    assert review.status == "validated"
    assert review_bundle.event_service.get(review.event_id).state is EventState.EXPIRED
    lifecycle_review = review_bundle.store.list_reviews(review.event_id)[0]
    assert lifecycle_review.applied is False
    assert lifecycle_review.state is EventState.EXPIRED


def test_provider_response_after_risk_expiry_is_audit_only(
    review_bundle,
) -> None:
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider)
    current = [review_bundle.reviewed_at]

    def finish_at_risk_expiry() -> None:
        current[0] = review_bundle.risk_snapshot.expires_at

    provider = _FakeProvider(
        (_success(_valid_raw(packet)),),
        after_response=finish_at_risk_expiry,
    )

    review = _service(
        review_bundle,
        provider,
        clock=lambda: current[0],
    ).review_event(review_bundle.event.event_id)

    assert provider.call_count == 1
    assert review.status == "validation_failed"
    assert review.verdict == "ABSTAIN"
    assert "runtime_risk:risk_snapshot_expired" in review.validation_errors
    assert review_bundle.event_service.get(review.event_id).state is EventState.REVIEW_PENDING
    assert review_bundle.store.count_reviews(review.event_id) == 0
    assert review_bundle.store.count_llm_reviews(review.event_id) == 1


def test_store_rejects_executable_verdict_for_failure_status(
    review_bundle,
) -> None:
    provider = _FakeProvider(())
    packet = _packet(review_bundle, provider)
    review_id = "manual-failure-review"
    owner_token = "a" * 32
    claim = _manual_claim(
        review_bundle,
        packet,
        review_id=review_id,
        owner_token=owner_token,
    )

    with pytest.raises(ValueError, match="failed review verdict must be ABSTAIN"):
        review_bundle.store.append_llm_review(
            review_id=review_id,
            owner_token=owner_token,
            fencing_token=claim.fencing_token,
            event_id=review_bundle.event.event_id,
            risk_snapshot_id=review_bundle.risk_snapshot.snapshot_id,
            packet_fingerprint=packet.packet_fingerprint,
            reviewed_data_fingerprint=review_bundle.event.data_fingerprint,
            provider="fake",
            model="model-v1",
            prompt_version=PROMPT_VERSION,
            status="provider_failed",
            provider_ok=False,
            verdict="CONFIRM",
            response_content=None,
            raw_response="failure",
            parsed_response_json=None,
            validation_errors=("provider_error:http_error",),
            attempt_count=1,
            latency_ms=1,
            error_code="http_error",
            error_message="safe failure",
            created_at=review_bundle.reviewed_at,
        )


def test_store_rejects_executable_verdict_for_validation_failure(
    review_bundle,
) -> None:
    provider = _FakeProvider(())
    packet = _packet(review_bundle, provider)
    raw = _valid_raw(packet)
    review_id = "manual-validation-failure"
    owner_token = "c" * 32
    claim = _manual_claim(
        review_bundle,
        packet,
        review_id=review_id,
        owner_token=owner_token,
    )
    review_bundle.store.append_llm_review_attempt(
        attempt_id="manual-validation-failure-attempt",
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        owner_token=owner_token,
        fencing_token=claim.fencing_token,
        attempt_number=1,
        provider="fake",
        model="model-v1",
        ok=True,
        retryable=False,
        response_content=raw,
        raw_response=raw,
        error_code=None,
        error_message=None,
        latency_ms=1,
        started_at=review_bundle.reviewed_at,
        completed_at=review_bundle.reviewed_at,
    )

    with pytest.raises(ValueError, match="non-validated review verdict"):
        review_bundle.store.append_llm_review(
            review_id=review_id,
            owner_token=owner_token,
            fencing_token=claim.fencing_token,
            event_id=review_bundle.event.event_id,
            risk_snapshot_id=review_bundle.risk_snapshot.snapshot_id,
            packet_fingerprint=packet.packet_fingerprint,
            reviewed_data_fingerprint=review_bundle.event.data_fingerprint,
            provider="fake",
            model="model-v1",
            prompt_version=PROMPT_VERSION,
            status="validation_failed",
            provider_ok=True,
            verdict="CONFIRM",
            response_content=raw,
            raw_response=raw,
            parsed_response_json="{}",
            validation_errors=("fake_evidence_id",),
            attempt_count=1,
            latency_ms=1,
            error_code=None,
            error_message=None,
            created_at=review_bundle.reviewed_at,
        )


def test_stored_executable_review_is_reparsed_before_transition(
    review_bundle,
) -> None:
    provider = _FakeProvider(())
    packet = _packet(review_bundle, provider)
    raw = _valid_raw(packet)
    parsed = parse_review(raw, packet)
    review_id = "manual-forged-review"
    owner_token = "b" * 32
    claim = _manual_claim(
        review_bundle,
        packet,
        review_id=review_id,
        owner_token=owner_token,
    )
    review_bundle.store.append_llm_review_attempt(
        attempt_id="manual-attempt",
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        owner_token=owner_token,
        fencing_token=claim.fencing_token,
        attempt_number=1,
        provider="fake",
        model="model-v1",
        ok=True,
        retryable=False,
        response_content=raw,
        raw_response=raw,
        error_code=None,
        error_message=None,
        latency_ms=12,
        started_at=review_bundle.reviewed_at,
        completed_at=review_bundle.reviewed_at,
    )
    review_bundle.store.append_llm_review(
        review_id=review_id,
        owner_token=owner_token,
        fencing_token=claim.fencing_token,
        event_id=review_bundle.event.event_id,
        risk_snapshot_id=review_bundle.risk_snapshot.snapshot_id,
        packet_fingerprint=packet.packet_fingerprint,
        reviewed_data_fingerprint=review_bundle.event.data_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        status="validated",
        provider_ok=True,
        verdict="REJECT",
        response_content=raw,
        raw_response=raw,
        parsed_response_json=parsed.parsed_response_json,
        validation_errors=(),
        attempt_count=1,
        latency_ms=12,
        error_code=None,
        error_message=None,
        created_at=review_bundle.reviewed_at,
    )

    with pytest.raises(EventConflictError, match="strict revalidation"):
        _service(review_bundle, provider).review_event(
            review_bundle.event.event_id
        )

    assert provider.call_count == 0
    assert (
        review_bundle.event_service.get(review_bundle.event.event_id).state
        is EventState.REVIEW_PENDING
    )


def test_raw_review_is_committed_before_event_transition(
    review_bundle, monkeypatch
) -> None:
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider)
    provider = _FakeProvider((_success(_valid_raw(packet)),))
    original_apply = review_bundle.event_service.apply_review

    def apply_after_audit(review):
        assert review_bundle.store.count_llm_reviews(review.reviewed_event_id) == 1
        return original_apply(review)

    monkeypatch.setattr(
        review_bundle.event_service,
        "apply_review",
        apply_after_audit,
    )

    _service(review_bundle, provider).review_event(review_bundle.event.event_id)


def test_concurrent_duplicate_creates_one_identity_row(review_bundle) -> None:
    empty_provider = _FakeProvider(())
    packet = _packet(review_bundle, empty_provider)
    entered = Event()
    release = Event()
    provider = _FakeProvider(
        (_success(_valid_raw(packet)),),
        entered=entered,
        release=release,
    )
    service = _service(review_bundle, provider)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            service.review_event,
            review_bundle.event.event_id,
        )
        assert entered.wait(timeout=10)
        second = pool.submit(
            service.review_event,
            review_bundle.event.event_id,
        )
        release.set()
        reviews = (first.result(timeout=10), second.result(timeout=10))

    assert len({review.review_id for review in reviews}) == 1
    assert review_bundle.store.count_llm_reviews(review_bundle.event.event_id) == 1
    assert provider.call_count == 1
    assert review_bundle.store.count_llm_review_attempts(reviews[0].review_id) == 1


def test_expired_claim_owner_cannot_finalize_after_takeover(
    review_bundle,
) -> None:
    provider = _FakeProvider(())
    packet = _packet(review_bundle, provider)
    review_id = "lease-takeover-review"
    first_owner = "a" * 32
    second_owner = "b" * 32
    started = review_bundle.reviewed_at
    first = review_bundle.store.acquire_llm_review_claim(
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        packet_fingerprint=packet.packet_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        owner_token=first_owner,
        now=started,
        lease_expires_at=started + timedelta(seconds=1),
    )
    assert first.acquired is True
    review_bundle.coordination_now[0] = started + timedelta(seconds=2)
    second = review_bundle.store.acquire_llm_review_claim(
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        packet_fingerprint=packet.packet_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        owner_token=second_owner,
        now=started + timedelta(seconds=2),
        lease_expires_at=started + timedelta(minutes=10),
    )
    assert second.acquired is True
    lost = review_bundle.store.acquire_llm_review_claim(
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        packet_fingerprint=packet.packet_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        owner_token=first_owner,
        now=started + timedelta(seconds=2),
        lease_expires_at=started + timedelta(minutes=20),
    )
    assert lost.acquired is False
    assert lost.owner_token == second_owner

    with pytest.raises(LLMReviewClaimLostError, match="not owned"):
        review_bundle.store.append_llm_review(
            review_id=review_id,
            owner_token=first_owner,
            fencing_token=first.fencing_token,
            event_id=review_bundle.event.event_id,
            risk_snapshot_id=review_bundle.risk_snapshot.snapshot_id,
            packet_fingerprint=packet.packet_fingerprint,
            reviewed_data_fingerprint=review_bundle.event.data_fingerprint,
            provider="fake",
            model="model-v1",
            prompt_version=PROMPT_VERSION,
            status="local_abstain",
            provider_ok=False,
            verdict="ABSTAIN",
            response_content=None,
            raw_response="",
            parsed_response_json=None,
            validation_errors=("claim_lost",),
            attempt_count=0,
            latency_ms=0,
            error_code="claim_lost",
            error_message="claim lost",
            created_at=started + timedelta(seconds=2),
        )


def test_stale_fencing_token_cannot_finalize_after_owner_aba(
    review_bundle,
) -> None:
    provider = _FakeProvider(())
    packet = _packet(review_bundle, provider)
    review_id = "lease-aba-review"
    first_owner = "1" * 32
    second_owner = "2" * 32
    started = review_bundle.reviewed_at
    first = review_bundle.store.acquire_llm_review_claim(
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        packet_fingerprint=packet.packet_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        owner_token=first_owner,
        now=started,
        lease_expires_at=started + timedelta(seconds=1),
    )
    review_bundle.coordination_now[0] = started + timedelta(seconds=2)
    second = review_bundle.store.acquire_llm_review_claim(
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        packet_fingerprint=packet.packet_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        owner_token=second_owner,
        now=started + timedelta(seconds=2),
        lease_expires_at=started + timedelta(seconds=3),
    )
    review_bundle.coordination_now[0] = started + timedelta(seconds=4)
    third = review_bundle.store.acquire_llm_review_claim(
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        packet_fingerprint=packet.packet_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        owner_token=first_owner,
        now=started + timedelta(seconds=4),
        lease_expires_at=started + timedelta(minutes=10),
    )

    assert first.fencing_token == 1
    assert second.fencing_token == 2
    assert third.fencing_token == 3
    assert third.acquired is True
    with pytest.raises(LLMReviewClaimLostError, match="not owned"):
        review_bundle.store.append_llm_review(
            review_id=review_id,
            owner_token=first_owner,
            fencing_token=first.fencing_token,
            event_id=review_bundle.event.event_id,
            risk_snapshot_id=review_bundle.risk_snapshot.snapshot_id,
            packet_fingerprint=packet.packet_fingerprint,
            reviewed_data_fingerprint=review_bundle.event.data_fingerprint,
            provider="fake",
            model="model-v1",
            prompt_version=PROMPT_VERSION,
            status="local_abstain",
            provider_ok=False,
            verdict="ABSTAIN",
            response_content=None,
            raw_response="",
            parsed_response_json=None,
            validation_errors=("stale_fence",),
            attempt_count=0,
            latency_ms=0,
            error_code="stale_fence",
            error_message="stale fencing token",
            created_at=started + timedelta(seconds=4),
        )
    for attempt_id, fencing_token in (
        ("aba-attempt-old", first.fencing_token),
        ("aba-attempt-current", third.fencing_token),
    ):
        review_bundle.store.append_llm_review_attempt(
            attempt_id=attempt_id,
            review_id=review_id,
            event_id=review_bundle.event.event_id,
            owner_token=first_owner,
            fencing_token=fencing_token,
            attempt_number=1,
            provider="fake",
            model="model-v1",
            ok=False,
            retryable=False,
            response_content=None,
            raw_response=attempt_id,
            error_code="late_attempt",
            error_message="late attempt retained for audit",
            latency_ms=1,
            started_at=started + timedelta(seconds=4),
            completed_at=started + timedelta(seconds=4),
        )

    attempts = review_bundle.store.list_llm_review_attempts(review_id)
    assert tuple(attempt.fencing_token for attempt in attempts) == (1, 3)


def test_old_created_at_cannot_bypass_expired_claim_lease(review_bundle) -> None:
    provider = _FakeProvider(())
    packet = _packet(review_bundle, provider)
    review_id = "expired-created-at-review"
    owner_token = "d" * 32
    claim = _manual_claim(
        review_bundle,
        packet,
        review_id=review_id,
        owner_token=owner_token,
    )
    review_bundle.coordination_now[0] = review_bundle.reviewed_at + timedelta(
        minutes=11
    )

    with pytest.raises(LLMReviewClaimLostError, match="lease expired"):
        review_bundle.store.append_llm_review(
            review_id=review_id,
            owner_token=owner_token,
            fencing_token=claim.fencing_token,
            event_id=review_bundle.event.event_id,
            risk_snapshot_id=review_bundle.risk_snapshot.snapshot_id,
            packet_fingerprint=packet.packet_fingerprint,
            reviewed_data_fingerprint=review_bundle.event.data_fingerprint,
            provider="fake",
            model="model-v1",
            prompt_version=PROMPT_VERSION,
            status="local_abstain",
            provider_ok=False,
            verdict="ABSTAIN",
            response_content=None,
            raw_response="",
            parsed_response_json=None,
            validation_errors=("lease_expired",),
            attempt_count=0,
            latency_ms=0,
            error_code="lease_expired",
            error_message="lease expired",
            created_at=review_bundle.reviewed_at,
        )


def test_finalized_claim_cannot_be_taken_over(review_bundle) -> None:
    provider = _FakeProvider(())
    packet = _packet(review_bundle, provider)
    review_id = "finalized-claim-review"
    first_owner = "e" * 32
    claim = _manual_claim(
        review_bundle,
        packet,
        review_id=review_id,
        owner_token=first_owner,
    )
    review_bundle.store.append_llm_review(
        review_id=review_id,
        owner_token=first_owner,
        fencing_token=claim.fencing_token,
        event_id=review_bundle.event.event_id,
        risk_snapshot_id=review_bundle.risk_snapshot.snapshot_id,
        packet_fingerprint=packet.packet_fingerprint,
        reviewed_data_fingerprint=review_bundle.event.data_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        status="local_abstain",
        provider_ok=False,
        verdict="ABSTAIN",
        response_content=None,
        raw_response="",
        parsed_response_json=None,
        validation_errors=("manual_abstain",),
        attempt_count=0,
        latency_ms=0,
        error_code="manual_abstain",
        error_message="manual abstain",
        created_at=review_bundle.reviewed_at,
    )
    review_bundle.coordination_now[0] = review_bundle.reviewed_at + timedelta(
        minutes=11
    )

    takeover = review_bundle.store.acquire_llm_review_claim(
        review_id=review_id,
        event_id=review_bundle.event.event_id,
        packet_fingerprint=packet.packet_fingerprint,
        provider="fake",
        model="model-v1",
        prompt_version=PROMPT_VERSION,
        owner_token="f" * 32,
        now=review_bundle.coordination_now[0],
        lease_expires_at=review_bundle.coordination_now[0]
        + timedelta(minutes=10),
    )

    assert takeover.acquired is False
    assert review_bundle.store.count_llm_reviews(review_bundle.event.event_id) == 1


def test_final_insert_and_claim_finalization_are_atomic(review_bundle) -> None:
    provider = _FakeProvider(())
    packet = _packet(review_bundle, provider)
    review_id = "atomic-final-review"
    owner_token = "7" * 32
    claim = _manual_claim(
        review_bundle,
        packet,
        review_id=review_id,
        owner_token=owner_token,
    )
    insert_started = Event()
    release_insert = Event()

    def pause_final_insert(
        conn,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        if statement.lstrip().startswith("INSERT INTO cl_decision_llm_review "):
            insert_started.set()
            assert release_insert.wait(timeout=10)

    sqlalchemy_event.listen(
        review_bundle.engine,
        "before_cursor_execute",
        pause_final_insert,
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            final = pool.submit(
                review_bundle.store.append_llm_review,
                review_id=review_id,
                owner_token=owner_token,
                fencing_token=claim.fencing_token,
                event_id=review_bundle.event.event_id,
                risk_snapshot_id=review_bundle.risk_snapshot.snapshot_id,
                packet_fingerprint=packet.packet_fingerprint,
                reviewed_data_fingerprint=review_bundle.event.data_fingerprint,
                provider="fake",
                model="model-v1",
                prompt_version=PROMPT_VERSION,
                status="local_abstain",
                provider_ok=False,
                verdict="ABSTAIN",
                response_content=None,
                raw_response="",
                parsed_response_json=None,
                validation_errors=("manual_abstain",),
                attempt_count=0,
                latency_ms=0,
                error_code="manual_abstain",
                error_message="manual abstain",
                created_at=review_bundle.reviewed_at,
            )
            assert insert_started.wait(timeout=10)
            review_bundle.coordination_now[0] = (
                review_bundle.reviewed_at + timedelta(minutes=11)
            )
            takeover = pool.submit(
                review_bundle.store.acquire_llm_review_claim,
                review_id=review_id,
                event_id=review_bundle.event.event_id,
                packet_fingerprint=packet.packet_fingerprint,
                provider="fake",
                model="model-v1",
                prompt_version=PROMPT_VERSION,
                owner_token="8" * 32,
                now=review_bundle.coordination_now[0],
                lease_expires_at=review_bundle.coordination_now[0]
                + timedelta(minutes=10),
            )
            release_insert.set()
            stored = final.result(timeout=10)
            takeover_claim = takeover.result(timeout=10)
    finally:
        release_insert.set()
        sqlalchemy_event.remove(
            review_bundle.engine,
            "before_cursor_execute",
            pause_final_insert,
        )

    assert stored.review_id == review_id
    assert takeover_claim.acquired is False
    assert review_bundle.store.count_llm_reviews(review_bundle.event.event_id) == 1


def test_oversized_provider_response_is_bounded_with_hash_audit(
    review_bundle,
) -> None:
    provider_without_response = _FakeProvider(())
    packet = _packet(review_bundle, provider_without_response)
    raw = _valid_raw(packet) + (" " * (1024 * 1024))
    provider = _FakeProvider((_success(raw),))

    review = _service(review_bundle, provider).review_event(
        review_bundle.event.event_id
    )
    attempt = review_bundle.store.list_llm_review_attempts(review.review_id)[0]
    expected_hash = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    assert review.status == "provider_failed"
    assert review.verdict == "ABSTAIN"
    assert review.error_code == "response_too_large"
    assert len(review.raw_response.encode("utf-8")) <= 1024 * 1024
    assert review.raw_response_bytes == len(raw.encode("utf-8"))
    assert review.raw_response_sha256 == expected_hash
    assert review.raw_response_truncated is True
    assert len(attempt.response_content.encode("utf-8")) <= 1024 * 1024
    assert attempt.response_content_bytes == len(raw.encode("utf-8"))
    assert attempt.response_content_sha256 == expected_hash
    assert attempt.response_content_truncated is True
    assert len(attempt.raw_response.encode("utf-8")) <= 1024 * 1024
    assert attempt.raw_response_bytes == len(raw.encode("utf-8"))
    assert attempt.raw_response_sha256 == expected_hash
    assert attempt.raw_response_truncated is True


def test_oversized_provider_error_is_bounded_with_hash_audit(
    review_bundle,
) -> None:
    error_message = "x" * (8 * 1024 + 1)
    provider = _FakeProvider(
        (
            ProviderResponse(
                ok=False,
                provider="fake",
                model="model-v1",
                content=None,
                raw_response="bounded raw envelope",
                error_code="http_error",
                error_message=error_message,
                retryable=False,
                latency_ms=1,
            ),
        )
    )

    review = _service(review_bundle, provider).review_event(
        review_bundle.event.event_id
    )
    attempt = review_bundle.store.list_llm_review_attempts(review.review_id)[0]
    expected_hash = "sha256:" + hashlib.sha256(
        error_message.encode("utf-8")
    ).hexdigest()

    assert review.status == "provider_failed"
    assert len(review.error_message.encode("utf-8")) <= 8 * 1024
    assert review.error_message_bytes == len(error_message.encode("utf-8"))
    assert review.error_message_sha256 == expected_hash
    assert review.error_message_truncated is True
    assert len(attempt.error_message.encode("utf-8")) <= 8 * 1024
    assert attempt.error_message_bytes == len(error_message.encode("utf-8"))
    assert attempt.error_message_sha256 == expected_hash
    assert attempt.error_message_truncated is True


def test_llm_review_table_has_physical_identity_uniqueness(review_bundle) -> None:
    inspector = inspect(review_bundle.engine)
    constraints = inspector.get_unique_constraints(
        "cl_decision_llm_review"
    )
    identities = {tuple(item["column_names"]) for item in constraints}

    assert (
        "event_id",
        "packet_fingerprint",
        "provider",
        "model",
        "prompt_version",
    ) in identities
    assert ("review_id",) in identities
    columns = {
        column["name"]: column for column in inspector.get_columns(
            "cl_decision_llm_review"
        )
    }
    identity_width = sum(
        columns[name]["type"].length
        for name in (
            "event_id",
            "packet_fingerprint",
            "provider",
            "model",
            "prompt_version",
        )
    )
    assert identity_width * 4 <= 3072


def test_mysql_review_audit_uses_binary_identity_and_longtext() -> None:
    ddl = str(
        CreateTable(TableByLLMReview.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "VARCHAR(40) COLLATE utf8mb4_bin" in ddl
    assert "VARCHAR(191) COLLATE utf8mb4_bin" in ddl
    assert "VARCHAR(64) COLLATE utf8mb4_bin" in ddl
    assert ddl.count("LONGTEXT") >= 5


def test_non_pending_event_cannot_start_new_provider_review(review_bundle) -> None:
    review_bundle.event_service.invalidate(
        review_bundle.event.event_id,
        "manual_invalidated",
        occurred_at=review_bundle.reviewed_at,
    )
    provider = _FakeProvider((_failure(),))

    with pytest.raises(InvalidEventTransition, match="review_pending"):
        _service(review_bundle, provider).review_event(
            review_bundle.event.event_id
        )

    assert provider.call_count == 0
    assert review_bundle.store.count_llm_reviews(review_bundle.event.event_id) == 0
