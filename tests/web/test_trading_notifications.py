from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
    assert title == "买卖通知｜候选股｜SZ.000001｜1分钟一类买点"
    rendered = "\n".join(lines)
    assert "失效价：9.80" in rendered
    assert "30分钟向上（有利）" in rendered
    assert "5分钟三类买点" in rendered
    assert "1分钟一类买点" in rendered
    assert "建议：确认反转后考虑分批买入" in rendered
    assert "计划风险倍数" not in rendered
    assert "结构层级" not in rendered


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
    assert "首次发现→1分钟已触发" in "\n".join(sender.messages[0][1])


@pytest.mark.parametrize(
    ("stage", "label"),
    (
        ("observed", "结构观察"),
        ("approaching", "即将确认"),
        ("armed", "已入观察池"),
        ("triggered", "1分钟已触发"),
        ("executable", "强提示待人工复核"),
        ("active", "持有跟踪"),
        ("invalidated", "结构已失效"),
        ("closed", "跟踪已结束"),
    ),
)
def test_notification_localizes_every_lifecycle_stage(
    stage: str,
    label: str,
) -> None:
    title, lines = format_notification(
        signal_document(stage),
        old_stage=stage,
        new_stage=stage,
    )

    assert label in "\n".join((title, *lines))
    assert f"{label}→{label}" in lines[0]
    assert f"{stage}→{stage}" not in lines[0]


def test_holding_source_is_explicitly_separated_from_candidate() -> None:
    signal = signal_document()
    signal["selection_sources"] = ["HOLDING_MONITOR"]

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert title == "买卖通知｜持仓股｜SZ.000001｜1分钟一类买点"
    assert "候选股" not in title
    assert lines[-1] == "建议：确认反转后考虑分批增持"


def test_watchlist_signal_remains_candidate_not_holding() -> None:
    signal = signal_document()
    signal["selection_sources"] = ["ACTIVE_WATCHLIST_MONITOR"]

    title, _lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert "｜候选股｜" in title
    assert "｜持仓股｜" not in title


def test_sell_and_invalidation_advice_are_explicit() -> None:
    sell = signal_document()
    sell["side"] = "sell"
    sell["point_type"] = "3sell"
    sell["setup_5m"] = {"point_type": "3sell", "center_ordinal": 1}
    sell["trigger_1m"] = {}

    title, lines = format_notification(
        sell,
        old_stage="armed",
        new_stage="triggered",
    )
    assert title.endswith("5分钟三类卖点")
    assert lines[-1] == "建议：优先检查退出条件"

    _title, invalidated_lines = format_notification(
        sell,
        old_stage="triggered",
        new_stage="invalidated",
    )
    assert invalidated_lines[-1] == "建议：取消该结构计划"


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
    health = dispatcher.health_snapshot()
    assert health["status"] == "verified"
    assert health["success_count"] == 1
    assert health["failure_count"] == 1
    assert health["delivered_event_count"] == 1


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
    assert persisted["success_count"] == 1
    assert persisted["failure_count"] == 0


def test_notification_health_records_failure_without_exposing_payload(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 13, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    dispatcher = SignalNotificationDispatcher(
        RecordingNotifier([False]),
        state_path=tmp_path / "delivered.json",
        clock=lambda: now,
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    health = dispatcher.health_snapshot()
    assert health["status"] == "degraded"
    assert health["reason_code"] == "LATEST_NOTIFICATION_DELIVERY_FAILED"
    assert health["last_failure_at"] == now.isoformat()
    assert health["last_failure_reason"] == "NOTIFIER_RETURNED_FALSE"
    assert health["credentials_exposed"] is False
    assert "webhook" not in json.dumps(health).lower()


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
