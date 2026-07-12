from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import secrets
import time
from typing import Callable

from .corpus_retrieval import CorpusIndex
from .corpus_types import ImageEvidence
from .evidence import (
    EvidencePacket,
    ModelCapabilities,
    RuleEvidenceBinding,
    build_evidence_packet,
)
from .event_service import DecisionEventService, ReviewApplication
from .event_store import (
    EventConflictError,
    InvalidEventTransition,
    LLMReviewClaimLostError,
    MAX_LLM_AUDIT_BYTES,
    StoredLLMReview,
)
from .fingerprints import normalize_datetime, sha256_json
from .llm_provider import LLMProvider, ProviderImage, ProviderResponse
from .models import DecisionEvent, EventState
from .mutation_fence import MutationLeaseGuard, mutation_fenced
from .review_prompt import PROMPT_VERSION, build_messages
from .review_schema import ReviewVerdict, parse_review
from .risk_snapshot import RiskSnapshot


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTITY_VALIDATION_ERRORS = frozenset(
    {"stale_event_id", "stale_data_fingerprint", "stale_packet_fingerprint"}
)
_RUNTIME_RISK_PREFIX = "runtime_risk:"
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_RESPONSE_BYTES = MAX_LLM_AUDIT_BYTES

RiskResolver = Callable[[DecisionEvent], RiskSnapshot]
RuleEvidenceResolver = Callable[[DecisionEvent], RuleEvidenceBinding]
ImageLoader = Callable[[ImageEvidence], ProviderImage]


class ReviewInProgressError(RuntimeError):
    pass


def _real_clock() -> datetime:
    return datetime.now(timezone.utc)


def _timeout_pair(value: object) -> tuple[float, float]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("timeout must be a positive connect/read tuple")
    parsed: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("timeout must be a positive connect/read tuple")
        number = float(item)
        if number <= 0:
            raise ValueError("timeout must be a positive connect/read tuple")
        parsed.append(number)
    return parsed[0], parsed[1]


def _positive_wait(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("claim_wait_seconds must be positive")
    parsed = float(value)
    if parsed <= 0:
        raise ValueError("claim_wait_seconds must be positive")
    return parsed


def _read_image(image: ImageEvidence) -> ProviderImage:
    path = Path(image.source_path)
    payload = path.read_bytes()
    if not payload or len(payload) > _MAX_IMAGE_BYTES:
        raise ValueError("image payload size is invalid")
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual != image.sha256:
        raise ValueError("image fingerprint mismatch")
    encoded = base64.b64encode(payload).decode("ascii")
    return ProviderImage(
        image_id=image.image_id,
        media_type=image.media_type,
        data_url=f"data:{image.media_type};base64,{encoded}",
    )


def _validated_image(
    source: ImageEvidence,
    loaded: object,
) -> ProviderImage:
    if not isinstance(loaded, ProviderImage):
        raise TypeError("image loader must return ProviderImage")
    if loaded.image_id != source.image_id or loaded.media_type != source.media_type:
        raise ValueError("loaded image identity mismatch")
    try:
        encoded = loaded.data_url.split(",", 1)[1]
        payload = base64.b64decode(encoded, validate=True)
    except (IndexError, ValueError) as exc:
        raise ValueError("loaded image data is invalid") from exc
    if not payload or len(payload) > _MAX_IMAGE_BYTES:
        raise ValueError("loaded image payload size is invalid")
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual != source.sha256:
        raise ValueError("loaded image fingerprint mismatch")
    return loaded


def _review_id(packet: EvidencePacket, provider: str, model: str) -> str:
    fingerprint = sha256_json(
        {
            "event_id": packet.event.event_id,
            "packet_fingerprint": packet.packet_fingerprint,
            "provider": provider,
            "model": model,
            "prompt_version": PROMPT_VERSION,
        }
    )
    return "llm-review-" + fingerprint[7:]


def _bind_risk_snapshot_identity(
    packet: EvidencePacket,
    snapshot: RiskSnapshot,
) -> EvidencePacket:
    return replace(
        packet,
        packet_fingerprint=sha256_json(
            {
                "evidence_packet_fingerprint": packet.packet_fingerprint,
                "risk_snapshot_id": snapshot.snapshot_id,
            }
        ),
    )


def _attempt_id(
    review_id: str,
    owner_token: str,
    fencing_token: int,
    attempt_number: int,
) -> str:
    fingerprint = sha256_json(
        {
            "review_id": review_id,
            "owner_token": owner_token,
            "fencing_token": fencing_token,
            "attempt_number": attempt_number,
        }
    )
    return "llm-attempt-" + fingerprint[7:]


def _internal_failure(
    provider: str,
    model: str,
    error_code: str,
    error_message: str,
    *,
    raw_response: str = "",
    latency_ms: int = 0,
) -> ProviderResponse:
    return ProviderResponse(
        ok=False,
        provider=provider,
        model=model,
        content=None,
        raw_response=raw_response,
        error_code=error_code,
        error_message=error_message,
        retryable=False,
        latency_ms=latency_ms,
    )


def _utf8_size(value: str | None) -> int:
    return 0 if value is None else len(value.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class _ReviewOutcome:
    status: str
    provider_ok: bool
    verdict: str
    response_content: str | None
    raw_response: str
    parsed_response_json: str | None
    validation_errors: tuple[str, ...]
    reviewed_data_fingerprint: str
    attempt_count: int
    latency_ms: int
    error_code: str | None
    error_message: str | None


class ReviewService:
    def __init__(
        self,
        event_service: DecisionEventService,
        corpus_index: CorpusIndex,
        risk_resolver: RiskResolver,
        provider: LLMProvider,
        *,
        timeout: tuple[float, float] = (10, 180),
        max_units: int = 8,
        image_loader: ImageLoader | None = None,
        rule_evidence_resolver: RuleEvidenceResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        claim_wait_seconds: float = 5,
    ) -> None:
        if not isinstance(event_service, DecisionEventService):
            raise TypeError("event_service must be DecisionEventService")
        if not isinstance(corpus_index, CorpusIndex):
            raise TypeError("corpus_index must be CorpusIndex")
        if not callable(risk_resolver):
            raise TypeError("risk_resolver must be callable")
        provider_name = getattr(provider, "provider", None)
        model = getattr(provider, "model", None)
        capabilities = getattr(provider, "capabilities", None)
        if not isinstance(provider_name, str) or not provider_name:
            raise TypeError("provider must declare provider")
        if len(provider_name) > 40:
            raise ValueError("provider exceeds 40 characters")
        if not isinstance(model, str) or not model:
            raise TypeError("provider must declare model")
        if len(model) > 191:
            raise ValueError("model exceeds 191 characters")
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("provider must implement complete")
        if not isinstance(capabilities, ModelCapabilities):
            raise TypeError("provider must declare ModelCapabilities")
        if isinstance(max_units, bool) or not isinstance(max_units, int) or max_units <= 0:
            raise ValueError("max_units must be a positive integer")
        if image_loader is not None and not callable(image_loader):
            raise TypeError("image_loader must be callable")
        if rule_evidence_resolver is not None and not callable(
            rule_evidence_resolver
        ):
            raise TypeError("rule_evidence_resolver must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")

        self.event_service = event_service
        self.store = event_service.store
        self._corpus_index = corpus_index
        self._risk_resolver = risk_resolver
        self._provider = provider
        self._provider_name = provider_name
        self._model = model
        self._capabilities = capabilities
        self._timeout = _timeout_pair(timeout)
        self._lease_seconds = 2 * sum(self._timeout) + 60
        self._max_units = max_units
        self._image_loader = image_loader or _read_image
        self._rule_evidence_resolver = rule_evidence_resolver
        self._clock = clock or _real_clock
        self._claim_wait_seconds = _positive_wait(claim_wait_seconds)
        self._mutation_fence = MutationLeaseGuard()
        self._strategy_run_binding: tuple[str, int, str] | None = None

    def bind_strategy_run(self, strategy_run: object) -> None:
        self._mutation_fence.bind(strategy_run)
        run_id = getattr(strategy_run, "run_id", None)
        epoch = getattr(strategy_run, "epoch", None)
        fingerprint = getattr(
            strategy_run,
            "strategy_run_fingerprint",
            None,
        )
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("strategy_run_id is invalid")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            raise ValueError("strategy_run_epoch must be a positive integer")
        if (
            not isinstance(fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(fingerprint) is None
        ):
            raise ValueError("strategy_run_fingerprint is invalid")
        binding = (run_id, epoch, fingerprint)
        if self._strategy_run_binding not in (None, binding):
            raise ValueError("strategy run binding cannot change")
        self._strategy_run_binding = binding

    def _require_current_strategy_event(self, event: DecisionEvent) -> None:
        if self._strategy_run_binding is None:
            return
        if (
            event.strategy_run_id,
            event.strategy_run_epoch,
            event.strategy_run_fingerprint,
        ) != self._strategy_run_binding:
            raise InvalidEventTransition(
                "event is outside the current strategy run"
            )

    def get_reviews(self, event_id: str) -> tuple[StoredLLMReview, ...]:
        return self.store.list_llm_reviews(event_id)

    def evidence_packet(
        self,
        event: DecisionEvent,
        risk_snapshot: RiskSnapshot,
    ) -> EvidencePacket:
        """Build the exact immutable packet used by review and paper admission."""

        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        if not isinstance(risk_snapshot, RiskSnapshot):
            raise TypeError("risk_snapshot must be RiskSnapshot")
        if risk_snapshot.event_binding_reasons(event):
            raise InvalidEventTransition(
                "risk snapshot does not match evidence packet event"
            )
        rule_evidence_binding = None
        if self._rule_evidence_resolver is not None:
            try:
                candidate_binding = self._rule_evidence_resolver(event)
            except Exception:
                candidate_binding = None
            if isinstance(candidate_binding, RuleEvidenceBinding):
                rule_evidence_binding = candidate_binding
        packet = build_evidence_packet(
            event,
            risk_snapshot.decision,
            self._corpus_index,
            self._capabilities,
            max_units=self._max_units,
            rule_evidence_binding=rule_evidence_binding,
        )
        return _bind_risk_snapshot_identity(packet, risk_snapshot)

    @mutation_fenced("review_service.review_event")
    def review_event(self, event_id: str) -> StoredLLMReview:
        self._mutation_fence.require()
        view = self.event_service.get(event_id)
        self._require_current_strategy_event(view.event)
        if view.event.rule_binding_status == "legacy_unbound":
            raise InvalidEventTransition(
                "legacy-unbound events are read-only and cannot be reviewed"
            )
        risk_snapshot = self._risk_resolver(view.event)
        if not isinstance(risk_snapshot, RiskSnapshot):
            raise TypeError("risk_resolver must return RiskSnapshot")
        risk_validation = risk_snapshot.validate_for_review(
            view.event,
            as_of=self._now(),
        )
        if not risk_validation.usable:
            raise InvalidEventTransition(
                "risk snapshot is not usable for review:"
                + ",".join(risk_validation.reasons)
            )
        packet = self.evidence_packet(view.event, risk_snapshot)
        existing = self._find(packet)
        if existing is not None:
            self._apply_stored(existing, packet, risk_snapshot)
            return existing
        if view.state is not EventState.REVIEW_PENDING:
            raise InvalidEventTransition(
                f"current state is {view.state.value}, not review_pending"
            )

        review_id = _review_id(packet, self._provider_name, self._model)
        owner_token = secrets.token_hex(16)
        claim = self._acquire_claim(packet, review_id, owner_token)
        if not claim.acquired:
            existing = self._wait_for_final(packet)
            if existing is not None:
                self._apply_stored(existing, packet, risk_snapshot)
                return existing
            claim = self._acquire_claim(packet, review_id, owner_token)
            if not claim.acquired:
                raise ReviewInProgressError(
                    "an LLM review for this immutable identity is in progress"
                )

        if packet.blockers:
            outcome = _ReviewOutcome(
                status="local_abstain",
                provider_ok=False,
                verdict=ReviewVerdict.ABSTAIN.value,
                response_content=None,
                raw_response="",
                parsed_response_json=None,
                validation_errors=packet.blockers,
                reviewed_data_fingerprint=packet.event.data_fingerprint,
                attempt_count=0,
                latency_ms=0,
                error_code="packet_blocked",
                error_message=",".join(packet.blockers),
            )
        else:
            outcome = self._call_provider(
                packet,
                review_id,
                owner_token,
                claim.fencing_token,
            )

        completed_at = self._now()
        runtime_risk_errors = self._runtime_risk_errors(
            view.event,
            risk_snapshot,
            as_of=completed_at,
        )
        if outcome.provider_ok and runtime_risk_errors:
            outcome = replace(
                outcome,
                status="validation_failed",
                verdict=ReviewVerdict.ABSTAIN.value,
                validation_errors=tuple(
                    dict.fromkeys(
                        (*outcome.validation_errors, *runtime_risk_errors)
                    )
                ),
            )
        renewed = self._acquire_claim(packet, review_id, owner_token)
        if (
            not renewed.acquired
            or renewed.fencing_token != claim.fencing_token
        ):
            existing = self._wait_for_final(packet)
            if existing is not None:
                self._apply_stored(existing, packet, risk_snapshot)
                return existing
            raise LLMReviewClaimLostError("LLM review claim was lost")
        try:
            stored = self._persist(
                packet,
                risk_snapshot,
                review_id,
                owner_token,
                renewed.fencing_token,
                outcome,
                completed_at,
            )
        except LLMReviewClaimLostError:
            existing = self._wait_for_final(packet)
            if existing is None:
                raise
            stored = existing
        self._apply_stored(stored, packet, risk_snapshot)
        return stored

    def _now(self) -> datetime:
        return normalize_datetime(self._clock(), "clock")

    def _runtime_risk_errors(
        self,
        event: DecisionEvent,
        expected: RiskSnapshot,
        *,
        as_of: datetime,
    ) -> tuple[str, ...]:
        try:
            current = self._risk_resolver(event)
        except Exception:
            return (_RUNTIME_RISK_PREFIX + "resolver_failed",)
        if not isinstance(current, RiskSnapshot):
            return (_RUNTIME_RISK_PREFIX + "resolver_invalid",)
        if current.snapshot_id != expected.snapshot_id:
            return (_RUNTIME_RISK_PREFIX + "snapshot_identity_changed",)
        if (
            current.identity_fingerprint != expected.identity_fingerprint
            or current.payload_fingerprint != expected.payload_fingerprint
        ):
            return (_RUNTIME_RISK_PREFIX + "snapshot_payload_changed",)
        try:
            validation = current.validate_for_review(event, as_of=as_of)
        except Exception:
            return (_RUNTIME_RISK_PREFIX + "validation_failed",)
        return tuple(
            _RUNTIME_RISK_PREFIX + reason for reason in validation.reasons
        )

    def _find(self, packet: EvidencePacket) -> StoredLLMReview | None:
        return self.store.find_llm_review(
            event_id=packet.event.event_id,
            packet_fingerprint=packet.packet_fingerprint,
            provider=self._provider_name,
            model=self._model,
            prompt_version=PROMPT_VERSION,
        )

    def _acquire_claim(
        self,
        packet: EvidencePacket,
        review_id: str,
        owner_token: str,
    ):
        now = self._now()
        return self.store.acquire_llm_review_claim(
            review_id=review_id,
            event_id=packet.event.event_id,
            packet_fingerprint=packet.packet_fingerprint,
            provider=self._provider_name,
            model=self._model,
            prompt_version=PROMPT_VERSION,
            owner_token=owner_token,
            now=now,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
        )

    def _wait_for_final(self, packet: EvidencePacket) -> StoredLLMReview | None:
        deadline = time.monotonic() + self._claim_wait_seconds
        while time.monotonic() < deadline:
            stored = self._find(packet)
            if stored is not None:
                return stored
            time.sleep(0.01)
        return self._find(packet)

    def _provider_images(
        self,
        packet: EvidencePacket,
    ) -> tuple[ProviderImage, ...]:
        return tuple(
            _validated_image(image, self._image_loader(image))
            for image in packet.image_evidence
        )

    def _normalized_response(
        self,
        candidate: object,
    ) -> tuple[ProviderResponse, str | None, str]:
        if not isinstance(candidate, ProviderResponse):
            response = _internal_failure(
                self._provider_name,
                self._model,
                "malformed_provider_response",
                "provider returned an unexpected response type",
            )
            return response, None, ""
        raw_response = (
            candidate.raw_response
            if isinstance(candidate.raw_response, str)
            else ""
        )
        response_content = (
            candidate.content if isinstance(candidate.content, str) else None
        )
        if not candidate.ok and (
            not isinstance(candidate.error_code, str)
            or len(candidate.error_code) > 100
            or (
                candidate.error_message is not None
                and not isinstance(candidate.error_message, str)
            )
        ):
            response = _internal_failure(
                self._provider_name,
                self._model,
                "malformed_provider_response",
                "provider failure metadata is invalid",
                raw_response=raw_response,
                latency_ms=candidate.latency_ms,
            )
            return response, response_content, raw_response
        if (
            _utf8_size(raw_response) > _MAX_RESPONSE_BYTES
            or _utf8_size(response_content) > _MAX_RESPONSE_BYTES
        ):
            response = _internal_failure(
                self._provider_name,
                self._model,
                "response_too_large",
                "provider response exceeds the audit limit",
                raw_response=raw_response,
                latency_ms=candidate.latency_ms,
            )
            return response, response_content, raw_response
        if not isinstance(candidate.raw_response, str):
            response = _internal_failure(
                self._provider_name,
                self._model,
                "malformed_provider_response",
                "provider raw response must be text",
                latency_ms=candidate.latency_ms,
            )
            return response, response_content, ""
        if (
            candidate.provider != self._provider_name
            or candidate.model != self._model
        ):
            response = _internal_failure(
                self._provider_name,
                self._model,
                "provider_identity_mismatch",
                "provider response identity does not match configuration",
                raw_response=raw_response,
                latency_ms=candidate.latency_ms,
            )
            return response, response_content, raw_response
        return candidate, response_content, raw_response

    def _call_provider(
        self,
        packet: EvidencePacket,
        review_id: str,
        owner_token: str,
        fencing_token: int,
    ) -> _ReviewOutcome:
        try:
            messages = build_messages(packet)
            images = self._provider_images(packet)
        except Exception:
            return _ReviewOutcome(
                status="local_abstain",
                provider_ok=False,
                verdict=ReviewVerdict.ABSTAIN.value,
                response_content=None,
                raw_response="",
                parsed_response_json=None,
                validation_errors=("image_evidence_unavailable",),
                reviewed_data_fingerprint=packet.event.data_fingerprint,
                attempt_count=0,
                latency_ms=0,
                error_code="image_load_failed",
                error_message="required image evidence could not be verified",
            )

        attempts = 0
        total_latency = 0
        response: ProviderResponse | None = None
        response_content: str | None = None
        raw_response = ""
        while attempts < 2:
            attempts += 1
            started_at = self._now()
            try:
                candidate = self._provider.complete(
                    messages,
                    images,
                    self._timeout,
                )
            except Exception:
                candidate = _internal_failure(
                    self._provider_name,
                    self._model,
                    "provider_exception",
                    "provider raised unexpectedly",
                )
            completed_at = self._now()
            response, response_content, raw_response = self._normalized_response(
                candidate
            )
            total_latency += response.latency_ms
            self.store.append_llm_review_attempt(
                attempt_id=_attempt_id(
                    review_id,
                    owner_token,
                    fencing_token,
                    attempts,
                ),
                review_id=review_id,
                event_id=packet.event.event_id,
                owner_token=owner_token,
                fencing_token=fencing_token,
                attempt_number=attempts,
                provider=self._provider_name,
                model=self._model,
                ok=response.ok,
                retryable=response.retryable,
                response_content=response_content,
                raw_response=raw_response,
                error_code=response.error_code,
                error_message=response.error_message,
                latency_ms=response.latency_ms,
                started_at=started_at,
                completed_at=completed_at,
            )
            if response.ok or not response.retryable:
                break
        if response is None:
            raise AssertionError("provider loop produced no response")
        if not response.ok:
            error_code = response.error_code or "provider_failure"
            return _ReviewOutcome(
                status="provider_failed",
                provider_ok=False,
                verdict=ReviewVerdict.ABSTAIN.value,
                response_content=None,
                raw_response=raw_response,
                parsed_response_json=None,
                validation_errors=(f"provider_error:{error_code}",),
                reviewed_data_fingerprint=packet.event.data_fingerprint,
                attempt_count=attempts,
                latency_ms=total_latency,
                error_code=error_code,
                error_message=response.error_message,
            )

        if response_content is None:
            raise AssertionError("successful provider response lost content")
        validated = parse_review(response_content, packet)
        reviewed_data_fingerprint = validated.reviewed_data_fingerprint
        if (
            not isinstance(reviewed_data_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(reviewed_data_fingerprint) is None
        ):
            reviewed_data_fingerprint = packet.event.data_fingerprint
        status = (
            "validated"
            if not validated.validation_errors
            else "validation_failed"
        )
        return _ReviewOutcome(
            status=status,
            provider_ok=True,
            verdict=validated.verdict.value,
            response_content=response_content,
            raw_response=raw_response,
            parsed_response_json=validated.parsed_response_json,
            validation_errors=validated.validation_errors,
            reviewed_data_fingerprint=reviewed_data_fingerprint,
            attempt_count=attempts,
            latency_ms=total_latency,
            error_code=None,
            error_message=None,
        )

    def _persist(
        self,
        packet: EvidencePacket,
        risk_snapshot: RiskSnapshot,
        review_id: str,
        owner_token: str,
        fencing_token: int,
        outcome: _ReviewOutcome,
        created_at: datetime,
    ) -> StoredLLMReview:
        return self.store.append_llm_review(
            review_id=review_id,
            owner_token=owner_token,
            fencing_token=fencing_token,
            event_id=packet.event.event_id,
            risk_snapshot_id=risk_snapshot.snapshot_id,
            packet_fingerprint=packet.packet_fingerprint,
            reviewed_data_fingerprint=outcome.reviewed_data_fingerprint,
            provider=self._provider_name,
            model=self._model,
            prompt_version=PROMPT_VERSION,
            status=outcome.status,
            provider_ok=outcome.provider_ok,
            verdict=outcome.verdict,
            response_content=outcome.response_content,
            raw_response=outcome.raw_response,
            parsed_response_json=outcome.parsed_response_json,
            validation_errors=outcome.validation_errors,
            attempt_count=outcome.attempt_count,
            latency_ms=outcome.latency_ms,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            created_at=created_at,
        )

    def _apply_stored(
        self,
        stored: StoredLLMReview,
        packet: EvidencePacket,
        risk_snapshot: RiskSnapshot,
    ) -> None:
        if stored.risk_snapshot_id != risk_snapshot.snapshot_id:
            raise EventConflictError(
                "stored LLM review risk snapshot identity mismatch"
            )
        if stored.provider_ok:
            if stored.response_content is None:
                raise EventConflictError(
                    "provider-valid review is missing response content"
                )
            validated = parse_review(stored.response_content, packet)
            reviewed_data_fingerprint = validated.reviewed_data_fingerprint
            if (
                not isinstance(reviewed_data_fingerprint, str)
                or _FINGERPRINT_RE.fullmatch(reviewed_data_fingerprint) is None
            ):
                reviewed_data_fingerprint = packet.event.data_fingerprint
            runtime_risk_errors = tuple(
                error
                for error in stored.validation_errors
                if error.startswith(_RUNTIME_RISK_PREFIX)
            )
            expected_errors = tuple(
                (*validated.validation_errors, *runtime_risk_errors)
            )
            expected_status = "validated" if not expected_errors else "validation_failed"
            expected_verdict = (
                ReviewVerdict.ABSTAIN.value
                if runtime_risk_errors
                else validated.verdict.value
            )
            if (
                stored.status != expected_status
                or stored.verdict != expected_verdict
                or stored.validation_errors != expected_errors
                or stored.parsed_response_json
                != validated.parsed_response_json
                or stored.reviewed_data_fingerprint
                != reviewed_data_fingerprint
            ):
                raise EventConflictError(
                    "stored LLM review does not match strict revalidation"
                )
        elif stored.verdict != ReviewVerdict.ABSTAIN.value:
            raise EventConflictError("failed LLM review must abstain")

        if any(
            error.startswith(_RUNTIME_RISK_PREFIX)
            for error in stored.validation_errors
        ):
            return
        if _IDENTITY_VALIDATION_ERRORS.intersection(stored.validation_errors):
            return
        if self.event_service.get(stored.event_id).state is not EventState.REVIEW_PENDING:
            return
        reviewed_at = self._now()
        if self._runtime_risk_errors(
            packet.event,
            risk_snapshot,
            as_of=reviewed_at,
        ):
            return
        self.event_service.apply_review(
            ReviewApplication(
                review_id=stored.review_id,
                reviewed_event_id=stored.event_id,
                reviewed_data_fingerprint=stored.reviewed_data_fingerprint,
                verdict=stored.verdict,
                reviewed_at=reviewed_at,
            )
        )
