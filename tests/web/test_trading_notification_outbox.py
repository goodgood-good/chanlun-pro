from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from cl_app.services.realtime_review_inbox import RealtimeReviewInbox
from cl_app.services.trading_notification_outbox import (
    DurableTradingNotificationOutbox,
)
from cl_app.services.trading_notifications import SignalNotificationDispatcher


CN = ZoneInfo("Asia/Shanghai")


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 15, 10, 1, 30, tzinfo=CN)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class RecordingTransport:
    available = True
    dry_run = False

    def __init__(self, results: list[bool] | None = None) -> None:
        self.results = list(results or [True])
        self.messages: list[tuple[str, list[str] | str, dict[str, object]]] = []

    def send(self, title: str, lines: list[str] | str) -> bool:
        self.messages.append((title, lines, {}))
        return self.results.pop(0) if self.results else True

    def send_rich(
        self,
        title: str,
        lines: list[str] | str,
        context: dict[str, object],
    ) -> bool:
        self.messages.append((title, lines, context))
        return self.results.pop(0) if self.results else True


class AdvancingTransport(RecordingTransport):
    def __init__(self, clock: MutableClock, seconds: float) -> None:
        super().__init__()
        self.clock = clock
        self.seconds = seconds

    def send_rich(
        self,
        title: str,
        lines: list[str] | str,
        context: dict[str, object],
    ) -> bool:
        self.messages.append((title, lines, context))
        self.clock.advance(self.seconds)
        return True

def event_context(event_id: str) -> dict[str, object]:
    return {
        "require_evidence_match": True,
        "charts": [{"artifact_key": event_id, "code": "SZ.000001"}],
    }


def signal_document(stage: str) -> dict[str, object]:
    return {
        "signal_id": "signal:outbox",
        "code": "SZ.000001",
        "name": "平安银行",
        "side": "buy",
        "point_id": "setup:outbox",
        "point_type": "3buy",
        "lifecycle_stage": stage,
        "physical_timeframe_recursive": True,
        "observed_at": "2026-08-15T10:01:30+08:00",
        "monitor_observed_at": "2026-08-15T10:01:30+08:00",
        "current_price": 10.25,
        "context_30m": {"direction": "up", "disposition": "supportive"},
        "setup_5m": {
            "point_id": "setup:outbox",
            "point_type": "3buy",
            "side": "buy",
            "recursive_level": 0,
            "center_ordinal": 1,
            "status": "confirmed",
            "source_frequency": "5m",
            "actionable": True,
            "available_at": "2026-08-15T10:00:00+08:00",
            "invalidation_price": "9.80",
        },
        "trigger_1m": {
            "point_id": "trigger:outbox",
            "point_type": "1buy",
            "side": "buy",
            "recursive_level": 0,
            "anchor_at": "2026-08-15T09:58:00+08:00",
            "status": "confirmed",
            "source_frequency": "1m",
            "actionable": True,
            "available_at": "2026-08-15T10:01:00+08:00",
            "confirmed_at": "2026-08-15T10:01:00+08:00",
        },
        "sector": {"sector_name": "银行", "regime": "supportive"},
        "sector_triggered": True,
        "higher_timeframe_risk": {
            "market_gate": "GREEN",
            "sector_gate": "GREEN",
            "symbol_gate": "GREEN",
        },
        "warmup": {"converged": True},
        "conflict": {"hard_block": False},
        "entry_allowed": True,
        "exit_allowed": False,
        "entry_execution_boundary": {
            "confirmation_bar_closed_at": "2026-08-15T10:01:00+08:00",
            "entry_valid_until": "2026-08-15T10:02:00+08:00",
        },
        "decision_reasons": [],
    }


def test_outbox_persists_before_external_delivery(tmp_path: Path) -> None:
    clock = MutableClock()
    transport = RecordingTransport()
    path = tmp_path / "outbox.json"
    outbox = DurableTradingNotificationOutbox(
        transport,
        state_path=path,
        clock=clock,
    )
    event_id = "sha256:" + "1" * 64

    assert outbox.send_rich("买卖通知", ["一条信号"], event_context(event_id))

    assert transport.messages == []
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert list(persisted["pending_events"]) == [event_id]
    assert outbox.health_snapshot()["pending_event_count"] == 1

    assert outbox.deliver_pending_once() is True
    assert len(transport.messages) == 1
    assert outbox.health_snapshot()["pending_event_count"] == 0
    assert outbox.health_snapshot()["delivered_event_count"] == 1


def test_outbox_claims_due_sell_priority_before_event_hash_order(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    transport = RecordingTransport()
    outbox = DurableTradingNotificationOutbox(
        transport,
        state_path=tmp_path / "outbox.json",
        clock=clock,
    )
    lexical_first = "sha256:" + "0" * 64
    risk_first = "sha256:" + "f" * 64
    assert outbox.send_rich(
        "低优先买点",
        ["buy"],
        {**event_context(lexical_first), "delivery_priority": 3},
    )
    assert outbox.send_rich(
        "高优先卖点",
        ["sell"],
        {**event_context(risk_first), "delivery_priority": 0},
    )

    assert outbox.deliver_pending_once() is True

    assert transport.messages[0][0] == "高优先卖点"


def test_failed_delivery_survives_restart_and_retries_after_backoff(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    path = tmp_path / "outbox.json"
    event_id = "sha256:" + "2" * 64
    failed_transport = RecordingTransport([False])
    first = DurableTradingNotificationOutbox(
        failed_transport,
        state_path=path,
        clock=clock,
        retry_base_seconds=5,
    )
    assert first.send_rich("买卖通知", ["重试信号"], event_context(event_id))
    assert first.deliver_pending_once() is True
    assert first.health_snapshot()["retrying_event_count"] == 1

    recovered_transport = RecordingTransport([True])
    recovered = DurableTradingNotificationOutbox(
        recovered_transport,
        state_path=path,
        clock=clock,
        retry_base_seconds=5,
    )
    assert recovered.deliver_pending_once() is False
    clock.advance(5)
    assert recovered.deliver_pending_once() is True
    assert len(recovered_transport.messages) == 1
    assert recovered.health_snapshot()["delivered_event_count"] == 1


def test_expired_realtime_event_is_retained_for_review_but_never_sent_stale(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    transport = RecordingTransport([False, True])
    observations: list[tuple[str, str, str | None]] = []

    def observe(event_id: str, status: str, reason: str | None) -> None:
        observations.append((event_id, status, reason))

    outbox = DurableTradingNotificationOutbox(
        transport,
        state_path=tmp_path / "outbox.json",
        clock=clock,
        delivery_observer=observe,
        retry_base_seconds=5,
    )
    event_id = "sha256:" + "e" * 64
    context = {
        **event_context(event_id),
        "expires_at": (clock.value + timedelta(seconds=30)).isoformat(),
    }
    assert outbox.send_rich("买卖通知", ["时效信号"], context)
    assert outbox.deliver_pending_once() is True
    assert len(transport.messages) == 1

    clock.advance(31)
    assert outbox.deliver_pending_once() is True

    assert len(transport.messages) == 1
    assert observations[-1] == (
        event_id,
        "expired",
        "NOTIFICATION_DELIVERY_EXPIRED",
    )
    health = outbox.health_snapshot()
    assert health["pending_event_count"] == 0
    assert health["expired_event_count"] == 1
    assert health["reason_code"] == "NOTIFICATION_DELIVERY_EXPIRED"


def test_invalid_outbox_state_is_not_silently_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    path.write_text("{broken", encoding="utf-8")
    outbox = DurableTradingNotificationOutbox(
        RecordingTransport(),
        state_path=path,
        clock=MutableClock(),
    )

    assert outbox.send("买卖通知", ["不能覆盖旧队列"]) is False
    assert path.read_text(encoding="utf-8") == "{broken"
    health = outbox.health_snapshot()
    assert health["status"] == "unavailable"
    assert health["reason_code"] == "NOTIFICATION_OUTBOX_STATE_INVALID"


def test_dispatcher_records_pending_until_outbox_transport_succeeds(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    transport = RecordingTransport()
    inbox = RealtimeReviewInbox(tmp_path / "review.json", clock=clock)

    def observe(event_id: str, status: str, reason: str | None) -> None:
        inbox.update_delivery([event_id], status=status, reason=reason)

    outbox = DurableTradingNotificationOutbox(
        transport,
        state_path=tmp_path / "outbox.json",
        clock=clock,
        delivery_observer=observe,
    )
    dispatcher = SignalNotificationDispatcher(
        outbox,
        state_path=tmp_path / "dispatcher.json",
        clock=clock,
        review_inbox=inbox,
    )

    dispatcher.dispatch_changes(
        {"signals": [signal_document("armed")]},
        {"signals": [signal_document("triggered")]},
    )

    assert transport.messages == []
    assert inbox.snapshot()["events"][0]["delivery_status"] == "pending"
    persisted = json.loads(
        (tmp_path / "outbox.json").read_text(encoding="utf-8")
    )
    queued = next(iter(persisted["pending_events"].values()))
    assert queued["context"]["expires_at"] == "2026-08-15T10:11:30+08:00"
    assert queued["context"]["delivery_priority"] == 3
    dispatcher_health = dispatcher.health_snapshot()
    assert dispatcher_health["delivery_mode"] == "DURABLE_BACKGROUND_OUTBOX"
    assert dispatcher_health["outbox_pending_event_count"] == 1

    assert outbox.deliver_pending_once() is True
    assert inbox.snapshot()["events"][0]["delivery_status"] == "delivered"
    assert dispatcher.health_snapshot()["delivered_event_count"] == 1


def test_review_projection_failure_retries_without_resending_transport(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    transport = RecordingTransport()
    observations: list[tuple[str, str]] = []

    def observe(event_id: str, status: str, _reason: str | None) -> None:
        observations.append((event_id, status))
        if len(observations) == 1:
            raise OSError("review store temporarily unavailable")

    outbox = DurableTradingNotificationOutbox(
        transport,
        state_path=tmp_path / "outbox.json",
        clock=clock,
        delivery_observer=observe,
        retry_base_seconds=5,
    )
    event_id = "sha256:" + "3" * 64
    assert outbox.send_rich("买卖通知", ["复核重试"], event_context(event_id))

    assert outbox.deliver_pending_once() is True
    assert len(transport.messages) == 1
    health = outbox.health_snapshot()
    assert health["reason_code"] == "REVIEW_INBOX_PROJECTION_RETRYING"
    assert health["review_projection_pending_event_count"] == 1

    clock.advance(5)
    assert outbox.deliver_pending_once() is True
    assert len(transport.messages) == 1
    assert len(observations) == 2
    assert outbox.health_snapshot()["delivered_event_count"] == 1


def test_transport_completion_time_is_captured_after_slow_send_and_survives_retry(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    started_at = clock.value
    transport = AdvancingTransport(clock, 12)
    observations = 0

    def observe(_event_id: str, _status: str, _reason: str | None) -> None:
        nonlocal observations
        observations += 1
        if observations == 1:
            raise OSError("review store temporarily unavailable")

    path = tmp_path / "outbox.json"
    outbox = DurableTradingNotificationOutbox(
        transport,
        state_path=path,
        clock=clock,
        delivery_observer=observe,
        retry_base_seconds=5,
    )
    event_id = "sha256:" + "a" * 64
    assert outbox.send_rich("买卖通知", ["慢发送"], event_context(event_id))

    assert outbox.deliver_pending_once() is True
    completed_at = started_at + timedelta(seconds=12)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert (
        persisted["pending_events"][event_id]["transport_completed_at"]
        == completed_at.astimezone(timezone.utc).isoformat()
    )

    clock.advance(5)
    assert outbox.deliver_pending_once() is True
    assert len(transport.messages) == 1
    assert outbox.health_snapshot()["last_success_at"] == (
        completed_at.astimezone(timezone.utc).isoformat()
    )


def test_transport_checkpoint_write_failure_does_not_strand_or_resend_event(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    transport = RecordingTransport()
    outbox = DurableTradingNotificationOutbox(
        transport,
        state_path=tmp_path / "outbox.json",
        clock=clock,
    )
    event_id = "sha256:" + "4" * 64
    assert outbox.send_rich("买卖通知", ["写盘恢复"], event_context(event_id))
    persist = outbox._persist_locked
    failed = False

    def fail_first_transport_checkpoint() -> None:
        nonlocal failed
        pending = outbox._state["pending_events"].get(event_id)
        if (
            not failed
            and pending is not None
            and pending.get("transport_status") == "delivered"
        ):
            failed = True
            raise OSError("temporary atomic replace failure")
        persist()

    outbox._persist_locked = fail_first_transport_checkpoint  # type: ignore[method-assign]

    try:
        outbox.deliver_pending_once()
    except OSError:
        pass
    else:
        raise AssertionError("the injected checkpoint failure was not observed")
    assert len(transport.messages) == 1
    assert outbox.health_snapshot()["in_flight_event_id"] is None

    assert outbox.deliver_pending_once() is True
    assert len(transport.messages) == 1
    assert outbox.health_snapshot()["delivered_event_count"] == 1


def test_dispatcher_never_enqueues_before_review_record_is_durable(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    transport = RecordingTransport()
    real_inbox = RealtimeReviewInbox(tmp_path / "review.json", clock=clock)

    class FlakyInbox:
        def __init__(self) -> None:
            self.calls = 0

        def record(self, event) -> None:
            self.calls += 1
            if self.calls == 1:
                raise OSError("disk unavailable")
            real_inbox.record(event)

    flaky = FlakyInbox()
    outbox = DurableTradingNotificationOutbox(
        transport,
        state_path=tmp_path / "outbox.json",
        clock=clock,
    )
    dispatcher = SignalNotificationDispatcher(
        outbox,
        state_path=tmp_path / "dispatcher.json",
        clock=clock,
        review_inbox=flaky,
    )
    before = {"signals": [signal_document("armed")]}
    after = {"signals": [signal_document("triggered")]}

    dispatcher.dispatch_changes(before, after)
    assert transport.messages == []
    assert outbox.health_snapshot()["pending_event_count"] == 0
    assert dispatcher.health_snapshot()["pending_trigger_event_count"] == 1

    dispatcher.dispatch_changes(before, after)
    assert outbox.health_snapshot()["pending_event_count"] == 1
    assert real_inbox.snapshot()["event_count"] == 1
