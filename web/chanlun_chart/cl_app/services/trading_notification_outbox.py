"""Durable background delivery for realtime trading notifications.

The screening loop must stop at the local durability boundary: once a signal
message has been atomically persisted, chart rendering and the external
webhook are independent delivery work.  This outbox deliberately provides
at-least-once delivery; the wrapped DingTalk notifier supplies the final
message-fingerprint dedupe barrier.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from math import isfinite
import os
from pathlib import Path
import threading
from typing import Any


SCHEMA = "chanlun-trading-notification-outbox-v1"
_DELIVERED_EVENT_LIMIT = 10_000


def _parse_aware(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _json_copy(value: object) -> Any:
    """Return the exact JSON-safe value that can cross a process restart."""

    return json.loads(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _fallback_event_id(
    title: str,
    lines: list[str] | str,
    context: Mapping[str, object],
) -> str:
    payload = json.dumps(
        {"title": title, "lines": lines, "context": context},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _context_event_id(context: Mapping[str, object]) -> str | None:
    direct = context.get("artifact_key")
    if isinstance(direct, str) and direct.startswith("sha256:") and len(direct) == 71:
        return direct
    charts = context.get("charts")
    if not isinstance(charts, Sequence) or isinstance(
        charts, (str, bytes, bytearray)
    ):
        return None
    for chart in charts:
        if not isinstance(chart, Mapping):
            continue
        value = chart.get("artifact_key")
        if isinstance(value, str) and value.startswith("sha256:") and len(value) == 71:
            return value
    return None


def _minimum_delivery_margin(
    context: Mapping[str, object],
) -> timedelta | None:
    raw = context.get("minimum_delivery_margin_seconds", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        return None
    seconds = float(raw)
    if not isfinite(seconds):
        return None
    try:
        return timedelta(seconds=seconds)
    except OverflowError:
        return None


class DurableTradingNotificationOutbox:
    """Persist messages immediately and deliver them on one background worker."""

    delivery_deferred = True

    def __init__(
        self,
        notifier: object,
        *,
        state_path: Path,
        clock: Callable[[], datetime] | None = None,
        delivery_observer: Callable[[str, str, str | None], None] | None = None,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
    ) -> None:
        if not callable(getattr(notifier, "send", None)):
            raise TypeError("notifier must expose send")
        if delivery_observer is not None and not callable(delivery_observer):
            raise TypeError("delivery_observer must be callable")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("notification outbox retry bounds are invalid")
        self._notifier = notifier
        self._state_path = Path(state_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._delivery_observer = delivery_observer
        self._retry_base_seconds = float(retry_base_seconds)
        self._retry_max_seconds = float(retry_max_seconds)
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_once = False
        self._in_flight_event_id: str | None = None
        self._log = logging.getLogger(__name__)
        self._state_load_error: str | None = None
        self._state = self._load_state()

    @property
    def available(self) -> bool:
        return bool(getattr(self._notifier, "available", True))

    @property
    def dry_run(self) -> bool:
        return getattr(self._notifier, "dry_run", False) is True

    @property
    def keyword(self) -> str:
        """Preserve the non-secret notifier configuration inspection surface."""

        return str(getattr(self._notifier, "keyword", ""))

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("notification outbox clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("notification outbox clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _empty_state() -> dict[str, object]:
        return {
            "pending_events": {},
            "delivered_event_ids": [],
            "expired_event_ids": [],
            "enqueued_count": 0,
            "success_count": 0,
            "simulated_success_count": 0,
            "failure_count": 0,
            "expired_count": 0,
            "last_enqueued_at": None,
            "last_success_at": None,
            "last_success_event_id": None,
            "last_failure_at": None,
            "last_failure_event_id": None,
            "last_failure_reason": None,
            "last_expired_at": None,
            "last_expired_event_id": None,
        }

    def _load_state(self) -> dict[str, object]:
        empty = self._empty_state()
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._state_load_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return empty

        def invalid(reason: str) -> dict[str, object]:
            self._state_load_error = reason
            return empty

        if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
            return invalid("OUTBOX_STATE_SCHEMA_INVALID")
        payload = dict(payload)
        # Migrate version-one queues written before stale-signal expiry was
        # tracked.  Existing pending rows remain intact and are revalidated.
        payload.setdefault("expired_event_ids", [])
        payload.setdefault("expired_count", 0)
        payload.setdefault("last_expired_at", None)
        payload.setdefault("last_expired_event_id", None)
        required = set(empty)
        if set(payload) != {"schema", *required}:
            return invalid("OUTBOX_STATE_FIELDS_INVALID")
        pending = payload.get("pending_events")
        delivered = payload.get("delivered_event_ids")
        expired = payload.get("expired_event_ids")
        if (
            not isinstance(pending, Mapping)
            or not isinstance(delivered, list)
            or not isinstance(expired, list)
        ):
            return invalid("OUTBOX_STATE_COLLECTIONS_INVALID")
        if any(
            not isinstance(value, str)
            or not value.startswith("sha256:")
            or len(value) != 71
            for value in [*delivered, *expired]
        ):
            return invalid("OUTBOX_TERMINAL_EVENT_IDS_INVALID")
        if delivered != sorted(set(delivered)) or expired != sorted(set(expired)):
            return invalid("OUTBOX_TERMINAL_EVENT_IDS_NOT_CANONICAL")
        if set(delivered) & set(expired):
            return invalid("OUTBOX_TERMINAL_EVENT_IDS_OVERLAP")
        normalized_pending: dict[str, dict[str, object]] = {}
        for event_id, raw in pending.items():
            normalized = dict(raw) if isinstance(raw, Mapping) else raw
            if isinstance(normalized, dict):
                # Version-one queues created before the review projection was
                # made durable have no transport checkpoint.  Treat them as
                # unsent so recovery preserves the original at-least-once
                # contract.
                normalized.setdefault("transport_status", None)
                normalized.setdefault("transport_completed_at", None)
            if not self._valid_pending_event(event_id, normalized):
                return invalid("OUTBOX_PENDING_EVENT_INVALID")
            normalized_pending[str(event_id)] = dict(normalized)
        for key in (
            "enqueued_count",
            "success_count",
            "simulated_success_count",
            "failure_count",
            "expired_count",
        ):
            if type(payload.get(key)) is not int or int(payload[key]) < 0:
                return invalid("OUTBOX_COUNTER_INVALID")
        for key in (
            "last_enqueued_at",
            "last_success_at",
            "last_failure_at",
            "last_expired_at",
        ):
            value = payload.get(key)
            if value is not None and _parse_aware(value) is None:
                return invalid("OUTBOX_TIMESTAMP_INVALID")
        for key in (
            "last_success_event_id",
            "last_failure_event_id",
            "last_failure_reason",
            "last_expired_event_id",
        ):
            value = payload.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                return invalid("OUTBOX_DIAGNOSTIC_FIELD_INVALID")
        return {
            **{key: payload[key] for key in empty if key != "pending_events"},
            "pending_events": normalized_pending,
            "delivered_event_ids": list(delivered),
        }

    @staticmethod
    def _valid_pending_event(event_id: object, raw: object) -> bool:
        if (
            not isinstance(event_id, str)
            or not event_id.startswith("sha256:")
            or len(event_id) != 71
            or not isinstance(raw, Mapping)
            or set(raw)
            != {
                "title",
                "lines",
                "lines_are_text",
                "context",
                "queued_at",
                "attempt_count",
                "last_attempt_at",
                "next_attempt_at",
                "last_error",
                "transport_status",
                "transport_completed_at",
            }
        ):
            return False
        lines = raw.get("lines")
        return bool(
            isinstance(raw.get("title"), str)
            and raw.get("title")
            and isinstance(raw.get("lines_are_text"), bool)
            and (
                isinstance(lines, str)
                if raw.get("lines_are_text") is True
                else isinstance(lines, list)
                and all(isinstance(value, str) for value in lines)
            )
            and isinstance(raw.get("context"), Mapping)
            and _minimum_delivery_margin(raw["context"]) is not None
            and _parse_aware(raw.get("queued_at")) is not None
            and type(raw.get("attempt_count")) is int
            and int(raw.get("attempt_count", -1)) >= 0
            and (
                raw.get("last_attempt_at") is None
                or _parse_aware(raw.get("last_attempt_at")) is not None
            )
            and _parse_aware(raw.get("next_attempt_at")) is not None
            and (
                raw.get("last_error") is None
                or isinstance(raw.get("last_error"), str)
            )
            and raw.get("transport_status")
            in {None, "delivered", "simulated", "expired"}
            and (
                raw.get("transport_completed_at") is None
                or _parse_aware(raw.get("transport_completed_at")) is not None
            )
        )

    @staticmethod
    def _expires_at(event: Mapping[str, object]) -> datetime | None:
        context = event.get("context")
        if not isinstance(context, Mapping):
            return None
        return _parse_aware(context.get("expires_at"))

    @classmethod
    def _expired(
        cls,
        event: Mapping[str, object],
        *,
        now: datetime,
    ) -> bool:
        expires_at = cls._expires_at(event)
        if expires_at is None:
            return False
        context = event.get("context")
        margin = (
            _minimum_delivery_margin(context)
            if isinstance(context, Mapping)
            else None
        )
        if margin is None:
            return True
        remaining = expires_at - now
        return (
            remaining <= timedelta(0)
            if margin == timedelta(0)
            else remaining < margin
        )

    @staticmethod
    def _delivery_priority(event: Mapping[str, object]) -> int:
        """Read an optional producer rank while preserving legacy queues."""

        context = event.get("context")
        value = (
            context.get("delivery_priority")
            if isinstance(context, Mapping)
            else None
        )
        return value if type(value) is int and 0 <= value <= 99 else 50

    def _persist_locked(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        payload = {"schema": SCHEMA, **self._state}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self._state_path)

    def _enqueue(
        self,
        title: str,
        lines: list[str] | str,
        context: Mapping[str, object],
    ) -> bool:
        normalized_title = str(title).strip()
        if not normalized_title:
            return False
        normalized_lines: list[str] | str = (
            str(lines)
            if isinstance(lines, str)
            else [str(value) for value in lines]
        )
        try:
            normalized_context = _json_copy(dict(context))
        except (TypeError, ValueError):
            return False
        if (
            normalized_context.get("expires_at") is not None
            and _parse_aware(normalized_context.get("expires_at")) is None
        ):
            return False
        if _minimum_delivery_margin(normalized_context) is None:
            return False
        event_id = _context_event_id(normalized_context) or _fallback_event_id(
            normalized_title,
            normalized_lines,
            normalized_context,
        )
        now = self._now().isoformat()
        with self._lock:
            if self._state_load_error is not None:
                # Never overwrite an unreadable durable queue with an empty
                # state.  Returning False leaves the complete signal payload
                # in the dispatcher's independent retry ledger.
                return False
            delivered = set(self._state["delivered_event_ids"])
            expired = set(self._state["expired_event_ids"])
            pending = self._state["pending_events"]
            if event_id in delivered or event_id in expired or event_id in pending:
                return True
            pending[event_id] = {
                "title": normalized_title,
                "lines": normalized_lines,
                "lines_are_text": isinstance(normalized_lines, str),
                "context": normalized_context,
                "queued_at": now,
                "attempt_count": 0,
                "last_attempt_at": None,
                "next_attempt_at": now,
                "last_error": None,
                "transport_status": None,
                "transport_completed_at": None,
            }
            self._state["enqueued_count"] += 1
            self._state["last_enqueued_at"] = now
            try:
                self._persist_locked()
            except OSError:
                pending.pop(event_id, None)
                self._state["enqueued_count"] -= 1
                raise
        self._wake.set()
        return True

    def send(self, title: str, lines: list[str] | str) -> bool:
        return self._enqueue(title, lines, {})

    def send_rich(
        self,
        title: str,
        lines: list[str] | str,
        context: Mapping[str, object],
    ) -> bool:
        return self._enqueue(title, lines, context)

    def _claim_due_event(self) -> tuple[str, dict[str, object]] | None:
        now = self._now()
        with self._lock:
            if self._in_flight_event_id is not None:
                return None
            pending = self._state["pending_events"]
            candidates = []
            for event_id, value in pending.items():
                next_attempt = _parse_aware(value.get("next_attempt_at"))
                if not (
                    next_attempt is not None and next_attempt <= now
                    or self._expired(value, now=now)
                ):
                    continue
                queued_at = _parse_aware(value.get("queued_at"))
                candidates.append(
                    (
                        self._delivery_priority(value),
                        next_attempt or datetime.max.replace(tzinfo=timezone.utc),
                        queued_at or datetime.max.replace(tzinfo=timezone.utc),
                        event_id,
                        value,
                    )
                )
            if not candidates:
                return None
            _priority, _next_at, _queued_at, event_id, raw = min(candidates)
            self._in_flight_event_id = event_id
            return event_id, _json_copy(raw)

    def _observe_delivery(
        self,
        event_id: str,
        status: str,
        reason: str | None,
    ) -> tuple[bool, str | None]:
        if self._delivery_observer is None:
            return True, None
        try:
            self._delivery_observer(event_id, status, reason)
            return True, None
        except Exception as exc:
            self._log.exception("failed to update notification delivery projection")
            return (
                False,
                "REVIEW_PROJECTION_FAILED:"
                f"{type(exc).__name__}: {str(exc)[:120]}",
            )

    def deliver_pending_once(self) -> bool:
        """Attempt one event and always release its in-flight ownership."""

        try:
            return self._deliver_pending_once()
        except Exception:
            # A transient atomic-write failure must not strand the queue or
            # terminate the background worker with a permanently claimed row.
            # Any transport checkpoint already installed in memory remains in
            # place, so the next attempt updates the review projection without
            # sending the external message again.
            with self._lock:
                self._in_flight_event_id = None
            raise

    def _deliver_pending_once(self) -> bool:
        """Attempt one due event without holding the producer/state lock."""

        claimed = self._claim_due_event()
        if claimed is None:
            return False
        event_id, event = claimed
        transport_status = event.get("transport_status")
        attempt_started_at = self._now()
        transport_completed_at = (
            _parse_aware(event.get("transport_completed_at"))
            or attempt_started_at
        )
        expired = transport_status == "expired" or (
            transport_status is None
            and self._expired(event, now=attempt_started_at)
        )
        sent = transport_status in {"delivered", "simulated"}
        failure_reason: str | None = None
        if expired:
            if transport_status is None:
                # Checkpoint terminal suppression before updating the review
                # projection.  Projection retries can then never leak a stale
                # signal to the external transport.
                with self._lock:
                    current = self._state["pending_events"].get(event_id)
                    if not isinstance(current, Mapping):
                        self._in_flight_event_id = None
                        return True
                    current["transport_status"] = "expired"
                    current["transport_completed_at"] = attempt_started_at.isoformat()
                    current["last_attempt_at"] = attempt_started_at.isoformat()
                    self._persist_locked()
                transport_status = "expired"
                transport_completed_at = attempt_started_at
        elif not sent:
            try:
                send_rich = getattr(self._notifier, "send_rich", None)
                context = event["context"]
                chart_required = bool(
                    isinstance(context, Mapping)
                    and context.get("require_chart") is True
                )
                if chart_required and not callable(send_rich):
                    sent = False
                    failure_reason = "REQUIRED_CHART_TRANSPORT_UNAVAILABLE"
                elif callable(send_rich) and context:
                    sent = bool(
                        send_rich(event["title"], event["lines"], context)
                    )
                else:
                    sent = bool(self._notifier.send(event["title"], event["lines"]))
                if not sent and failure_reason is None:
                    failure_reason = "NOTIFIER_RETURNED_FALSE"
            except Exception as exc:
                failure_reason = f"{type(exc).__name__}: {str(exc)[:120]}"
            finally:
                # Webhook/图表发送可能持续数秒；完成时间必须在外部调用返回后读取，
                # 不能把尝试开始时间伪装成真实送达时间。
                transport_completed_at = self._now()

        observer_status = (
            str(transport_status)
            if transport_status in {"delivered", "simulated", "expired"}
            else ("simulated" if self.dry_run and sent else "delivered")
        )
        if sent and transport_status is None:
            # Checkpoint the external success before touching the secondary
            # review projection.  A projection retry therefore never sends the
            # DingTalk message a second time.
            with self._lock:
                current = self._state["pending_events"].get(event_id)
                if not isinstance(current, Mapping):
                    self._in_flight_event_id = None
                    return True
                current["transport_status"] = observer_status
                current["transport_completed_at"] = transport_completed_at.isoformat()
                current["last_attempt_at"] = transport_completed_at.isoformat()
                self._persist_locked()

        observer_ok = True
        observer_error = None
        if sent or expired:
            observer_ok, observer_error = self._observe_delivery(
                event_id,
                observer_status,
                (
                    "NOTIFICATION_DELIVERY_EXPIRED"
                    if expired
                    else None
                ),
            )
        else:
            # A transport failure remains retryable regardless of whether the
            # diagnostic projection itself is temporarily unavailable.
            self._observe_delivery(event_id, "failed", failure_reason)

        finalized_at = self._now()
        with self._lock:
            try:
                current = self._state["pending_events"].get(event_id)
                if not isinstance(current, Mapping):
                    return True
                attempt_count = int(current.get("attempt_count", 0)) + 1
                if (sent or expired) and observer_ok:
                    completed_at_text = str(
                        current.get("transport_completed_at")
                        or transport_completed_at.isoformat()
                    )
                    self._state["pending_events"].pop(event_id, None)
                    if expired:
                        expired_ids = set(self._state["expired_event_ids"])
                        expired_ids.add(event_id)
                        self._state["expired_event_ids"] = sorted(expired_ids)[
                            -_DELIVERED_EVENT_LIMIT:
                        ]
                        self._state["expired_count"] += 1
                        self._state["last_expired_at"] = completed_at_text
                        self._state["last_expired_event_id"] = event_id
                    else:
                        delivered = set(self._state["delivered_event_ids"])
                        delivered.add(event_id)
                        self._state["delivered_event_ids"] = sorted(delivered)[
                            -_DELIVERED_EVENT_LIMIT:
                        ]
                        if self.dry_run:
                            self._state["simulated_success_count"] += 1
                            observer_status = "simulated"
                        else:
                            self._state["success_count"] += 1
                            observer_status = "delivered"
                        self._state["last_success_at"] = completed_at_text
                        self._state["last_success_event_id"] = event_id
                else:
                    retry_reason = observer_error or failure_reason
                    delay = min(
                        self._retry_max_seconds,
                        self._retry_base_seconds * (2 ** min(attempt_count - 1, 12)),
                    )
                    current.update(
                        {
                            "attempt_count": attempt_count,
                            "last_attempt_at": finalized_at.isoformat(),
                            "next_attempt_at": (
                                finalized_at + timedelta(seconds=delay)
                            ).isoformat(),
                            "last_error": retry_reason,
                        }
                    )
                    self._state["failure_count"] += 1
                    self._state["last_failure_at"] = finalized_at.isoformat()
                    self._state["last_failure_event_id"] = event_id
                    self._state["last_failure_reason"] = retry_reason
                self._persist_locked()
            finally:
                self._in_flight_event_id = None
        return True

    def _next_wait_seconds(self) -> float:
        now = self._now()
        with self._lock:
            due_times: list[datetime] = []
            for raw in self._state["pending_events"].values():
                next_attempt = _parse_aware(raw.get("next_attempt_at"))
                expires_at = (
                    None
                    if raw.get("transport_status") is not None
                    else self._expires_at(raw)
                )
                context = raw.get("context")
                margin = (
                    _minimum_delivery_margin(context)
                    if isinstance(context, Mapping)
                    else None
                )
                delivery_cutoff = (
                    expires_at - margin
                    if expires_at is not None and margin is not None
                    else expires_at
                )
                due_times.extend(
                    value
                    for value in (next_attempt, delivery_cutoff)
                    if value is not None
                )
        if not due_times:
            return 30.0
        return max(0.0, min(30.0, (min(due_times) - now).total_seconds()))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delivered = self.deliver_pending_once()
            except Exception:
                self._log.exception("notification outbox delivery worker recovered")
                self._wake.wait(timeout=min(30.0, self._retry_base_seconds))
                self._wake.clear()
                continue
            if delivered:
                continue
            self._wake.wait(timeout=self._next_wait_seconds())
            self._wake.clear()

    def start_background(self) -> threading.Thread:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._wake.set()
            self._thread = threading.Thread(
                target=self._run,
                name="trading-notification-outbox",
                daemon=True,
            )
            self._started_once = True
            self._thread.start()
            return self._thread

    def shutdown_background(
        self,
        *,
        wait: bool = True,
        timeout: float | None = 2.0,
    ) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            self._stop.set()
            self._wake.set()
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lifecycle_lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None

    def health_snapshot(self) -> dict[str, object]:
        now = self._now()
        with self._lock:
            pending = list(self._state["pending_events"].values())
            queued_times = [
                parsed
                for raw in pending
                if (parsed := _parse_aware(raw.get("queued_at"))) is not None
            ]
            retrying_count = sum(bool(raw.get("last_error")) for raw in pending)
            review_projection_pending_count = sum(
                raw.get("transport_status") in {"delivered", "simulated"}
                for raw in pending
            )
            thread = self._thread
            worker_alive = bool(thread is not None and thread.is_alive())
            delivered_count = len(self._state["delivered_event_ids"])
            expired_count = int(self._state["expired_count"])
            last_success = _parse_aware(self._state["last_success_at"])
            last_expired = _parse_aware(self._state["last_expired_at"])
            expiry_unrecovered = last_expired is not None and (
                last_success is None or last_expired > last_success
            )
            configured = self.available
            if self._state_load_error is not None:
                status = "unavailable"
                reason = "NOTIFICATION_OUTBOX_STATE_INVALID"
            elif not configured:
                status = "unavailable"
                reason = "EXTERNAL_NOTIFICATION_TRANSPORT_NOT_CONFIGURED"
            elif self._started_once and not worker_alive:
                status = "unavailable"
                reason = "NOTIFICATION_OUTBOX_WORKER_STOPPED"
            elif review_projection_pending_count:
                status = "degraded"
                reason = "REVIEW_INBOX_PROJECTION_RETRYING"
            elif retrying_count:
                status = "degraded"
                reason = "NOTIFICATION_OUTBOX_RETRYING"
            elif pending:
                status = "queued"
                reason = "NOTIFICATION_DELIVERY_QUEUED"
            elif expiry_unrecovered:
                status = "degraded"
                reason = "NOTIFICATION_DELIVERY_EXPIRED"
            elif delivered_count:
                status = "verified"
                reason = "DELIVERY_SUCCESS_PROVEN"
            else:
                status = "awaiting_first_delivery"
                reason = "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED"
            oldest = min(queued_times) if queued_times else None
            return {
                "schema": "chanlun-signal-notification-readiness",
                "configured": configured,
                "operationally_verified": bool(delivered_count),
                "status": status,
                "reason_code": reason,
                "delivery_mode": "DURABLE_BACKGROUND_OUTBOX",
                "worker_alive": worker_alive,
                "pending_event_count": len(pending),
                "retrying_event_count": retrying_count,
                "review_projection_pending_event_count": (
                    review_projection_pending_count
                ),
                "delivery_observer_configured": (
                    self._delivery_observer is not None
                ),
                "oldest_pending_at": None if oldest is None else oldest.isoformat(),
                "oldest_pending_age_seconds": (
                    None
                    if oldest is None
                    else max(0.0, (now - oldest).total_seconds())
                ),
                "in_flight_event_id": self._in_flight_event_id,
                "delivered_event_count": delivered_count,
                "expired_event_count": expired_count,
                "enqueued_count": self._state["enqueued_count"],
                "success_count": self._state["success_count"],
                "simulated_success_count": self._state["simulated_success_count"],
                "failure_count": self._state["failure_count"],
                "last_enqueued_at": self._state["last_enqueued_at"],
                "last_success_at": self._state["last_success_at"],
                "last_success_event_id": self._state["last_success_event_id"],
                "last_failure_at": self._state["last_failure_at"],
                "last_failure_event_id": self._state["last_failure_event_id"],
                "last_failure_reason": self._state["last_failure_reason"],
                "last_expired_at": self._state["last_expired_at"],
                "last_expired_event_id": self._state["last_expired_event_id"],
                "state_load_error": self._state_load_error,
                "credentials_exposed": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }


__all__ = ("DurableTradingNotificationOutbox", "SCHEMA")
