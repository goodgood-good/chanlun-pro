"""Persistent, read-mostly projection for the chart decision-support API."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import re

from .event_store import (
    DecisionEventStore,
    EventNotFoundError,
    UserDecisionConflictError,
)
from .fingerprints import normalize_datetime
from .models import DecisionEvent, EventState, StrategyTrack


_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CURSOR_PAYLOAD_RE = re.compile(r"([0-9]{1,20}):([0-9a-f]{16})")


class ReadModelError(RuntimeError):
    pass


class ReadModelNotFound(ReadModelError):
    pass


class ReadModelConflict(ReadModelError):
    pass


def _event_cursor(event: DecisionEvent) -> str:
    timestamp_us = int(event.observed_at.timestamp() * 1_000_000)
    identity = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:16]
    payload = f"{timestamp_us}:{identity}".encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[int, str]:
    if not isinstance(value, str) or _CURSOR_RE.fullmatch(value) is None:
        raise ValueError("cursor is invalid")
    try:
        padding = "=" * (-len(value) % 4)
        payload = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        ).decode("ascii")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("cursor is invalid") from exc
    match = _CURSOR_PAYLOAD_RE.fullmatch(payload)
    if match is None:
        raise ValueError("cursor is invalid")
    return int(match.group(1)), match.group(2)


def _cursor_identity(event: DecisionEvent) -> tuple[int, str]:
    return (
        int(event.observed_at.timestamp() * 1_000_000),
        hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:16],
    )


def _transition_payload(value: object) -> dict[str, object]:
    return {
        "id": value.id,
        "event_id": value.event_id,
        "from_state": value.from_state.value,
        "to_state": value.to_state.value,
        "occurred_at": value.occurred_at.isoformat(),
        "reason": value.reason,
        "actor": value.actor,
    }


def _user_decision_payload(value: object) -> dict[str, object]:
    return {
        "decision_id": value.decision_id,
        "event_id": value.event_id,
        "user_id": value.user_id,
        "action": value.action,
        "note": value.note,
        "event_data_fingerprint": value.event_data_fingerprint,
        "idempotency_key": value.idempotency_key,
        "payload_fingerprint": value.payload_fingerprint,
        "decided_at": value.decided_at.isoformat(),
    }


class DecisionSupportReadModel:
    def __init__(
        self,
        store: DecisionEventStore,
        *,
        clock: Callable[[], datetime] | None = None,
        strategy_run: object | None = None,
    ) -> None:
        if not isinstance(store, DecisionEventStore):
            raise TypeError("store must be DecisionEventStore")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._strategy_run_binding: tuple[str, int, str] | None = None
        if strategy_run is not None:
            run_id = getattr(strategy_run, "run_id", None)
            epoch = getattr(strategy_run, "epoch", None)
            fingerprint = getattr(
                strategy_run,
                "strategy_run_fingerprint",
                None,
            )
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("strategy_run_id is invalid")
            if (
                isinstance(epoch, bool)
                or not isinstance(epoch, int)
                or epoch <= 0
            ):
                raise ValueError(
                    "strategy_run_epoch must be a positive integer"
                )
            if (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
            ):
                raise ValueError("strategy_run_fingerprint is invalid")
            self._strategy_run_binding = (run_id, epoch, fingerprint)

    def _now(self) -> datetime:
        return normalize_datetime(self._clock(), "clock")

    def _snapshot(self, event_id: str):
        try:
            return self.store.get_snapshot(event_id)
        except EventNotFoundError as exc:
            raise ReadModelNotFound("event_not_found") from exc

    def _event_matches_strategy_run(self, event: DecisionEvent) -> bool:
        binding = self._strategy_run_binding
        return binding is None or (
            event.strategy_run_id,
            event.strategy_run_epoch,
            event.strategy_run_fingerprint,
        ) == binding

    def _current_events(self) -> tuple[DecisionEvent, ...]:
        return tuple(
            event
            for event in self.store.list_current_strategy_events()
            if self._event_matches_strategy_run(event)
        )

    def _candidate(self, snapshot: object) -> dict[str, object]:
        event = snapshot.event
        current_strategy_run_match = self._event_matches_strategy_run(event)
        return {
            "event_id": event.event_id,
            "data_fingerprint": event.data_fingerprint,
            "market": event.market,
            "code": event.code,
            "name": event.name,
            "observed_at": event.observed_at.isoformat(),
            "bar_closed_at": event.bar_closed_at.isoformat(),
            "strategy_track": event.strategy_track.value,
            "state": snapshot.state.value,
            "rule_id": event.rule_id,
            "rule_card_version": event.rule_card_version,
            "strategy_run_id": event.strategy_run_id,
            "strategy_run_epoch": event.strategy_run_epoch,
            "strategy_run_fingerprint": event.strategy_run_fingerprint,
            "current_strategy_run_match": current_strategy_run_match,
            "operational_actions_allowed": current_strategy_run_match,
            "signal": event.to_dict()["signal"],
        }

    def candidates(
        self,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between one and one hundred")
        events = sorted(
            self._current_events(),
            key=lambda event: (event.observed_at, event.event_id),
            reverse=True,
        )
        start = 0
        if cursor is not None:
            identity = _decode_cursor(cursor)
            matching = [
                index
                for index, event in enumerate(events)
                if _cursor_identity(event) == identity
            ]
            if len(matching) != 1:
                raise ValueError("cursor is stale or invalid")
            start = matching[0] + 1
        selected = events[start : start + limit]
        cards = [self._candidate(self._snapshot(event.event_id)) for event in selected]
        end = start + len(selected)
        next_cursor = (
            _event_cursor(selected[-1])
            if selected and end < len(events)
            else None
        )
        return {
            "trend": [
                card
                for card in cards
                if card["strategy_track"]
                == StrategyTrack.TREND_CONTINUATION.value
            ],
            "reversal": [
                card
                for card in cards
                if card["strategy_track"] == StrategyTrack.BOTTOM_REVERSAL.value
            ],
            "next_cursor": next_cursor,
            "stale": False,
        }

    def event(self, event_id: str) -> dict[str, object]:
        snapshot = self._snapshot(event_id)
        risk_snapshots = self.store.list_risk_snapshots(event_id)
        user_decisions = self.store.list_user_decisions(event_id)
        event = snapshot.event
        current_strategy_run_match = self._event_matches_strategy_run(event)
        risk_payloads = [item.to_dict() for item in risk_snapshots]
        latest_risk = risk_snapshots[-1] if risk_snapshots else None
        risk_validation = (
            latest_risk.validate_for_review(event, as_of=self._now())
            if latest_risk is not None
            else None
        )
        if risk_validation is None:
            freshness = "risk_snapshot_missing"
        elif risk_validation.usable:
            freshness = "fresh"
        else:
            freshness = "blocked:" + ",".join(risk_validation.reasons)
        bs_type = event.signal.bs_type
        direction = (
            "buy"
            if "buy" in bs_type
            else "sell"
            if "sell" in bs_type
            else "observe"
        )
        stop_value = (
            event.signal.structural_stop_below
            if direction == "buy"
            else event.signal.structural_stop_above
        )
        decision = latest_risk.decision if latest_risk is not None else None
        plan = {
            "direction": direction,
            "trigger": bs_type,
            "entry_price": str(
                decision.entry_reference
                if decision is not None
                else event.signal.price
            ),
            "stop_price": None if stop_value is None else str(stop_value),
            "target_price": None,
            "risk_fraction": None,
            "target_weight": (
                None if decision is None else str(decision.target_weight)
            ),
            "planned_risk_cash": (
                None if decision is None else str(decision.planned_risk_cash)
            ),
            "position_size": None if decision is None else decision.shares,
            "risk_allowed": None if decision is None else decision.allowed,
            "risk_reasons": [] if decision is None else list(decision.reasons),
            "risk_snapshot_id": (
                None if latest_risk is None else latest_risk.snapshot_id
            ),
            "expires_at": (
                None if latest_risk is None else latest_risk.expires_at.isoformat()
            ),
            "earliest_sell": "T+1_next_verified_trading_day",
            "exit_rules": [
                "hard_risk_full_exit",
                "structural_invalidation_or_control_down_full_exit",
                "control_level_sell_full_exit",
                "operation_level_sell_layered_exit",
                "t1_or_limit_blocked_pending_retry",
            ],
        }
        return {
            "event_id": event.event_id,
            "event_data_fingerprint": event.data_fingerprint,
            "market": event.market,
            "code": event.code,
            "name": event.name,
            "observed_at": event.observed_at.isoformat(),
            "bar_closed_at": event.bar_closed_at.isoformat(),
            "strategy_track": event.strategy_track.value,
            "strategy_run_id": event.strategy_run_id,
            "strategy_run_epoch": event.strategy_run_epoch,
            "strategy_run_fingerprint": event.strategy_run_fingerprint,
            "freshness": freshness,
            "plan": plan,
            "event": event.to_dict(),
            "state": snapshot.state.value,
            "transitions": [
                _transition_payload(item) for item in snapshot.transitions
            ],
            "risk_snapshots": risk_payloads,
            "user_decisions": [
                _user_decision_payload(item) for item in user_decisions
            ],
            "current_strategy_run_match": current_strategy_run_match,
            "operational_actions_allowed": current_strategy_run_match,
        }

    def risk_status(self) -> dict[str, object]:
        events = self._current_events()
        as_of = self._now()
        fresh = 0
        unavailable = 0
        missing = 0
        daily_loss_locked = False
        drawdown_locked = False
        for event in events:
            snapshots = self.store.list_risk_snapshots(event.event_id)
            if not snapshots:
                missing += 1
                continue
            latest = snapshots[-1]
            validation = latest.validate_for_review(event, as_of=as_of)
            fresh += int(validation.usable)
            unavailable += int(not validation.usable)
            daily_loss_locked = (
                daily_loss_locked or latest.decision.daily_loss_locked
            )
            drawdown_locked = drawdown_locked or latest.decision.drawdown_locked
        reasons: list[str] = []
        if not events:
            reasons.append("no_decision_events")
        if events and fresh == 0:
            reasons.append("no_fresh_risk_snapshot")
        return {
            "available": fresh > 0,
            "as_of": as_of.isoformat(),
            "event_count": len(events),
            "fresh_snapshot_count": fresh,
            "unavailable_snapshot_count": unavailable,
            "missing_snapshot_count": missing,
            "daily_loss_locked": daily_loss_locked,
            "drawdown_locked": drawdown_locked,
            "reasons": reasons,
        }

    def record_user_decision(
        self,
        event_id: str,
        user_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        decided_at = self._now()
        try:
            snapshot = self._snapshot(event_id)
            if not self._event_matches_strategy_run(snapshot.event):
                raise ReadModelConflict("outside_current_strategy_run")
            action = payload.get("action")
            if action in {"accepted", "executed_externally"}:
                if snapshot.state is not EventState.CONFIRMED:
                    raise ReadModelConflict("event_not_confirmed")
                risk_snapshots = self.store.list_risk_snapshots(event_id)
                if not risk_snapshots:
                    raise ReadModelConflict("risk_snapshot_unavailable")
                validation = risk_snapshots[-1].validate_for_review(
                    snapshot.event,
                    as_of=decided_at,
                )
                if not validation.usable:
                    raise ReadModelConflict("risk_snapshot_unusable")
            stored = self.store.append_user_decision(
                event_id=event_id,
                user_id=user_id,
                action=action,
                note=payload.get("note"),
                event_data_fingerprint=payload.get(
                    "event_data_fingerprint"
                ),
                decided_at=decided_at,
                idempotency_key=payload.get("idempotency_key"),
            )
        except EventNotFoundError as exc:
            raise ReadModelNotFound("event_not_found") from exc
        except UserDecisionConflictError as exc:
            raise ReadModelConflict("user_decision_conflict") from exc
        return _user_decision_payload(stored)
