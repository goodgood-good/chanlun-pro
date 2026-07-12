from __future__ import annotations

import re


MANUAL_CHECK_TRANSITION_ACTOR = "manual_check_workflow"
_PENDING_ID_PATTERN = r"manual-pending:[0-9a-f]{64}"
_FINGERPRINT_PATTERN = r"sha256:[0-9a-f]{64}"
_REASON_RE = re.compile(
    rf"manual_check_approved:({_PENDING_ID_PATTERN}):({_FINGERPRINT_PATTERN})"
)


def manual_check_transition_reason(
    pending_id: str,
    payload_fingerprint: str,
) -> str:
    reason = f"manual_check_approved:{pending_id}:{payload_fingerprint}"
    if _REASON_RE.fullmatch(reason) is None:
        raise ValueError("invalid manual check transition binding")
    return reason


def parse_manual_check_transition(
    actor: str,
    reason: str,
) -> tuple[str, str] | None:
    if actor != MANUAL_CHECK_TRANSITION_ACTOR:
        return None
    match = _REASON_RE.fullmatch(reason)
    if match is None:
        raise ValueError("invalid manual check transition binding")
    return match.group(1), match.group(2)


__all__ = [
    "MANUAL_CHECK_TRANSITION_ACTOR",
    "manual_check_transition_reason",
    "parse_manual_check_transition",
]
