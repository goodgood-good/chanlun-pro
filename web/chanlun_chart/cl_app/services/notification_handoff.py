"""Retain unsent events independently of the monitor's bounded page history."""

from copy import deepcopy
import re

from .dingtalk_notifications import eligible_signal_events


def pending_events(state, now, new=()):
    if not state.get("dingtalk_enabled"):
        return {}
    since = state.get("notification_since", float("inf"))
    # Old state files migrate their remaining recent history once. New state
    # uses its own journal, so a 500-row page cannot evict unqueued events.
    existing = state.get("pending_notifications")
    existing = state.get("events", []) if existing is None else existing.values()
    previous = eligible_signal_events(existing, since=since, stamp=now)
    incoming = eligible_signal_events(new, since=since, stamp=now)
    # Freeze the first event for this notification identity; in particular,
    # repeated observations cannot silently replace an illustrated warning.
    return {**incoming, **previous}


def retain(state, now, new=()):
    pending = pending_events(state, now, new)
    changed = pending != state.get("pending_notifications")
    state["pending_notifications"] = deepcopy(pending)
    return changed


def evidence_references(events):
    references = set()
    for event in events:
        for source in (event, event.get("invalidation_reference") or {}):
            run = source.get("evidence_run_id")
            if isinstance(run, str) and re.fullmatch(r"[a-f0-9]{32}", run):
                references.add(run)
    return references
