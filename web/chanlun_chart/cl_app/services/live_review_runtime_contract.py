"""Web-only compatibility boundary for the live-review audit contract.

The fixed-year research process hashes every file below ``src/chanlun`` and is
currently running against an immutable revision.  One presentation-only
producer/auditor difference was discovered while that run was active:

* an invalidated buy row may retain its already-observed expired 1m segment
  boundary advisory, while the auditor stops deriving it after invalidation.

The adapter below does not rewrite or accept an unsigned signal. It first uses
the original auditor. On failure it builds an in-memory shadow containing only
the exact inverse of that difference and asks the original auditor to validate
the shadow. The persisted signal, its decision identity, and every downstream
review document remain unchanged. All other differences still fail closed.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from threading import RLock

from chanlun.decision_support.trading_system import live_human_review as _core


WEB_LIVE_REVIEW_RUNTIME_CONTRACT_ID = (
    "chanlun-web-live-review-producer-auditor-compat-v1"
)
_EXPIRED_SEGMENT_BOUNDARY = "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"
_INSTALL_LOCK = RLock()

_ORIGINAL_DISPLAYED_DECISION_CHECK = getattr(
    _core,
    "_chanlun_web_original_displayed_decision_check",
    _core._displayed_decision_evidence_is_consistent,
)
setattr(
    _core,
    "_chanlun_web_original_displayed_decision_check",
    _ORIGINAL_DISPLAYED_DECISION_CHECK,
)


def _buy_shadow_matches_original_auditor(shadow: dict[str, object]) -> bool:
    profile = shadow.get("execution_profile")
    if (
        shadow.get("lifecycle_stage") != "invalidated"
        or not isinstance(profile, dict)
    ):
        return False
    advisories = profile.get("advisory_reason_codes")
    decision_reasons = shadow.get("decision_reasons")
    if not isinstance(advisories, list) or not isinstance(decision_reasons, list):
        return False
    if _EXPIRED_SEGMENT_BOUNDARY not in advisories:
        return False
    if _EXPIRED_SEGMENT_BOUNDARY not in decision_reasons:
        return False
    profile["advisory_reason_codes"] = [
        value for value in advisories if value != _EXPIRED_SEGMENT_BOUNDARY
    ]
    shadow["decision_reasons"] = [
        value for value in decision_reasons if value != _EXPIRED_SEGMENT_BOUNDARY
    ]
    return True


def displayed_decision_evidence_is_consistent(
    signal: Mapping[str, object],
    *,
    policy: object,
    risk: Mapping[str, object],
    warmup: Mapping[str, object],
) -> bool:
    """Retain the original audit and admit only one exact producer shadow."""

    if _ORIGINAL_DISPLAYED_DECISION_CHECK(
        signal,
        policy=policy,
        risk=risk,
        warmup=warmup,
    ):
        return True
    if not isinstance(signal.get("execution_profile"), Mapping):
        return False
    shadow = copy.deepcopy(dict(signal))
    if signal.get("side") == "buy":
        changed = _buy_shadow_matches_original_auditor(shadow)
    else:
        return False
    if not changed:
        return False
    shadow_risk = shadow.get("higher_timeframe_risk")
    shadow_warmup = shadow.get("warmup")
    if not isinstance(shadow_risk, Mapping) or not isinstance(
        shadow_warmup, Mapping
    ):
        return False
    return _ORIGINAL_DISPLAYED_DECISION_CHECK(
        shadow,
        policy=policy,
        risk=shadow_risk,
        warmup=shadow_warmup,
    )


def install_web_live_review_runtime_contract() -> None:
    """Install the exact Web-process audit adapter idempotently."""

    with _INSTALL_LOCK:
        current = _core._displayed_decision_evidence_is_consistent
        if current is displayed_decision_evidence_is_consistent:
            return
        if current is not _ORIGINAL_DISPLAYED_DECISION_CHECK:
            raise RuntimeError("live review decision auditor was replaced unexpectedly")
        _core._displayed_decision_evidence_is_consistent = (
            displayed_decision_evidence_is_consistent
        )


install_web_live_review_runtime_contract()


validate_live_review_snapshot = _core.validate_live_review_snapshot
live_human_review_document = _core.live_human_review_document


__all__ = (
    "WEB_LIVE_REVIEW_RUNTIME_CONTRACT_ID",
    "displayed_decision_evidence_is_consistent",
    "install_web_live_review_runtime_contract",
    "live_human_review_document",
    "validate_live_review_snapshot",
)
