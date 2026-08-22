from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chanlun.notifications import DingTalkWebhookNotifier
from chanlun.decision_support.trading_system.position_recommendation import (
    build_position_recommendation,
)
from cl_app.services.realtime_review_inbox import RealtimeReviewInbox
from cl_app.services.trading_notifications import (
    SignalNotificationDispatcher,
    format_notification,
)


TEST_NOW = datetime(
    2026,
    7,
    20,
    10,
    1,
    30,
    tzinfo=ZoneInfo("Asia/Shanghai"),
)


@pytest.fixture(autouse=True)
def _freeze_dispatch_clock(monkeypatch) -> None:
    original = SignalNotificationDispatcher.__init__

    def frozen_init(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: TEST_NOW)
        original(self, *args, **kwargs)

    monkeypatch.setattr(SignalNotificationDispatcher, "__init__", frozen_init)


def signal_document(stage: str = "triggered") -> dict[str, object]:
    return {
        "signal_id": "signal:stable",
        "point_id": "setup:stable-5m-3buy",
        "code": "SZ.000001",
        "side": "buy",
        "point_type": "3buy",
        "tower": "xd",
        "recursive_level": 0,
        "lifecycle_stage": stage,
        "physical_timeframe_recursive": True,
        "observed_at": "2026-07-20T10:01:30+08:00",
        "current_price": 10.25,
        "context_30m": {"direction": "up", "disposition": "supportive"},
        "setup_5m": {
            "point_id": "setup:stable-5m-3buy",
            "point_type": "3buy",
            "side": "buy",
            "center_ordinal": 1,
            "status": "confirmed",
            "source_frequency": "5m",
            "actionable": True,
            "recursive_level": 0,
            "anchor_at": "2026-07-20T09:55:00+08:00",
            "confirmed_at": "2026-07-20T10:00:00+08:00",
            "available_at": "2026-07-20T10:00:00+08:00",
            "anchor_price": "10.00",
            "structure_anchor_price": "10.00",
            "invalidation_price": "9.80",
        },
        "trigger_1m": {
            "point_id": "trigger:stable-1m-1buy",
            "point_type": "1buy",
            "side": "buy",
            "recursive_level": 0,
            "anchor_at": "2026-07-20T09:58:00+08:00",
            "status": "confirmed",
            "source_frequency": "1m",
            "actionable": True,
            "available_at": "2026-07-20T10:01:00+08:00",
            "confirmed_at": "2026-07-20T10:01:00+08:00",
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
            "confirmation_bar_closed_at": "2026-07-20T10:01:00+08:00",
            "entry_valid_until": "2026-07-20T10:02:00+08:00",
        },
        "risk_multiplier": "0.75",
        "position_recommendation": build_position_recommendation(
            side="buy",
            recommendation="READY",
            risk_multiplier="0.75",
            context_risk_scale="1.00",
            entry_price="10.00",
            structural_stop="9.80",
            exit_action="none",
        ).document(),
        "decision_reasons": [],
    }


def snapshot(stage: str | None) -> dict[str, object]:
    return {"signals": [] if stage is None else [signal_document(stage)]}


def shared_trigger_signal(signal_id: str, setup_point_type: str) -> dict[str, object]:
    signal = signal_document("triggered")
    signal["signal_id"] = signal_id
    signal["point_type"] = setup_point_type
    signal["point_id"] = f"setup:{setup_point_type}"
    signal["setup_5m"] = {
        **signal["setup_5m"],
        "point_id": f"setup:{setup_point_type}",
        "point_type": setup_point_type,
        "center_ordinal": 1,
        "invalidation_price": "9.80",
    }
    signal["trigger_1m"] = {
        **signal["trigger_1m"],
        "point_id": "trigger:shared-1m-1buy",
        "point_type": "1buy",
        "confirmed_at": "2026-07-20T10:01:00+08:00",
    }
    return signal


class RecordingNotifier:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.messages: list[tuple[str, list[str]]] = []
        self.results = list(results or [True])

    def send(self, title: str, lines: list[str]) -> bool:
        self.messages.append((title, lines))
        return self.results.pop(0) if self.results else True


class RichRecordingNotifier(RecordingNotifier):
    def __init__(self) -> None:
        super().__init__()
        self.rich_messages: list[tuple[str, list[str], dict[str, object]]] = []

    def send_rich(
        self,
        title: str,
        lines: list[str],
        context: dict[str, object],
    ) -> bool:
        self.rich_messages.append((title, lines, context))
        return True


def test_only_material_lifecycle_transitions_notify(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
        clock=lambda: datetime(
            2026,
            7,
            20,
            10,
            1,
            30,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))
    dispatcher.dispatch_changes(snapshot("triggered"), snapshot("triggered"))

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert title == ("买卖通知｜新买点·待人工确认｜候选｜SZ.000001｜5分钟三类买点")
    rendered = "\n".join(lines)
    assert "时间：操作确认（末端结构仍会随新K更新） 2026-07-20 10:00:00" in rendered
    assert "监听发现：2026-07-20 10:01:30" in rendered
    assert "最近已完成K线收盘价：10.25" in rendered
    assert "5分钟三类买点（递归层级：L0）" in rendered
    assert "1分钟段差：一类买点（递归层级：L0）" in rendered
    assert "防守价：9.80（跌破买入结构失效）" in rendered
    assert "30分钟向上（有利）" in rendered
    assert "5分钟三类买点" in rendered
    assert "1分钟段差：一类买点" in rendered
    assert "在其他交易软件手工确认并分批买入" in rendered
    assert "本系统不会自动下单" in rendered
    assert (
        "风险参考：结构模型比例上限 8.5%"
        "（按当前价至5分钟防守位；精确测算 8.54%；仅作结构模型比较）"
    ) in rendered
    assert "结构锚点：10（+2.50%）" in rendered
    assert "状态：可人工复核执行" in rendered
    assert not any(
        term in f"{title}\n{rendered}"
        for term in ("账户", "现金", "持仓", "仓位", "虚拟", "组合热度")
    )
    assert "进度：旧版等待态→5分钟操作确认" in rendered
    assert "计划风险倍数" not in rendered
    assert "结构层级" not in rendered


def test_geometric_candidate_never_sends_a_trade_notification(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "candidate-delivery.json",
    )

    dispatcher.dispatch_changes(snapshot(None), snapshot("formed"))

    assert sender.messages == []
    assert dispatcher.health_snapshot()["event_audit_record_count"] == 0


def test_execution_profile_hard_block_still_reports_confirmed_structure_as_observation(
    tmp_path: Path,
) -> None:
    sender = RichRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    blocked = signal_document("triggered")
    blocked["execution_profile"] = {
        "structure_signal_confirmed": True,
        "execution_trigger_confirmed": True,
        "one_minute_required_for_trade_signal": False,
        "one_minute_required_for_precise_execution": True,
        # The hard gate remains authoritative if a migrated producer carries
        # a contradictory recommendation.
        "recommendation": "READY",
        "hard_blocked": True,
        "hard_block_reason_codes": ["structure_invalidated"],
        "advisory_reason_codes": [],
        "context_grade": "C",
        "context_grade_label": "C级（逆风观察）",
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [blocked]})

    assert sender.messages == []
    assert len(sender.rich_messages) == 1
    _title, lines, context = sender.rich_messages[0]
    rendered = "\n".join(lines)
    assert "0%" in rendered
    assert "本条5分钟买点结构已失效" in rendered
    assert "存在结构或数据硬阻断" not in rendered
    assert context["delivery_priority"] == 4
    assert "待人工复核" in _title
    assert dispatcher.health_snapshot()["last_suppressed_reason"] is None


def test_a_share_lunch_break_does_not_expire_a_fresh_confirmed_point(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    observed_at = datetime(
        2026,
        7,
        20,
        13,
        1,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "lunch-delivery.json",
        clock=lambda: observed_at,
    )
    signal = signal_document("triggered")
    signal["observed_at"] = observed_at.isoformat()
    signal["setup_5m"] = {
        **signal["setup_5m"],
        "confirmed_at": "2026-07-20T11:30:00+08:00",
        "available_at": "2026-07-20T11:30:00+08:00",
    }
    signal["trigger_1m"] = {
        **signal["trigger_1m"],
        "confirmed_at": "2026-07-20T13:01:00+08:00",
        "available_at": "2026-07-20T13:01:00+08:00",
    }
    signal["entry_execution_boundary"] = {
        "confirmation_bar_closed_at": "2026-07-20T13:01:00+08:00",
        "entry_valid_until": "2026-07-20T13:02:00+08:00",
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [signal]})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert "新买点·待人工确认" in title
    assert "结构模型比例上限" in "\n".join(lines)
    assert "买点已超过10分钟" not in "\n".join(lines)
    assert dispatcher.health_snapshot()["suppressed_count"] == 0


def test_context_caution_still_notifies_for_manual_review(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    caution = signal_document("triggered")
    caution["execution_profile"] = {
        "structure_signal_confirmed": True,
        "execution_trigger_confirmed": True,
        "one_minute_required_for_trade_signal": False,
        "one_minute_required_for_precise_execution": True,
        "recommendation": "CAUTION",
        "hard_blocked": False,
        "hard_block_reason_codes": [],
        "advisory_reason_codes": ["CONTEXT_GRADE_C"],
        "context_grade": "C",
        "context_grade_label": "C级（逆风观察）",
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }
    caution["context_d"] = {
        "direction": "down",
        "disposition": "hostile",
        "same_period_technical_evidence": {
            "ma5": 9.0,
            "ma10": 10.0,
            "ma5_vs_ma10": "ma5_below_ma10",
            "fractal_type": "top",
            "fractal_state": "confirmed",
        },
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [caution]})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    rendered = "\n".join(lines)
    assert title.startswith("买卖通知｜买点观察·待人工复核")
    assert "需手工复核" in rendered
    assert "环境：C级（逆风观察）" in rendered
    assert "日线 MA5 9.0｜MA10 10.0" in rendered


def test_final_position_block_overrides_caution_action_copy() -> None:
    blocked = signal_document("triggered")
    blocked["execution_profile"] = {
        "structure_signal_confirmed": True,
        "execution_trigger_confirmed": True,
        "one_minute_required_for_trade_signal": False,
        "one_minute_required_for_precise_execution": True,
        "recommendation": "CAUTION",
        "hard_blocked": False,
        "hard_block_reason_codes": [],
        "advisory_reason_codes": ["CONTEXT_GRADE_C"],
        "context_grade": "C",
        "context_grade_label": "C级（逆风观察）",
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }
    blocked["notification_position_recommendation"] = {
        "side": "buy",
        "status": "BLOCKED",
        "basis": "NO_TRADE",
        "recommended_percent": "0",
        "reason_codes": ["three_buy_lacks_tick_clearance"],
    }

    _title, lines = format_notification(
        blocked,
        old_stage="armed",
        new_stage="triggered",
    )

    assert lines[-1] == (
        "操作：本条买入不纳入操作计划；三买离开中枢的价格空间不足一个最小价位"
    )
    assert "谨慎" not in lines[-1]


def test_notification_does_not_conflate_recursive_confirmation_and_availability() -> (
    None
):
    signal = signal_document()
    setup = signal["setup_5m"]
    assert isinstance(setup, dict)
    setup["confirmed_at"] = "2026-07-20T10:01:00+08:00"
    setup["available_at"] = "2026-07-20T10:04:00+08:00"
    setup["recursive_level"] = 1
    signal["observed_at"] = "2026-07-20T10:04:25+08:00"

    _title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    rendered = "\n".join(lines)
    assert "操作确认（末端结构仍会随新K更新） 2026-07-20 10:01:00" in rendered
    assert "信号可用 2026-07-20 10:04:00" in rendered
    assert "监听发现：2026-07-20 10:04:25" in rendered
    assert "5分钟三类买点（递归层级：L1）" in rendered


def test_notification_labels_five_minute_price_fallback_honestly() -> None:
    signal = signal_document()
    signal["current_price_source"] = "latest_completed_5m_close"

    _title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    rendered = "\n".join(lines)
    assert "最近5分钟收盘价：10.25" in rendered
    assert "最近1分钟收盘价：10.25" not in rendered


def test_every_attempted_realtime_signal_is_kept_for_human_review(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier(results=[False])
    inbox = RealtimeReviewInbox(tmp_path / "review-inbox.json")
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
        review_inbox=inbox,
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    review = inbox.snapshot()
    assert review["event_count"] == 1
    event = review["events"][0]
    assert event["code"] == "SZ.000001"
    assert event["point_type"] == "3buy"
    assert event["new_stage"] == "triggered"
    assert event["delivery_status"] == "failed"
    assert event["delivery_reason"] == "NOTIFIER_RETURNED_FALSE"
    assert event["review_required"] is True
    assert event["automated_action_authorized"] is False


def test_local_human_review_inbox_does_not_depend_on_external_transport(
    tmp_path: Path,
) -> None:
    inbox = RealtimeReviewInbox(tmp_path / "review-inbox.json")
    dispatcher = SignalNotificationDispatcher(
        None,
        state_path=tmp_path / "delivery-state.json",
        review_inbox=inbox,
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    event = inbox.snapshot()["events"][0]
    assert event["delivery_status"] == "failed"
    assert event["delivery_reason"] == "NOTIFIER_RETURNED_FALSE"
    health = dispatcher.health_snapshot()
    assert health["configured"] is False
    assert health["review_inbox_configured"] is True
    assert health["status"] == "unavailable"
    assert health["reason_code"] == ("EXTERNAL_NOTIFICATION_TRANSPORT_NOT_CONFIGURED")


def test_dispatcher_passes_stable_a_share_chart_context(tmp_path: Path) -> None:
    sender = RichRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    assert sender.messages == []
    assert len(sender.rich_messages) == 1
    context = sender.rich_messages[0][2]
    chart = context["charts"][0]
    assert chart["market"] == "a"
    assert chart["code"] == "SZ.000001"
    assert str(chart["artifact_key"]).startswith("sha256:")
    assert chart["point_type"] == "3buy"
    assert chart["signal_time"] == "2026-07-20T10:00:00+08:00"
    assert chart["evidence_id"] == "setup:stable-5m-3buy"
    assert chart["recursive_level"] == 0
    assert chart["anchor_time"] == "2026-07-20T09:55:00+08:00"
    assert chart["frequency"] == "5m"
    assert chart["evidence_required"] is True
    assert context["delivery_priority"] == 3
    assert sender.rich_messages[0][2]["require_evidence_match"] is True


def _without_one_minute_segment(
    signal: dict[str, object],
) -> dict[str, object]:
    previous = dict(signal)
    previous["trigger_1m"] = None
    previous.pop("segment_difference_1m", None)
    previous.pop("entry_execution_boundary", None)
    return previous


def test_stage_stable_new_one_minute_segment_sends_enrichment_notification(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "segment-enrichment.json",
    )
    current = signal_document("triggered")

    dispatcher.dispatch_changes(
        {"signals": [_without_one_minute_segment(current)]},
        {"signals": [current]},
    )

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    rendered = "\n".join(lines)
    assert "1分钟段差新出现" in title
    assert "5分钟三类买点＋1分钟一类买点" in title
    assert "进度：5分钟操作确认→1分钟段差补充" in rendered
    assert "1分钟段差确认 2026-07-20 10:01:00" in rendered
    assert "监听发现：2026-07-20 10:01:30（延迟 30秒）" in rendered
    assert "1分钟区间套已完成且定位窗口有效，现已升级为精确执行候选" in rendered
    assert dispatcher.health_snapshot()["delivered_event_count"] == 1


def test_newer_one_minute_segment_rearms_same_five_minute_notification(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    now = [TEST_NOW]
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "segment-rearmed.json",
        clock=lambda: now[0],
    )
    previous = signal_document("triggered")
    dispatcher.dispatch_changes(
        {"signals": [_without_one_minute_segment(previous)]},
        {"signals": [previous]},
    )
    current = signal_document("triggered")
    current["observed_at"] = "2026-07-20T10:02:30+08:00"
    current["trigger_1m"] = {
        **current["trigger_1m"],
        "point_id": "trigger:rearmed-1m-1buy",
        "anchor_at": "2026-07-20T10:00:00+08:00",
        "available_at": "2026-07-20T10:02:00+08:00",
        "confirmed_at": "2026-07-20T10:02:00+08:00",
    }
    current["entry_execution_boundary"] = {
        "confirmation_bar_closed_at": "2026-07-20T10:02:00+08:00",
        "entry_valid_until": "2026-07-20T10:03:00+08:00",
    }
    now[0] = datetime(
        2026,
        7,
        20,
        10,
        2,
        30,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )

    dispatcher.dispatch_changes(
        {"signals": [previous]},
        {"signals": [current]},
    )

    assert len(sender.messages) == 2
    assert "1分钟段差新出现" in sender.messages[1][0]
    assert "1分钟段差确认 2026-07-20 10:02:00" in "\n".join(
        sender.messages[1][1]
    )
    assert dispatcher.health_snapshot()["delivered_event_count"] == 2


def test_segment_enrichment_rich_notification_uses_one_minute_evidence(
    tmp_path: Path,
) -> None:
    sender = RichRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "segment-enrichment-rich.json",
    )
    current = signal_document("triggered")

    dispatcher.dispatch_changes(
        {"signals": [_without_one_minute_segment(current)]},
        {"signals": [current]},
    )

    assert sender.messages == []
    assert len(sender.rich_messages) == 1
    _title, _lines, context = sender.rich_messages[0]
    [chart] = context["charts"]
    assert chart["frequency"] == "1m"
    assert chart["point_type"] == "1buy"
    assert chart["evidence_id"] == "trigger:stable-1m-1buy"
    assert chart["signal_time"] == "2026-07-20T10:01:00+08:00"
    assert chart["evidence_required"] is True


def test_segment_enrichment_dedupe_survives_refresh_restart_and_rebuilt_ids(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "segment-enrichment-dedupe.json"
    first_sender = RecordingNotifier()
    current = signal_document("triggered")
    first = SignalNotificationDispatcher(first_sender, state_path=state_path)
    first.dispatch_changes(
        {"signals": [_without_one_minute_segment(current)]},
        {"signals": [current]},
    )
    first.dispatch_changes({"signals": [current]}, {"signals": [current]})

    rebuilt = signal_document("triggered")
    rebuilt["signal_id"] = "signal:rebuilt"
    rebuilt["point_id"] = "setup:rebuilt"
    rebuilt["setup_5m"] = {
        **rebuilt["setup_5m"],
        "point_id": "setup:rebuilt",
    }
    rebuilt["trigger_1m"] = {
        **rebuilt["trigger_1m"],
        "point_id": "trigger:rebuilt",
    }
    second_sender = RecordingNotifier()
    SignalNotificationDispatcher(
        second_sender,
        state_path=state_path,
    ).dispatch_changes(
        {"signals": [_without_one_minute_segment(rebuilt)]},
        {"signals": [rebuilt]},
    )

    assert len(first_sender.messages) == 1
    assert second_sender.messages == []


def test_stale_segment_enrichment_is_suppressed(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "stale-segment-enrichment.json",
    )
    current = signal_document("triggered")
    current["observed_at"] = "2026-07-20T10:11:01+08:00"

    dispatcher.dispatch_changes(
        {"signals": [_without_one_minute_segment(current)]},
        {"signals": [current]},
    )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ONE_MINUTE_SEGMENT_STALE"
    )


def test_segment_formed_inside_five_minute_structure_is_fresh_at_confluence(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "formation-segment-enrichment.json",
    )
    current = signal_document("triggered")
    current["trigger_1m"] = {
        **current["trigger_1m"],
        "anchor_at": "2026-07-20T09:40:00+08:00",
        "confirmed_at": "2026-07-20T09:50:00+08:00",
        "available_at": "2026-07-20T09:50:00+08:00",
        "divergence_kind": "trend",
    }

    dispatcher.dispatch_changes(
        {"signals": [_without_one_minute_segment(current)]},
        {"signals": [current]},
    )

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    rendered = "\n".join(lines)
    assert "1分钟一类买点（趋势背驰）" in title
    assert "1分钟段差：一类买点（趋势背驰）" in rendered
    assert "监听发现：2026-07-20 10:01:30（延迟 1分30秒）" in rendered
    assert dispatcher.health_snapshot()["last_suppressed_reason"] is None
    persisted = json.loads(
        (tmp_path / "formation-segment-enrichment.json").read_text(encoding="utf-8")
    )
    audit = persisted["event_audit"][-1]
    assert audit["trigger_divergence_kind"] == "trend"
    assert audit["notification_evidence_at"] == "2026-07-20T10:00:00+08:00"


def test_fresh_segment_recomputes_position_age_from_confluence(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    inbox = RealtimeReviewInbox(tmp_path / "segment-position-inbox.json")
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "segment-position-state.json",
        review_inbox=inbox,
    )
    current = signal_document("triggered")
    current["setup_5m"] = {
        **current["setup_5m"],
        "anchor_at": "2026-07-20T09:35:00+08:00",
        "confirmed_at": "2026-07-20T09:40:00+08:00",
        "available_at": "2026-07-20T09:40:00+08:00",
    }

    dispatcher.dispatch_changes(
        {"signals": [_without_one_minute_segment(current)]},
        {"signals": [current]},
    )

    assert len(sender.messages) == 1
    [event] = inbox.snapshot()["events"]
    assert event["new_stage"] == "segment_enriched"
    assert event["position_recommendation"]["status"] == "RECOMMENDED"
    assert "BUY_SIGNAL_DISCOVERY_TOO_LATE_NO_CHASE" not in event[
        "position_recommendation"
    ]["reason_codes"]


def test_five_minute_transition_takes_precedence_over_simultaneous_segment(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "transition-with-segment.json",
    )
    current = signal_document("triggered")
    previous = _without_one_minute_segment(signal_document("armed"))

    dispatcher.dispatch_changes(
        {"signals": [previous]},
        {"signals": [current]},
    )
    dispatcher.dispatch_changes({"signals": [current]}, {"signals": [current]})

    assert len(sender.messages) == 1
    assert "1分钟段差新出现" not in sender.messages[0][0]
    assert "5分钟三类买点" in sender.messages[0][0]


def test_stale_before_snapshot_cannot_repeat_segment_carried_by_trigger(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "trigger-carried-segment.json"
    previous = _without_one_minute_segment(signal_document("armed"))
    current = signal_document("triggered")
    first_sender = RecordingNotifier()
    first_dispatcher = SignalNotificationDispatcher(
        first_sender,
        state_path=state_path,
    )

    first_dispatcher.dispatch_changes(
        {"signals": [previous]},
        {"signals": [current]},
    )

    assert len(first_sender.messages) == 1
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(persisted["delivered_event_ids"]) == 1
    assert len(persisted["delivered_segment_evidence_ids"]) == 1

    restarted_sender = RecordingNotifier()
    restarted_dispatcher = SignalNotificationDispatcher(
        restarted_sender,
        state_path=state_path,
    )
    # The service can retry a partial publish with the same stale before-image.
    restarted_dispatcher.dispatch_changes(
        {"signals": [previous]},
        {"signals": [current]},
    )

    assert restarted_sender.messages == []
    assert restarted_dispatcher.health_snapshot()["delivered_event_count"] == 1


@pytest.mark.parametrize(
    ("previous_stage", "current_stage"),
    (("triggered", "executable"), ("executable", "active")),
)
def test_delivered_five_minute_stage_change_cannot_swallow_new_segment(
    tmp_path: Path,
    previous_stage: str,
    current_stage: str,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / f"segment-during-{current_stage}.json",
    )
    triggered_without_segment = _without_one_minute_segment(
        signal_document("triggered")
    )
    dispatcher.dispatch_changes(
        {
            "signals": [
                _without_one_minute_segment(signal_document("armed"))
            ]
        },
        {"signals": [triggered_without_segment]},
    )
    previous = _without_one_minute_segment(signal_document(previous_stage))
    current = signal_document(current_stage)

    dispatcher.dispatch_changes(
        {"signals": [previous]},
        {"signals": [current]},
    )

    assert len(sender.messages) == 2
    assert "1分钟段差新出现" in sender.messages[1][0]
    persisted = json.loads(
        (tmp_path / f"segment-during-{current_stage}.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["event_audit"][-1]["new_stage"] == "segment_enriched"


def test_pending_five_minute_retry_absorbs_simultaneous_segment(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier([False, True])
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "pending-trigger-with-segment.json",
    )
    triggered_without_segment = _without_one_minute_segment(
        signal_document("triggered")
    )
    dispatcher.dispatch_changes(
        {
            "signals": [
                _without_one_minute_segment(signal_document("armed"))
            ]
        },
        {"signals": [triggered_without_segment]},
    )

    dispatcher.dispatch_changes(
        {"signals": [triggered_without_segment]},
        {"signals": [signal_document("executable")]},
    )

    assert len(sender.messages) == 2
    assert "1分钟段差新出现" not in sender.messages[1][0]
    assert "1分钟段差：一类买点" in "\n".join(sender.messages[1][1])
    assert dispatcher.health_snapshot()["delivered_event_count"] == 1


def test_same_one_minute_segment_does_not_coalesce_distinct_five_minute_signals(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    one_buy = shared_trigger_signal("signal:five-minute-one-buy", "1buy")
    two_buy = shared_trigger_signal("signal:five-minute-two-buy", "2buy")
    previous = {
        "signals": [
            {**one_buy, "lifecycle_stage": "armed"},
            {**two_buy, "lifecycle_stage": "armed"},
        ]
    }

    dispatcher.dispatch_changes(previous, {"signals": [one_buy, two_buy]})

    assert len(sender.messages) == 2
    assert {
        message[0].split("｜")[-1].split("（")[0] for message in sender.messages
    } == {
        "5分钟一类买点",
        "5分钟二类买点",
    }
    assert dispatcher.health_snapshot()["success_count"] == 2


def test_semantic_trigger_dedupe_survives_restart_and_changed_setup_id(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "delivered.json"
    first_sender = RecordingNotifier()
    first = shared_trigger_signal("signal:first-lane", "1buy")
    SignalNotificationDispatcher(
        first_sender,
        state_path=state_path,
    ).dispatch_changes(
        {"signals": [{**first, "lifecycle_stage": "armed"}]},
        {"signals": [first]},
    )
    second_sender = RecordingNotifier()
    second = shared_trigger_signal("signal:second-lane", "1buy")
    second["point_id"] = "setup:rebuilt-same-5m-event"
    second["setup_5m"] = {
        **second["setup_5m"],
        "point_id": "setup:rebuilt-same-5m-event",
    }
    second["trigger_1m"] = {
        **second["trigger_1m"],
        # A price-basis rebuild may replace this internal ID while leaving the
        # completed trigger bar and every visible notification fact unchanged.
        "point_id": "trigger:rebuilt-same-causal-event",
    }

    SignalNotificationDispatcher(
        second_sender,
        state_path=state_path,
    ).dispatch_changes(
        {"signals": [{**second, "lifecycle_stage": "armed"}]},
        {"signals": [second]},
    )

    assert len(first_sender.messages) == 1
    assert second_sender.messages == []


def test_distinct_one_minute_triggers_are_not_coalesced(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    first = shared_trigger_signal("signal:first-trigger", "1buy")
    second = shared_trigger_signal("signal:second-trigger", "2buy")
    second["trigger_1m"] = {
        **second["trigger_1m"],
        "point_id": "trigger:distinct-1m-1buy",
        "available_at": "2026-07-20T10:02:00+08:00",
        "confirmed_at": "2026-07-20T10:02:00+08:00",
    }
    second["observed_at"] = "2026-07-20T10:02:30+08:00"
    second["entry_execution_boundary"] = {
        "confirmation_bar_closed_at": "2026-07-20T10:02:00+08:00",
        "entry_valid_until": "2026-07-20T10:03:00+08:00",
    }

    dispatcher.dispatch_changes(
        {
            "signals": [
                {**first, "lifecycle_stage": "armed"},
                {**second, "lifecycle_stage": "armed"},
            ]
        },
        {"signals": [first, second]},
    )

    assert len(sender.messages) == 2


def test_same_close_time_with_distinct_l0_segment_anchor_is_not_coalesced(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "recursive-occurrences.json",
    )
    first = shared_trigger_signal("signal:level-zero", "1buy")
    second = shared_trigger_signal("signal:level-one", "2buy")
    second["trigger_1m"] = {
        **second["trigger_1m"],
        "point_id": "trigger:rebuilt-distinct-anchor",
        "recursive_level": 0,
        "anchor_at": "2026-07-20T09:55:00+08:00",
    }

    dispatcher.dispatch_changes(
        {
            "signals": [
                {**first, "lifecycle_stage": "armed"},
                {**second, "lifecycle_stage": "armed"},
            ]
        },
        {"signals": [first, second]},
    )

    assert len(sender.messages) == 2


def test_newly_discovered_fresh_trigger_is_sent_immediately(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )

    dispatcher.dispatch_changes(snapshot(None), snapshot("triggered"))

    assert len(sender.messages) == 1
    assert sender.messages[0][0].startswith("买卖通知｜新买点·待人工确认｜")
    assert dispatcher.health_snapshot()["success_count"] == 1


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ({"warmup": {"converged": False}}, "WARMUP_NOT_CONVERGED"),
        (
            {"physical_timeframe_recursive": False},
            "PHYSICAL_TIMEFRAME_AUTHORITY_MISSING",
        ),
    ),
)
def test_buy_transition_fails_closed_when_decision_gate_is_not_proven(
    tmp_path: Path,
    mutation: dict[str, object],
    reason: str,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / f"{reason}.json",
    )
    current = signal_document("triggered")
    current.update(mutation)

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [current]})

    assert sender.messages == []
    health = dispatcher.health_snapshot()
    assert health["suppressed_count"] == 1
    assert health["last_suppressed_reason"] == reason


def test_context_warmup_divergence_does_not_suppress_five_minute_notification(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "context-warmup-advisory.json",
    )
    current = signal_document("triggered")
    current["warmup"] = {
        "converged": False,
        "by_frequency": [
            {"frequency": "d", "converged": True},
            {"frequency": "30m", "converged": False},
            {"frequency": "5m", "converged": True},
            {"frequency": "1m", "converged": True},
        ],
        "reason_codes": [
            "D:WARMUP_TAIL_STABLE",
            "30M:WARMUP_TAIL_DIVERGED",
            "5M:WARMUP_TAIL_STABLE",
            "1M:WARMUP_TAIL_STABLE",
        ],
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [current]})

    assert len(sender.messages) == 1
    assert dispatcher.health_snapshot()["suppressed_count"] == 0


def test_five_minute_warmup_row_is_authoritative_for_notification(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "five-minute-warmup-block.json",
    )
    current = signal_document("triggered")
    current["warmup"] = {
        "converged": True,
        "by_frequency": [
            {"frequency": "d", "converged": True},
            {"frequency": "30m", "converged": True},
            {"frequency": "5m", "converged": False},
            {"frequency": "1m", "converged": True},
        ],
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [current]})

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "WARMUP_NOT_CONVERGED"
    )


@pytest.mark.parametrize(
    "mutation",
    (
        {"entry_allowed": False},
        {
            "entry_allowed": False,
            "higher_timeframe_risk": {
                "market_gate": "GREEN",
                "sector_gate": "AMBER",
                "symbol_gate": "GREEN",
            },
        },
        {"entry_allowed": False, "sector_triggered": False},
    ),
)
def test_confirmed_buy_point_notifies_even_when_formal_entry_is_blocked(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    sender = RecordingNotifier()
    current = signal_document("triggered")
    current.update(mutation)

    SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "structural-buy.json",
    ).dispatch_changes(snapshot("armed"), {"signals": [current]})

    assert len(sender.messages) == 1
    rendered = "\n".join(sender.messages[0][1])
    assert sender.messages[0][0].startswith("买卖通知｜买点观察·待人工复核")
    assert "需手工复核" in rendered
    assert "其他交易软件手工决定" in rendered


def test_delayed_armed_transition_is_suppressed_as_stale(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    stale = signal_document("triggered")
    stale["observed_at"] = "2026-07-20T10:11:00+08:00"

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [stale]})

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "FIVE_MINUTE_SIGNAL_STALE"
    )


def test_stale_trigger_first_seen_without_prior_tracking_is_suppressed(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "stale-first-seen.json",
    )
    stale = signal_document("triggered")
    stale["observed_at"] = "2026-07-20T10:11:00+08:00"

    dispatcher.dispatch_changes(snapshot(None), {"signals": [stale]})

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "FIVE_MINUTE_SIGNAL_STALE"
    )


def test_just_late_detection_is_delivered_as_zero_percent_review_within_grace(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    now = datetime(
        2026,
        7,
        20,
        10,
        10,
        51,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "late-within-grace.json",
        clock=lambda: now,
    )
    delayed = signal_document("triggered")
    # 结构快照在严格10分钟边界完成，监听调度在51秒后才拿到它。
    delayed["observed_at"] = "2026-07-20T10:10:00+08:00"

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [delayed]})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    rendered = "\n".join(lines)
    assert title.startswith("买卖通知｜延迟买点复核｜")
    assert (
        "风险参考：本条买入不纳入操作计划（买点已超过10分钟新鲜窗口，等待新的5分钟结构）"
        in rendered
    )
    assert "监听发现：2026-07-20 10:10:51（延迟 10分51秒）" in rendered
    assert "不追价，等待新的5分钟结构" in rendered


def test_detection_after_delivery_grace_is_expired_without_quote_or_send(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    quote_calls: list[str] = []
    now = datetime(
        2026,
        7,
        20,
        10,
        12,
        1,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "late-after-grace.json",
        clock=lambda: now,
        review_inbox=RealtimeReviewInbox(tmp_path / "late-after-grace-review.json"),
        quote_provider=lambda code: quote_calls.append(code) or {"last": 10.1},
    )
    delayed = signal_document("triggered")
    delayed["observed_at"] = "2026-07-20T10:10:00+08:00"

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [delayed]})

    assert sender.messages == []
    assert quote_calls == []
    assert (
        dispatcher.health_snapshot()["last_suppressed_reason"]
        == "NOTIFICATION_DELIVERY_EXPIRED"
    )


@pytest.mark.parametrize("point_type", ("3buy", "3sell"))
def test_boundary_touch_third_class_one_minute_point_is_not_a_continuation_trigger(
    tmp_path: Path,
    point_type: str,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / f"{point_type}.json",
    )
    signal = signal_document("triggered")
    side = "buy" if point_type == "3buy" else "sell"
    signal.update({"side": side, "point_type": point_type})
    signal["setup_5m"] = {
        **signal["setup_5m"],
        "point_type": point_type,
        "side": side,
    }
    signal["trigger_1m"] = {
        **signal["trigger_1m"],
        "point_type": point_type,
        "side": side,
        "variant": "boundary_touch",
        "center_id": "trigger-center",
        "center_ordinal": 1,
        "anchor_price": "10.00",
        "center_zd": "10.00",
        "center_zg": "10.00",
    }

    dispatcher.dispatch_changes(
        {"signals": [{**signal, "lifecycle_stage": "armed"}]},
        {"signals": [signal]},
    )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ONE_MINUTE_SEGMENT_EVIDENCE_INVALID"
    )


def test_standard_third_class_one_minute_continuation_trigger_notifies(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "third-continuation.json",
    )
    signal = signal_document("triggered")
    signal["trigger_1m"] = {
        **signal["trigger_1m"],
        "point_type": "3buy",
        "variant": "standard",
        "center_id": "trigger-center",
        "center_ordinal": 1,
        "anchor_price": "10.00",
        "center_zd": "9.90",
        "center_zg": "9.98",
    }

    dispatcher.dispatch_changes(
        {"signals": [{**signal, "lifecycle_stage": "armed"}]},
        {"signals": [signal]},
    )

    assert len(sender.messages) == 1
    assert "1分钟段差：三类买点" in "\n".join(sender.messages[0][1])


def test_expired_one_minute_boundary_does_not_suppress_five_minute_signal(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    expired = signal_document("triggered")
    expired["observed_at"] = "2026-07-20T10:01:30+08:00"
    expired["entry_execution_boundary"] = {
        "confirmation_bar_closed_at": "2026-07-20T10:01:00+08:00",
        "entry_valid_until": "2026-07-20T10:01:15+08:00",
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [expired]})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert "5分钟三类买点" in title
    assert "段差已定位" not in title
    assert (
        "1分钟段差：一类买点（递归层级：L0）证据已确认；"
        "买入定位窗口已过，但证据没有消失"
    ) in "\n".join(lines)


def test_notification_expires_boundary_crossed_after_snapshot_observation() -> None:
    signal = signal_document("triggered")

    _title, lines = format_notification(
        signal,
        old_stage="triggered",
        new_stage="segment_enriched",
        detected_at=datetime.fromisoformat("2026-07-20T10:02:30+08:00"),
    )

    rendered = "\n".join(lines)
    assert "定位窗口已过" in rendered
    assert "精确执行候选已解锁" not in rendered
    assert "结构模型比例上限" not in rendered
    assert "本条买入不纳入操作计划" in rendered


def test_sell_transition_notifies_without_an_actual_holding_exit_decision(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    sell = signal_document("triggered")
    sell.update(
        {
            "side": "sell",
            "point_type": "3sell",
            "entry_allowed": False,
            "exit_allowed": False,
            "setup_5m": {
                **sell["setup_5m"],
                "point_type": "3sell",
                "side": "sell",
            },
            "trigger_1m": {
                **sell["trigger_1m"],
                "point_type": "1sell",
                "side": "sell",
            },
        }
    )
    previous_sell = {**sell, "lifecycle_stage": "armed"}

    dispatcher.dispatch_changes(
        {"signals": [previous_sell]},
        {"signals": [sell]},
    )

    assert len(sender.messages) == 1
    rendered = "\n".join(sender.messages[0][1])
    assert "结构卖出提醒" in rendered
    assert "请核对卖点级别与结构仍然有效" in rendered


def test_fresh_sell_alert_is_dispatched_before_lexically_earlier_buy(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "risk-priority.json",
    )
    buy = signal_document("triggered")
    buy["signal_id"] = "signal:buy-first-code"
    sell = signal_document("triggered")
    sell_point_id = "setup:sell-later-code"
    sell.update(
        {
            "signal_id": "signal:sell-later-code",
            "point_id": sell_point_id,
            "code": "SZ.999999",
            "side": "sell",
            "point_type": "1sell",
            "selection_sources": ["HOLDING_MONITOR"],
            "entry_allowed": False,
            "exit_allowed": True,
            "setup_5m": {
                **sell["setup_5m"],
                "point_id": sell_point_id,
                "point_type": "1sell",
                "side": "sell",
            },
            "trigger_1m": {
                **sell["trigger_1m"],
                "point_type": "1sell",
                "side": "sell",
            },
        }
    )
    previous = {
        "signals": [
            {**buy, "lifecycle_stage": "armed"},
            {**sell, "lifecycle_stage": "armed"},
        ]
    }

    dispatcher.dispatch_changes(previous, {"signals": [buy, sell]})

    assert len(sender.messages) == 2
    assert "卖点" in sender.messages[0][0]
    assert "买点" in sender.messages[1][0]


def test_same_trigger_stage_upgrade_does_not_send_twice(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    triggered = signal_document("triggered")
    executable = signal_document("executable")

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [triggered]})
    dispatcher.dispatch_changes({"signals": [triggered]}, {"signals": [executable]})

    assert len(sender.messages) == 1
    assert dispatcher.health_snapshot()["delivered_event_count"] == 1


def test_same_trigger_with_changed_defense_price_is_one_notification(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    first = shared_trigger_signal("signal:first", "1buy")
    second = shared_trigger_signal("signal:second", "1buy")
    first["setup_5m"] = {**first["setup_5m"], "invalidation_price": "9.80"}
    second["setup_5m"] = {**second["setup_5m"], "invalidation_price": "9.60"}

    dispatcher.dispatch_changes(
        {
            "signals": [
                {**first, "lifecycle_stage": "armed"},
                {**second, "lifecycle_stage": "armed"},
            ]
        },
        {"signals": [first, second]},
    )

    assert len(sender.messages) == 1
    assert "防守价：9.80、9.60（跌破买入结构失效）" in "\n".join(sender.messages[0][1])


def test_authoritative_refresh_retracts_a_disappeared_trigger(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))
    dispatcher.dispatch_changes(
        {"signals": [signal_document("triggered")]},
        {
            "signals": [],
            "notification_authoritative_codes": ["SZ.000001"],
        },
    )

    assert len(sender.messages) == 2
    assert "结构已失效" in sender.messages[1][0]
    assert "取消该结构计划" in "\n".join(sender.messages[1][1])


def test_authoritative_refresh_retracts_a_disappeared_armed_setup(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )

    dispatcher.dispatch_changes(
        {"signals": [signal_document("armed")]},
        {
            "signals": [],
            "notification_authoritative_codes": ["SZ.000001"],
        },
    )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ORPHAN_INVALIDATION_WITHOUT_DELIVERED_TRIGGER"
    )


def test_partial_refresh_never_retracts_an_unrecomputed_symbol(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )

    dispatcher.dispatch_changes(
        {"signals": [signal_document("triggered")]},
        {"signals": [], "notification_authoritative_codes": ["SZ.000002"]},
    )

    assert sender.messages == []


def test_event_audit_persists_delivery_and_suppression_facts(tmp_path: Path) -> None:
    state_path = tmp_path / "delivered.json"
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(sender, state_path=state_path)
    blocked = signal_document("triggered")
    blocked["physical_timeframe_recursive"] = False

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [blocked]})
    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert [row["status"] for row in persisted["event_audit"]] == [
        "suppressed",
        "delivered",
    ]
    assert (
        persisted["event_audit"][0]["reason"] == "PHYSICAL_TIMEFRAME_AUTHORITY_MISSING"
    )
    assert persisted["event_audit"][1]["trigger_available_at"] == (
        "2026-07-20T10:01:00+08:00"
    )
    assert persisted["event_audit"][1]["trigger_anchor_at"] == (
        "2026-07-20T09:58:00+08:00"
    )
    assert persisted["event_audit"][1]["trigger_confirmed_at"] == (
        "2026-07-20T10:01:00+08:00"
    )
    assert persisted["event_audit"][1]["trigger_recursive_level"] == 0


def test_suppression_dedupe_survives_audit_window_and_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "delivered.json"
    dispatcher = SignalNotificationDispatcher(
        RecordingNotifier(),
        state_path=state_path,
    )
    reason = "ORPHAN_INVALIDATION_WITHOUT_DELIVERED_TRIGGER"

    for index in range(600):
        dispatcher._record_suppressed(  # noqa: SLF001 - persistence regression
            event_id=f"sha256:{index:064x}",
            old_stage="triggered",
            new_stage="invalidated",
            document={},
            reason=reason,
        )
    dispatcher._persist()  # noqa: SLF001

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(persisted["event_audit"]) == 500
    assert len(persisted["suppressed_fingerprints"]) == 600

    restored = SignalNotificationDispatcher(
        RecordingNotifier(),
        state_path=state_path,
    )
    restored._record_suppressed(  # noqa: SLF001
        event_id="sha256:" + "0" * 64,
        old_stage="triggered",
        new_stage="invalidated",
        document={},
        reason=reason,
    )

    health = restored.health_snapshot()
    assert health["suppressed_count"] == 600
    assert health["suppressed_fingerprint_count"] == 600
    assert health["event_audit_record_count"] == 500


@pytest.mark.parametrize(
    ("stage", "label"),
    (
        ("observed", "结构观察"),
        ("approaching", "即将确认"),
        ("formed", "5分钟几何候选待锁定确认"),
        ("armed", "旧版等待态"),
        ("triggered", "5分钟操作确认"),
        ("executable", "强提示待人工复核"),
        ("active", "结构持续跟踪"),
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


def test_manual_attention_source_is_explicitly_separated_from_candidate() -> None:
    signal = signal_document()
    signal["selection_sources"] = ["HOLDING_MONITOR"]

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert title == ("买卖通知｜新买点·待人工确认｜人工关注｜SZ.000001｜5分钟三类买点")
    assert "候选" not in title
    assert lines[-1] == (
        "操作：回抽确认后在其他交易软件手工确认并分批买入；本系统不会自动下单"
    )


def test_watchlist_signal_remains_candidate() -> None:
    signal = signal_document()
    signal["selection_sources"] = ["ACTIVE_WATCHLIST_MONITOR"]

    title, _lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert "｜候选｜" in title
    assert "｜人工关注｜" not in title


def test_sell_and_invalidation_advice_are_explicit() -> None:
    sell = signal_document()
    sell["side"] = "sell"
    sell["point_type"] = "3sell"
    sell["setup_5m"] = {
        "point_type": "3sell",
        "center_ordinal": 1,
        "invalidation_price": "10.80",
    }
    sell["trigger_1m"] = {}

    title, lines = format_notification(
        sell,
        old_stage="armed",
        new_stage="triggered",
    )
    assert title.endswith("5分钟三类卖点")
    assert "防守价：10.80（突破卖出结构失效）" in "\n".join(lines)
    assert lines[-1] == (
        "操作：结构卖出提醒已达到操作确认；请核对卖点级别与结构仍然有效，"
        "再在其他交易软件手工决定；本系统不会自动下单"
    )

    _title, invalidated_lines = format_notification(
        sell,
        old_stage="triggered",
        new_stage="invalidated",
    )
    assert invalidated_lines[-1] == "操作：取消该结构计划"


def test_unknown_sell_structure_relation_never_looks_like_an_exit_ratio() -> None:
    sell = signal_document()
    sell["side"] = "sell"
    sell["point_type"] = "2sell"
    sell["setup_5m"] = {
        **sell["setup_5m"],
        "point_type": "2sell",
        "side": "sell",
        "invalidation_price": "10.80",
    }
    recommendation = build_position_recommendation(
        side="sell",
        recommendation="CAUTION",
        risk_multiplier="1",
        context_risk_scale="1",
        entry_price="10",
        structural_stop="10.80",
        exit_action="none",
    ).document()
    sell["position_recommendation"] = recommendation
    sell["notification_position_recommendation"] = recommendation

    _title, lines = format_notification(
        sell,
        old_stage="armed",
        new_stage="triggered",
    )
    rendered = "\n".join(lines)

    assert "关系未确认前不生成退出比例" in rendered
    assert "参考上限 25%" not in rendered


def test_missing_defense_price_is_explicit_and_never_estimated() -> None:
    signal = signal_document()
    signal["setup_5m"] = {"point_type": "3buy", "invalidation_price": None}

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert title.startswith("买卖通知｜买点观察·待人工复核｜")
    assert "状态：仅观察，结构风险待核对" in lines[0]
    assert "防守价：待结构确认（跌破买入结构失效）" in "\n".join(lines)


def test_sub_tenth_percent_position_limit_is_not_rounded_to_zero() -> None:
    signal = signal_document()
    signal["notification_position_recommendation"] = {
        "side": "buy",
        "status": "RECOMMENDED",
        "recommended_percent": "0.04",
        "reason_codes": ["CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED"],
    }

    _title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert "风险参考：结构模型比例上限 0.04%" in "\n".join(lines)


def test_delivered_trigger_invalidation_and_active_close_notify(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered-lineage.json",
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))
    dispatcher.dispatch_changes(snapshot("triggered"), snapshot("invalidated"))
    dispatcher.dispatch_changes(snapshot("active"), snapshot("closed"))

    assert len(sender.messages) == 3


def test_same_terminal_occurrence_is_not_resent_after_signal_id_rebuild(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "stable-terminal-lineage.json",
    )
    first = shared_trigger_signal("signal:first", "3buy")
    first_armed = {**first, "lifecycle_stage": "armed"}
    first_invalidated = {**first, "lifecycle_stage": "invalidated"}
    rebuilt = shared_trigger_signal("signal:rebuilt", "3buy")
    rebuilt_invalidated = {**rebuilt, "lifecycle_stage": "invalidated"}

    dispatcher.dispatch_changes(
        {"signals": [first_armed]},
        {"signals": [first]},
    )
    dispatcher.dispatch_changes(
        {"signals": [first]},
        {"signals": [first_invalidated]},
    )
    dispatcher.dispatch_changes(
        {"signals": [rebuilt]},
        {"signals": [rebuilt_invalidated]},
    )

    assert len(sender.messages) == 2


def test_terminal_occurrence_ignores_non_trade_recursive_levels(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "terminal-recursive-levels.json",
    )
    first = shared_trigger_signal("signal:l0", "3buy")
    second = shared_trigger_signal("signal:l1", "3buy")
    second["trigger_1m"] = {
        **second["trigger_1m"],
        "point_id": "trigger:shared-1m-1buy:l1",
        "recursive_level": 1,
    }
    second["recursive_level"] = 2
    second["setup_5m"] = {
        **second["setup_5m"],
        "point_id": "setup:five-minute-l2",
        "recursive_level": 2,
    }
    second["point_id"] = "setup:five-minute-l2"

    for signal in (first, second):
        dispatcher.dispatch_changes(
            {"signals": [{**signal, "lifecycle_stage": "armed"}]},
            {"signals": [signal]},
        )
        dispatcher.dispatch_changes(
            {"signals": [signal]},
            {"signals": [{**signal, "lifecycle_stage": "invalidated"}]},
        )

    assert len(sender.messages) == 2
    assert all("递归层级：L0" in "\n".join(lines) for _title, lines in sender.messages)


def test_a_share_notification_enriches_current_price_from_realtime_quote(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    inbox = RealtimeReviewInbox(tmp_path / "quote-review.json")
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "quote-delivery.json",
        review_inbox=inbox,
        quote_provider=lambda code: {"last": 10.88, "code": code},
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    assert "当前价：10.88（获取 2026-07-20 10:01:30）" in "\n".join(
        sender.messages[0][1]
    )
    [event] = inbox.snapshot()["events"]
    assert event["current_price"] == 10.88
    assert event["current_price_source"] == "realtime_tick"


def test_realtime_quote_failure_keeps_buy_alert_but_fails_closed_ratio(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    inbox = RealtimeReviewInbox(tmp_path / "quote-failure-review.json")

    def unavailable_quote(_code: str):
        raise RuntimeError("quote unavailable")

    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "quote-failure-delivery.json",
        review_inbox=inbox,
        quote_provider=unavailable_quote,
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    rendered = "\n".join(lines)
    assert "买点观察·待人工复核" in title
    assert "最近已完成K线收盘价：10.25" in rendered
    assert "不使用已完成K线价格生成买入比例" in rendered
    assert "结构模型比例上限" not in rendered
    assert "分批买入" not in rendered
    [event] = inbox.snapshot()["events"]
    assert event["current_price_source"] == "latest_completed_bar_close"
    assert event["position_recommendation"]["status"] == "UNRESOLVED"
    assert event["position_recommendation"]["basis"] == "REALTIME_PRICE_UNAVAILABLE"
    assert event["position_recommendation"]["reason_codes"] == [
        "REALTIME_PRICE_UNAVAILABLE"
    ]
    assert event["position_recommendation"]["recommended_percent"] is None


@pytest.mark.parametrize("old_stage", ("armed", "triggered"))
def test_orphan_invalidation_without_delivered_trigger_is_suppressed(
    tmp_path: Path,
    old_stage: str,
) -> None:
    sender = RecordingNotifier()
    state_path = tmp_path / f"orphan-{old_stage}.json"
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=state_path,
    )

    dispatcher.dispatch_changes(snapshot(old_stage), snapshot("invalidated"))
    dispatcher.dispatch_changes(snapshot(old_stage), snapshot("invalidated"))

    assert sender.messages == []
    health = dispatcher.health_snapshot()
    assert health["last_suppressed_reason"] == (
        "ORPHAN_INVALIDATION_WITHOUT_DELIVERED_TRIGGER"
    )
    assert health["suppressed_count"] == 1
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert (
        len([row for row in persisted["event_audit"] if row["status"] == "suppressed"])
        == 1
    )


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


def test_failed_send_expires_instead_of_retrying_an_old_trigger(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier([False, True])
    inbox = RealtimeReviewInbox(tmp_path / "delayed-review.json")
    quote_calls: list[str] = []

    def quote(code: str) -> dict[str, object]:
        quote_calls.append(code)
        return {"code": code, "last": 10.88}

    now = [
        datetime(
            2026,
            7,
            20,
            10,
            1,
            30,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    ]
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delayed-delivery.json",
        review_inbox=inbox,
        clock=lambda: now[0],
        quote_provider=quote,
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))
    now[0] = datetime(
        2026,
        7,
        20,
        10,
        11,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    dispatcher.dispatch_changes(snapshot("triggered"), snapshot("triggered"))

    assert len(sender.messages) == 1
    [event] = inbox.snapshot()["events"]
    assert event["delivery_status"] == "expired"
    assert event["detected_at"] == "2026-07-20T10:01:30+08:00"
    assert event["delivered_at"] is None
    assert quote_calls == ["SZ.000001"]


def test_failed_trigger_retries_after_lifecycle_has_already_advanced(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier([False, True])
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    triggered = signal_document("triggered")
    executable = signal_document("executable")

    dispatcher.dispatch_changes(
        {"signals": [triggered]},
        {"signals": [executable]},
    )
    dispatcher.dispatch_changes(
        {"signals": [executable]},
        {"signals": [executable]},
    )

    assert len(sender.messages) == 2
    health = dispatcher.health_snapshot()
    assert health["success_count"] == 1
    assert health["failure_count"] == 1
    assert health["pending_trigger_event_count"] == 0


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
    assert persisted["schema"] == "chanlun-signal-notifications"
    assert len(persisted["delivered_event_ids"]) == 1
    assert persisted["success_count"] == 1
    assert persisted["failure_count"] == 0


@pytest.mark.parametrize(
    "raw_state",
    ("{broken", '{"schema":"wrong"}'),
)
def test_invalid_dispatch_state_is_not_silently_overwritten_or_replayed(
    tmp_path: Path,
    raw_state: str,
) -> None:
    state_path = tmp_path / "delivered.json"
    state_path.write_text(raw_state, encoding="utf-8")
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=state_path,
    )

    dispatcher.dispatch_changes(snapshot("armed"), snapshot("triggered"))

    assert sender.messages == []
    assert state_path.read_text(encoding="utf-8") == raw_state
    health = dispatcher.health_snapshot()
    assert health["status"] == "unavailable"
    assert health["reason_code"] == "NOTIFICATION_DISPATCH_STATE_INVALID"
    assert health["operationally_verified"] is False


def test_notification_health_records_failure_without_exposing_payload(
    tmp_path: Path,
) -> None:
    now = TEST_NOW
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
