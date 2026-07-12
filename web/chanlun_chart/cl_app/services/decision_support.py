from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

from chanlun.decision_support.api_read_model import (
    DecisionSupportReadModel,
    ReadModelConflict,
    ReadModelNotFound,
)
from chanlun.decision_support.certified_runtime import CertifiedCorpusRuntime
from chanlun.decision_support.evidence import (
    ModelCapabilities,
    RuleEvidenceBinding,
)
from chanlun.decision_support.event_store import DecisionEventStore
from chanlun.decision_support.event_store import EventNotFoundError


_IMAGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_MEDIA_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
_REDACTED_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "error_message",
        "internal_path",
        "owner_token",
        "password",
        "prompt",
        "raw_response",
        "response_content",
        "secret",
        "token",
    }
)


class DecisionSupportError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.status_code = status_code
        self.message = message or code


def _redact(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if str(key).casefold() not in _REDACTED_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_redact(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _provider_payload(
    provider: Callable[..., object] | None,
    unavailable_code: str,
    *args: object,
) -> dict[str, object]:
    if provider is None:
        raise DecisionSupportError(unavailable_code, 503)
    payload = provider(*args)
    if is_dataclass(payload) and not isinstance(payload, type):
        payload = asdict(payload)
    if not isinstance(payload, Mapping):
        raise DecisionSupportError(unavailable_code, 503)
    return dict(_redact(payload))


@dataclass(frozen=True, slots=True)
class _TrustedImage:
    image_id: str
    path: Path
    sha256: str
    media_type: str


class TrustedImageCatalog:
    def __init__(
        self,
        manifest_path: str | Path,
        tier_roots: Mapping[str, str | Path],
    ) -> None:
        path = Path(manifest_path).resolve()
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise DecisionSupportError("image_untrusted", 409) from exc
        if not isinstance(manifest, Mapping):
            raise DecisionSupportError("image_untrusted", 409)
        roots = {
            str(tier): Path(root).resolve()
            for tier, root in tier_roots.items()
        }
        images = manifest.get("images")
        if not isinstance(images, list):
            raise DecisionSupportError("image_untrusted", 409)
        catalog: dict[str, _TrustedImage] = {}
        for raw in images:
            if not isinstance(raw, Mapping):
                raise DecisionSupportError("image_untrusted", 409)
            image_id = raw.get("image_id")
            tier = raw.get("source_tier")
            source_path = raw.get("source_path")
            digest = raw.get("sha256")
            media_type = raw.get("media_type")
            if (
                not isinstance(image_id, str)
                or _IMAGE_ID_RE.fullmatch(image_id) is None
                or image_id in catalog
                or not isinstance(tier, str)
                or tier not in roots
                or not isinstance(source_path, str)
                or not source_path
                or not isinstance(digest, str)
                or not isinstance(media_type, str)
            ):
                raise DecisionSupportError("image_untrusted", 409)
            normalized_digest = digest.removeprefix("sha256:").casefold()
            if (
                _DIGEST_RE.fullmatch(normalized_digest) is None
                or media_type not in _SAFE_MEDIA_TYPES
            ):
                raise DecisionSupportError("image_untrusted", 409)
            relative = Path(source_path)
            if relative.is_absolute():
                raise DecisionSupportError("image_untrusted", 409)
            root = roots[tier]
            candidate = (root / relative).resolve()
            if not candidate.is_relative_to(root):
                raise DecisionSupportError("image_untrusted", 409)
            catalog[image_id] = _TrustedImage(
                image_id=image_id,
                path=candidate,
                sha256=normalized_digest,
                media_type=media_type,
            )
        self._images = catalog

    def read(self, image_id: str) -> tuple[bytes, str]:
        if (
            not isinstance(image_id, str)
            or _IMAGE_ID_RE.fullmatch(image_id) is None
        ):
            raise DecisionSupportError("image_not_found", 404)
        image = self._images.get(image_id)
        if image is None:
            raise DecisionSupportError("image_not_found", 404)
        try:
            payload = image.path.read_bytes()
        except OSError as exc:
            raise DecisionSupportError("image_untrusted", 409) from exc
        if hashlib.sha256(payload).hexdigest() != image.sha256:
            raise DecisionSupportError("image_untrusted", 409)
        return payload, image.media_type


class DecisionSupportFacade:
    def __init__(
        self,
        *,
        candidate_provider: Callable[[str | None, int], object] | None = None,
        event_provider: Callable[[str], object] | None = None,
        evidence_provider: Callable[[str], object] | None = None,
        risk_provider: Callable[[], object] | None = None,
        corpus_provider: Callable[[], object] | None = None,
        image_catalog: object | None = None,
        review_provider: Callable[[str, str, bool], object] | None = None,
        user_decision_provider: (
            Callable[[str, str, Mapping[str, object]], object] | None
        ) = None,
        promotion_provider: Callable[[], object] | None = None,
    ) -> None:
        self._candidate_provider = candidate_provider
        self._event_provider = event_provider
        self._evidence_provider = evidence_provider
        self._risk_provider = risk_provider
        self._corpus_provider = corpus_provider
        self.image_catalog = image_catalog
        self._review_provider = review_provider
        self._user_decision_provider = user_decision_provider
        self._promotion_provider = promotion_provider

    def candidates(self, cursor: str | None, limit: int) -> dict[str, object]:
        return _provider_payload(
            self._candidate_provider,
            "scanner_unavailable",
            cursor,
            limit,
        )

    def event(self, event_id: str) -> dict[str, object]:
        return _provider_payload(
            self._event_provider,
            "event_store_unavailable",
            event_id,
        )

    def evidence(self, event_id: str) -> dict[str, object]:
        return _provider_payload(
            self._evidence_provider,
            "evidence_unavailable",
            event_id,
        )

    def risk_status(self) -> dict[str, object]:
        payload = _provider_payload(self._risk_provider, "risk_unavailable")
        if self._promotion_provider is None:
            payload["paper_gate_pending"] = True
            payload["promotion_state"] = "research"
            payload["promotion_reasons"] = ["promotion_status_unavailable"]
            return payload
        promotion = _provider_payload(
            self._promotion_provider,
            "promotion_unavailable",
        )
        payload["paper_gate_pending"] = (
            promotion.get("paper_gate_pending") is not False
        )
        payload["promotion_state"] = promotion.get("state", "research")
        payload["promotion_reasons"] = promotion.get("reasons", [])
        return payload

    def corpus_status(self) -> dict[str, object]:
        payload = _provider_payload(
            self._corpus_provider,
            "corpus_untrusted",
        )
        payload["review_eligible"] = (
            payload.get("original_integrity", payload.get("integrity"))
            == "complete"
            and payload.get("original_evidence") == "available"
        )
        return payload

    def image(self, image_id: str) -> tuple[bytes, str]:
        if self.image_catalog is None:
            raise DecisionSupportError("image_not_found", 404)
        try:
            return self.image_catalog.read(image_id)
        except DecisionSupportError:
            raise
        except KeyError as exc:
            raise DecisionSupportError("image_not_found", 404) from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DecisionSupportError("image_untrusted", 409) from exc

    def request_review(
        self,
        event_id: str,
        user_id: str,
        force: bool,
    ) -> dict[str, object]:
        return _provider_payload(
            self._review_provider,
            "review_unavailable",
            event_id,
            user_id,
            force,
        )

    def record_user_decision(
        self,
        event_id: str,
        user_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return _provider_payload(
            self._user_decision_provider,
            "user_decision_unavailable",
            event_id,
            user_id,
            dict(payload),
        )


def build_persistent_decision_support_facade(
    store: DecisionEventStore,
    *,
    clock: Callable[[], datetime] | None = None,
    strategy_run: object | None = None,
    evidence_provider: Callable[[str], object] | None = None,
    corpus_provider: Callable[[], object] | None = None,
    image_catalog: object | None = None,
    review_provider: Callable[[str, str, bool], object] | None = None,
    promotion_provider: Callable[[], object] | None = None,
    rule_evidence_resolver: (
        Callable[[object], RuleEvidenceBinding] | None
    ) = None,
    certified_corpus_runtime: CertifiedCorpusRuntime | None = None,
) -> DecisionSupportFacade:
    read_model = DecisionSupportReadModel(
        store,
        clock=clock,
        strategy_run=strategy_run,
    )

    def candidates(cursor: str | None, limit: int) -> object:
        try:
            return read_model.candidates(cursor, limit)
        except ValueError as exc:
            raise DecisionSupportError("invalid_cursor", 400) from exc
        except Exception as exc:
            raise DecisionSupportError("scanner_unavailable", 503) from exc

    def event(event_id: str) -> object:
        try:
            return read_model.event(event_id)
        except ReadModelNotFound as exc:
            raise DecisionSupportError("event_not_found", 404) from exc
        except Exception as exc:
            raise DecisionSupportError("event_store_unavailable", 503) from exc

    def risk() -> object:
        try:
            return read_model.risk_status()
        except Exception as exc:
            raise DecisionSupportError("risk_unavailable", 503) from exc

    def user_decision(
        event_id: str,
        user_id: str,
        payload: Mapping[str, object],
    ) -> object:
        try:
            return read_model.record_user_decision(event_id, user_id, payload)
        except ReadModelNotFound as exc:
            raise DecisionSupportError("event_not_found", 404) from exc
        except ReadModelConflict as exc:
            raise DecisionSupportError("user_decision_conflict", 409) from exc
        except (TypeError, ValueError) as exc:
            raise DecisionSupportError("invalid_user_decision", 400) from exc
        except Exception as exc:
            raise DecisionSupportError(
                "user_decision_unavailable",
                503,
            ) from exc

    if evidence_provider is None and certified_corpus_runtime is not None:
        capabilities = ModelCapabilities(
            supports_images=True,
            supports_json_schema=True,
        )

        def certified_evidence(event_id: str) -> object:
            try:
                snapshot = store.get_snapshot(event_id)
            except EventNotFoundError as exc:
                raise DecisionSupportError("event_not_found", 404) from exc
            try:
                risk_snapshots = store.list_risk_snapshots(event_id)
                if not risk_snapshots:
                    raise DecisionSupportError(
                        "risk_snapshot_unavailable",
                        409,
                    )
                risk_snapshot = risk_snapshots[-1]
                as_of = (
                    clock() if clock is not None else datetime.now(timezone.utc)
                )
                validation = risk_snapshot.validate_for_review(
                    snapshot.event,
                    as_of=as_of,
                )
                packet = certified_corpus_runtime.evidence_packet(
                    snapshot.event,
                    risk_snapshot.decision,
                    capabilities,
                    rule_evidence_binding=(
                        rule_evidence_resolver(snapshot.event)
                        if rule_evidence_resolver is not None
                        else None
                    ),
                )
                blockers = tuple(
                    dict.fromkeys((*packet.blockers, *validation.reasons))
                )
                return {
                    "event_id": snapshot.event.event_id,
                    "packet_fingerprint": packet.packet_fingerprint,
                    "risk_snapshot_id": risk_snapshot.snapshot_id,
                    "reviewable": packet.reviewable and validation.usable,
                    "blockers": blockers,
                    "supporting": packet.supporting,
                    "counter_evidence": packet.counter_evidence,
                    "image_evidence": packet.image_evidence,
                }
            except DecisionSupportError:
                raise
            except Exception as exc:
                raise DecisionSupportError("evidence_unavailable", 503) from exc

        evidence_provider = certified_evidence
    if corpus_provider is None and certified_corpus_runtime is not None:
        corpus_provider = certified_corpus_runtime.status
    if image_catalog is None and certified_corpus_runtime is not None:
        image_catalog = certified_corpus_runtime

    return DecisionSupportFacade(
        candidate_provider=candidates,
        event_provider=event,
        evidence_provider=evidence_provider,
        risk_provider=risk,
        corpus_provider=corpus_provider,
        image_catalog=image_catalog,
        review_provider=review_provider,
        user_decision_provider=user_decision,
        promotion_provider=promotion_provider,
    )
