from __future__ import annotations

import json
from pathlib import Path

import pytest

from chanlun.notifications import DingTalkWebhookNotifier
from cl_app.services.trading_notifications import (
    SignalNotificationDispatcher,
    format_notification,
)


def signal_document(stage: str = "triggered") -> dict[str, object]:
    return {
        "signal_id": "signal:stable",
        "code": "SZ.000001",
        "point_type": "3buy",
        "tower": "xd",
        "recursive_level": 1,
        "lifecycle_stage": stage,
        "context_30m": {"direction": "up", "disposition": "supportive"},
        "setup_5m": {"point_type": "3buy", "center_ordinal": 1},
        "trigger_1m": {"point_type": "1buy", "confirmed_at": "2026-07-20T10:01:00+08:00"},
        "sector": {"sector_name": "银行", "regime": "supportive"},
        "structural_stop": "9.80",
        "risk_multiplier": "0.75",
        "decision_reasons": [],
    }


def snapshot(stage: str | None) -> dict[str, object]:
    return {"signals": [] if stage is None else [signal_document(stage)]}


class RecordingNotifier:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.messages: list[tuple[str, list[str]]] = []
        self.results = list(results or [True])

    def send(self, title: str, lines: list[str]) -> bool:
        self.messages.append((title, lines))
        return self.results.pop(0) if self.results else True


def test_only_material_lifecycle_transitions_notify(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))
    dispatcher.dispatch_changes(snapshot("triggered"), snapshot("triggered"))

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert "买卖通知" in title
    assert "结构失效价" in "\n".join(lines)
    assert "30m" in "\n".join(lines)
    assert "5m" in "\n".join(lines)
    assert "1m" in "\n".join(lines)


def test_newly_discovered_triggered_signal_notifies_immediately(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )

    dispatcher.dispatch_changes(snapshot(None), snapshot("triggered"))

    assert len(sender.messages) == 1
    assert "首次发现 → triggered" in "\n".join(sender.messages[0][1])


@pytest.mark.parametrize(
    ("old_stage", "new_stage"),
    (("armed", "invalidated"), ("triggered", "invalidated"), ("active", "closed")),
)
def test_invalidation_and_close_transitions_notify(
    tmp_path: Path,
    old_stage: str,
    new_stage: str,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / f"{old_stage}-{new_stage}.json",
    )

    dispatcher.dispatch_changes(snapshot(old_stage), snapshot(new_stage))

    assert len(sender.messages) == 1


def test_failed_send_remains_retryable(tmp_path: Path) -> None:
    sender = RecordingNotifier([False, True])
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))
    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    assert len(sender.messages) == 2


def test_persisted_event_id_deduplicates_after_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "delivered.json"
    first_sender = RecordingNotifier()
    SignalNotificationDispatcher(
        first_sender,
        state_path=state_path,
    ).dispatch_changes(snapshot("armed"), snapshot("triggered"))
    second_sender = RecordingNotifier()

    SignalNotificationDispatcher(
        second_sender,
        state_path=state_path,
    ).dispatch_changes(snapshot("armed"), snapshot("triggered"))

    assert len(first_sender.messages) == 1
    assert second_sender.messages == []
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == "chanlun-signal-notifications/v1"
    assert len(persisted["delivered_event_ids"]) == 1


def test_notification_payload_and_failure_log_never_contain_webhook(
    monkeypatch,
) -> None:
    import urllib.request

    secret = "https://example.invalid/send?credential=secret-url"
    warnings: list[str] = []
    title, lines = format_notification(
        signal_document(),
        old_stage="armed",
        new_stage="triggered",
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(f"network down for {secret}")
        ),
    )
    monkeypatch.setattr(
        "chanlun.notifications.fun.get_logger",
        lambda: type("Logger", (), {"warning": warnings.append})(),
    )

    notifier = DingTalkWebhookNotifier(secret, keyword="买卖通知")

    assert notifier.send(title, lines) is False
    rendered = json.dumps({"title": title, "lines": lines}, ensure_ascii=False)
    assert "secret-url" not in rendered
    assert warnings and "secret-url" not in "\n".join(warnings)


def test_dry_run_collects_without_network_or_stdout(monkeypatch, capsys) -> None:
    import urllib.request

    collected: list[str] = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not use network")
        ),
    )
    notifier = DingTalkWebhookNotifier(
        "https://example.invalid/send?credential=secret-url",
        keyword="买卖通知",
        dry_run=True,
        dry_run_collector=collected.append,
    )

    assert notifier.send("三类买点", ["SZ.000001"]) is True

    assert len(collected) == 1
    assert "买卖通知" in collected[0]
    assert "secret-url" not in collected[0]
    assert capsys.readouterr().out == ""


def test_webhook_notifier_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        DingTalkWebhookNotifier("https://example.invalid", timeout=0)
