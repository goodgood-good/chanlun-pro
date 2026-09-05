from __future__ import annotations

import json
from datetime import datetime, timedelta
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
    _legacy_trigger_occurrence_event_id,
    _notification_eligibility_reason,
    format_approaching_digest,
    format_notification,
    format_preconfirmation_divergence_digest,
    format_screening_completion,
    screening_completion_event_id,
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
        "segment_difference_1m": {
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
            "raw_high": "10.30",
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


def test_later_center_third_point_is_research_only_for_notifications() -> None:
    signal = signal_document("triggered")
    signal["setup_5m"] = {
        **signal["setup_5m"],
        "center_ordinal": 2,
    }

    assert _notification_eligibility_reason(
        signal,
        old_stage="armed",
        new_stage="triggered",
    ) == "LATER_CENTER_THIRD_POINT_RESEARCH_ONLY"


def test_later_center_research_never_enters_approaching_or_divergence_digests(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "later-center-research-digests.json",
    )
    approaching = approaching_document()
    approaching["setup_5m"]["center_ordinal"] = 2
    divergence = preconfirmation_divergence_document(2)
    divergence["setup_5m"]["center_ordinal"] = 3

    dispatcher.dispatch_approaching_digest({"signals": [approaching, divergence]})

    assert sender.messages == []
    health = dispatcher.health_snapshot()
    assert health["approaching_alerted_occurrence_count"] == 0
    assert health["preconfirmation_divergence_alerted_occurrence_count"] == 0


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
    signal["segment_difference_1m"] = {
        **signal["segment_difference_1m"],
        "point_id": "trigger:shared-1m-1buy",
        "point_type": "1buy",
        "confirmed_at": "2026-07-20T10:01:00+08:00",
    }
    return signal


def approaching_document(
    index: int = 1,
    *,
    available_at: str = "2026-07-20T10:00:00+08:00",
    observed_at: str = "2026-07-20T10:01:30+08:00",
    terminal_segment_id: str | None = None,
    terminal_segment_start_at: str = "2026-07-20T09:30:00+08:00",
    terminal_segment_end_at: str = "2026-07-20T09:55:00+08:00",
) -> dict[str, object]:
    signal = signal_document("approaching")
    code = f"SZ.{index:06d}"
    point_id = f"approaching:{index}:{available_at}"
    signal.update(
        {
            "signal_id": f"signal:approaching:{index}:{available_at}",
            "point_id": point_id,
            "code": code,
            "name": f"候选{index}",
            "observed_at": observed_at,
            "monitor_observed_at": observed_at,
            "entry_allowed": False,
            "exit_allowed": False,
            "segment_difference_1m": {},
        }
    )
    signal["setup_5m"] = {
        **signal["setup_5m"],
        "point_id": point_id,
        "status": "provisional",
        "actionable": False,
        "confirmed_at": None,
        "available_at": available_at,
        "lock_state": "pending",
        "terminal_segment_id": terminal_segment_id or f"terminal:{index}",
        "terminal_segment_role": "latest_unfinished",
        "terminal_segment_source_kind": "segment",
        "terminal_segment_direction": "down",
        "terminal_segment_state": "forming",
        "terminal_segment_start_at": terminal_segment_start_at,
        "terminal_segment_end_at": terminal_segment_end_at,
    }
    return signal


def preconfirmation_divergence_document(
    index: int = 1,
    *,
    available_at: str = "2026-07-20T10:01:00+08:00",
    observed_at: str = "2026-07-20T10:01:30+08:00",
) -> dict[str, object]:
    signal = approaching_document(index, observed_at=observed_at)
    signal["segment_difference_1m"] = None
    signal["setup_5m"] = {
        **signal["setup_5m"],
        "price_basis_revision": "test-raw",
        "terminal_segment_source_kind": "segment",
    }
    signal["execution_profile"] = {
        "structure_signal_confirmed": False,
        "segment_difference_status": "STRUCTURE_PENDING",
        "segment_difference_ready": False,
        "precise_execution_ready": False,
    }
    anchor = TEST_NOW.replace(hour=9, minute=40 + index, second=0)
    terminal_start = anchor.replace(minute=anchor.minute - 1)
    divergence = {
        "point_id": f"divergence:{index}",
        "point_type": "1buy",
        "side": "buy",
        "status": "confirmed",
        "actionable": True,
        "source_frequency": "1m",
        "recursive_level": 0,
        "anchor_at": anchor.isoformat(),
        "confirmed_at": available_at,
        "available_at": available_at,
        "variant": "standard",
        "divergence_kind": "trend",
        "price_basis_revision": "test-raw",
        "terminal_segment_role": "latest_completed",
        "terminal_segment_id": f"terminal:1m:{index}",
        "terminal_segment_source_kind": "segment",
        "terminal_segment_direction": "down",
        "terminal_segment_state": "locked",
        "terminal_segment_start_at": terminal_start.isoformat(),
        "terminal_segment_end_at": anchor.isoformat(),
        "terminal_segment_available_at": available_at,
        "evidence_codes": ["strict_divergence"],
        "missing_conditions": [],
    }
    signal["preconfirmation_divergences_1m"] = [divergence]
    return signal


class RecordingNotifier:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.messages: list[tuple[str, list[str]]] = []
        self.results = list(results or [True])

    def send(self, title: str, lines: list[str]) -> bool:
        self.messages.append((title, lines))
        return self.results.pop(0) if self.results else True

    def send_rich(
        self,
        title: str,
        lines: list[str],
        context: dict[str, object],
    ) -> bool:
        del context
        return self.send(title, lines)


class PlainRecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, list[str]]] = []

    def send(self, title: str, lines: list[str]) -> bool:
        self.messages.append((title, lines))
        return True


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


def completed_screening_snapshot(
    *,
    session: str = "2026-07-20",
    monitoring_only: bool = False,
) -> dict[str, object]:
    market_data_as_of = datetime.fromisoformat(f"{session}T15:00:00+08:00")
    signals = [
        {"code": "SZ.000001", "side": "buy"},
        {"code": "SZ.000001", "side": "buy"},
        {"code": "SH.600000", "side": "sell"},
    ]
    return {
        "available": True,
        "scan_state": "complete",
        "last_batch_state": "complete",
        "full_coverage_state": "complete",
        "read_only": True,
        "no_order_execution": True,
        "market_data_as_of": market_data_as_of.isoformat(),
        "coverage_epoch_id": f"coverage:{session}",
        "signals": signals,
        "errors": [],
        "coverage_manifest": {"complete": True},
        "data_quality": {"complete": True, "stale": False, "failure_codes": []},
        "scan_audit": {
            "coverage_cycle_complete": True,
            "monitoring_only_refresh": monitoring_only,
            "discovered_symbol_count": 4,
            "coverage_cycle_completed_symbol_count": 3,
            "coverage_cycle_excluded_symbol_count": 1,
            "coverage_cycle_failed_symbol_count": 0,
            "pending_symbol_count": 0,
            "immediate_pending_symbol_count": 0,
            "backoff_retry_symbol_count": 0,
            "next_epoch_retry_symbol_count": 0,
        },
    }


def completed_screening_snapshot_with_deferred_exclusion() -> dict[str, object]:
    snapshot = completed_screening_snapshot()
    excluded_code = "BJ.430001"
    snapshot["coverage_manifest"] = {
        "complete": True,
        "discovered_codes": [
            excluded_code,
            "SH.600000",
            "SZ.000001",
            "SZ.000002",
        ],
        "completed_codes": ["SH.600000", "SZ.000001", "SZ.000002"],
        "excluded_codes": [excluded_code],
        "failed_codes": [],
        "exclusions": [
            {
                "code": excluded_code,
                "exclusion_type": "stock_analysis_exclusion",
                "eligibility": "EXCLUDED_FOR_CURRENT_MARKET_DATA_EPOCH",
                "reason_code": "FROZEN_MINIMUM_HISTORY_NOT_MET",
                "retry_policy": "NEXT_MARKET_DATA_EPOCH",
                "deterministic_for_coverage_epoch": True,
                "remote_error_type": "InsufficientHistoryError",
                "reason": "frozen minimum history is not met",
            }
        ],
        "pending_frequencies": {},
        "backoff_frequencies": {},
        "deferred_frequencies": {excluded_code: ["d", "30m", "5m", "1m"]},
    }
    snapshot["scan_audit"]["next_epoch_retry_symbol_count"] = 1
    snapshot["scan_audit"]["retry_symbol_count"] = 1
    return snapshot


def test_daily_screening_completion_is_durable_once_per_market_session(
    tmp_path: Path,
) -> None:
    completed_at = datetime(
        2026,
        7,
        20,
        15,
        18,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    state_path = tmp_path / "daily-completion.json"
    current = completed_screening_snapshot()
    sender = RichRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=state_path,
        clock=lambda: completed_at,
    )

    dispatcher.dispatch_screening_completion({}, current)
    dispatcher.dispatch_screening_completion(current, current)

    assert len(sender.rich_messages) == 1
    title, lines, context = sender.rich_messages[0]
    assert title == "买卖通知｜每日选股完成｜2026-07-20"
    assert lines == [
        "结论：2026-07-20 日终选股已完成，本轮完整结果已生成。",
        "范围：发现 4 只｜完成 3 只｜排除 1 只｜失败 0 只｜待处理 0 只",
        "结果：入选标的 2 只｜结构 3 个｜买入方向 2 个｜卖出方向 1 个",
        "时间：行情截止 2026-07-20 15:00:00｜任务完成 2026-07-20 15:18:00",
        "说明：这是选股任务完成回执，不是买卖建议；具体买卖点仍以独立实时通知为准；系统不会自动下单",
    ]
    assert context == {
        "artifact_key": screening_completion_event_id("2026-07-20"),
        "require_evidence_match": False,
        "delivery_priority": 80,
        "charts": [],
        "notification_kind": "daily_screening_completion",
        "market_data_session": "2026-07-20",
    }
    health = dispatcher.health_snapshot()
    assert health["screening_completion_session_count"] == 1
    assert health["last_screening_completion_session"] == "2026-07-20"
    assert health["last_screening_completion_at"] == completed_at.isoformat()
    assert health["pending_screening_completion_count"] == 0

    restarted_sender = RichRecordingNotifier()
    restarted = SignalNotificationDispatcher(
        restarted_sender,
        state_path=state_path,
        clock=lambda: completed_at + timedelta(minutes=1),
    )
    restarted.dispatch_screening_completion({}, current)

    assert restarted_sender.rich_messages == []

    next_sender = RichRecordingNotifier()
    next_day = completed_at + timedelta(days=1)
    next_dispatcher = SignalNotificationDispatcher(
        next_sender,
        state_path=state_path,
        clock=lambda: next_day,
    )
    next_snapshot = completed_screening_snapshot(session="2026-07-21")
    next_dispatcher.dispatch_screening_completion(current, next_snapshot)

    assert len(next_sender.rich_messages) == 1
    assert next_sender.rich_messages[0][0] == (
        "买卖通知｜每日选股完成｜2026-07-21"
    )
    assert next_dispatcher.health_snapshot()["screening_completion_session_count"] == 2


def test_daily_screening_completion_accepts_authenticated_deferred_exclusion(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "daily-deterministic-exclusion.json",
    )

    dispatcher.dispatch_screening_completion(
        {}, completed_screening_snapshot_with_deferred_exclusion()
    )

    assert len(sender.messages) == 1
    assert "排除 1 只" in sender.messages[0][1][1]
    assert "下周期复查 1 只" in sender.messages[0][1][1]
    assert dispatcher.health_snapshot()["screening_completion_session_count"] == 1


@pytest.mark.parametrize(
    "mutation",
    (
        "deferred_not_excluded",
        "nondeterministic",
        "wrong_retry_policy",
        "retry_count_mismatch",
        "missing_exclusion_evidence",
    ),
)
def test_daily_screening_completion_rejects_unauthenticated_deferred_exclusion(
    tmp_path: Path,
    mutation: str,
) -> None:
    current = completed_screening_snapshot_with_deferred_exclusion()
    manifest = current["coverage_manifest"]
    if mutation == "deferred_not_excluded":
        manifest["deferred_frequencies"] = {"SH.600000": ["d"]}
    elif mutation == "nondeterministic":
        manifest["exclusions"][0]["deterministic_for_coverage_epoch"] = False
    elif mutation == "wrong_retry_policy":
        manifest["exclusions"][0]["retry_policy"] = "NEXT_REFRESH_AFTER_BACKOFF"
    elif mutation == "retry_count_mismatch":
        current["scan_audit"]["retry_symbol_count"] = 2
    else:
        manifest["exclusions"] = []
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / f"daily-deferred-{mutation}.json",
    )

    dispatcher.dispatch_screening_completion({}, current)

    assert sender.messages == []
    assert dispatcher.health_snapshot()["screening_completion_session_count"] == 0


def test_daily_screening_completion_does_not_retroactively_send_loaded_snapshot(
    tmp_path: Path,
) -> None:
    current = completed_screening_snapshot()
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "fresh-dispatch-state.json",
        clock=lambda: datetime(
            2026,
            7,
            20,
            15,
            20,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )

    dispatcher.dispatch_screening_completion(current, current)

    assert sender.messages == []
    assert dispatcher.health_snapshot()["screening_completion_session_count"] == 0


def test_daily_screening_completion_retries_exact_message_after_restart(
    tmp_path: Path,
) -> None:
    completed_at = datetime(
        2026,
        7,
        20,
        15,
        18,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    state_path = tmp_path / "daily-completion-retry.json"
    current = completed_screening_snapshot()
    failed_sender = RecordingNotifier([False])
    failed = SignalNotificationDispatcher(
        failed_sender,
        state_path=state_path,
        clock=lambda: completed_at,
    )

    failed.dispatch_screening_completion({}, current)

    assert len(failed_sender.messages) == 1
    assert failed.health_snapshot()["pending_screening_completion_count"] == 1
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert list(persisted["pending_screening_completions"]) == ["2026-07-20"]

    retry_sender = RecordingNotifier([True])
    retried = SignalNotificationDispatcher(
        retry_sender,
        state_path=state_path,
        clock=lambda: completed_at,
    )
    retried.dispatch_screening_completion(current, current)

    assert retry_sender.messages == failed_sender.messages
    assert retried.health_snapshot()["pending_screening_completion_count"] == 0
    assert retried.health_snapshot()["last_screening_completion_session"] == (
        "2026-07-20"
    )


@pytest.mark.parametrize(
    "mutation",
    ("preclose", "monitoring_only", "incomplete", "failed_symbol"),
)
def test_daily_screening_completion_rejects_nonfinal_cycles(
    tmp_path: Path,
    mutation: str,
) -> None:
    current = completed_screening_snapshot()
    if mutation == "preclose":
        current["market_data_as_of"] = "2026-07-20T14:59:59+08:00"
    elif mutation == "monitoring_only":
        current["scan_audit"]["monitoring_only_refresh"] = True
    elif mutation == "incomplete":
        current["coverage_manifest"]["complete"] = False
    else:
        current["scan_audit"]["coverage_cycle_completed_symbol_count"] = 2
        current["scan_audit"]["coverage_cycle_failed_symbol_count"] = 1
        current["data_quality"]["complete"] = False
        current["data_quality"]["failure_codes"] = ["stock_scan_partial"]
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / f"daily-{mutation}.json",
    )

    dispatcher.dispatch_screening_completion({}, current)

    assert sender.messages == []
    assert dispatcher.health_snapshot()["screening_completion_session_count"] == 0
    assert dispatcher.health_snapshot()["pending_screening_completion_count"] == 0


def test_screening_completion_formatter_requires_complete_daily_snapshot() -> None:
    with pytest.raises(ValueError, match="complete daily selection"):
        format_screening_completion(
            completed_screening_snapshot(monitoring_only=True),
            completed_at=TEST_NOW,
        )


def test_preconfirmation_divergence_notifies_once_without_promoting_five_minute(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-divergence.json",
    )
    signals = [
        preconfirmation_divergence_document(index) for index in range(1, 11)
    ]

    dispatcher.dispatch_approaching_digest({"signals": signals})
    dispatcher.dispatch_approaching_digest({"signals": signals})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert title == "买卖通知｜1分钟背驰预警·5分钟未确认｜10个"
    assert lines[0] == (
        "结论：发现 10 个已确认的1分钟背驰；对应5分钟结构仍未确认，"
        "这是提前观察，不是正式买卖点，不可据此操作"
    )
    assert "买入方向 10｜卖出方向 0｜本条展示 8 个" in lines[1]
    assert sum(line.startswith("预警") for line in lines) == 8
    assert any("其余：还有 2 个1分钟背驰预警" in line for line in lines)
    assert any("5分钟结构正式确认后" in line for line in lines)
    assert all("可执行" not in line for line in lines)
    health = dispatcher.health_snapshot()
    assert health["accepted_event_count"] == 1
    assert health["preconfirmation_divergence_alerted_occurrence_count"] == 10
    assert health["preconfirmation_divergence_digest_pending"] is False
    assert health["approaching_alerted_occurrence_count"] == 0


def test_preconfirmation_divergence_has_priority_and_required_chart_capture(
    tmp_path: Path,
) -> None:
    sender = RichRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-divergence-rich.json",
    )

    dispatcher.dispatch_approaching_digest(
        {"signals": [preconfirmation_divergence_document()]}
    )

    assert len(sender.rich_messages) == 1
    _title, _lines, context = sender.rich_messages[0]
    assert context["artifact_key"].startswith("sha256:")
    assert context["require_evidence_match"] is False
    assert context["require_chart"] is True
    assert context["delivery_priority"] == 8
    assert len(context["charts"]) == 1
    assert context["charts"][0]["code"] == "SZ.000001"
    assert context["charts"][0]["evidence_required"] is False
    assert context["expires_at"] == "2026-07-20T10:11:30+08:00"


def test_preconfirmation_required_chart_never_uses_plain_transport(
    tmp_path: Path,
) -> None:
    sender = PlainRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-plain-transport.json",
    )

    dispatcher.dispatch_approaching_digest(
        {"signals": [preconfirmation_divergence_document()]}
    )

    assert sender.messages == []
    health = dispatcher.health_snapshot()
    assert health["preconfirmation_divergence_digest_pending"] is True
    assert health["last_failure_reason"] == (
        "REQUIRED_CHART_TRANSPORT_UNAVAILABLE"
    )


def test_preconfirmation_divergence_accepts_locked_unconfirmed_five_minute_geometry(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-divergence-locked-geometry.json",
    )
    signal = preconfirmation_divergence_document()
    signal["lifecycle_stage"] = "formed"
    signal["setup_5m"] = {
        **signal["setup_5m"],
        "terminal_segment_role": "latest_completed",
        "terminal_segment_state": "locked",
    }

    dispatcher.dispatch_approaching_digest({"signals": [signal]})

    assert len(sender.messages) == 1
    assert "5分钟未确认" in sender.messages[0][0]


def test_new_one_minute_divergence_rearms_same_forming_five_minute_structure(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-divergence-rearm.json",
    )
    first = preconfirmation_divergence_document(1)
    updated = preconfirmation_divergence_document(1)
    second_divergence = preconfirmation_divergence_document(2)[
        "preconfirmation_divergences_1m"
    ][0]
    updated["preconfirmation_divergences_1m"] = [
        first["preconfirmation_divergences_1m"][0],
        second_divergence,
    ]

    dispatcher.dispatch_approaching_digest({"signals": [first]})
    dispatcher.dispatch_approaching_digest({"signals": [updated]})

    assert len(sender.messages) == 2
    assert sender.messages[0][0].endswith("1个")
    assert sender.messages[1][0].endswith("1个")
    assert (
        dispatcher.health_snapshot()[
            "preconfirmation_divergence_alerted_occurrence_count"
        ]
        == 2
    )


def test_failed_preconfirmation_divergence_retries_exact_persisted_event(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "preconfirmation-divergence-retry.json"
    failed_sender = RecordingNotifier([False])
    failed = SignalNotificationDispatcher(failed_sender, state_path=state_path)
    current = {"signals": [preconfirmation_divergence_document()]}

    failed.dispatch_approaching_digest(current)
    failed_health = failed.health_snapshot()
    assert failed_health["preconfirmation_divergence_digest_pending"] is True
    assert failed_health["preconfirmation_divergence_alerted_occurrence_count"] == 0
    pending_event_id = json.loads(state_path.read_text(encoding="utf-8"))[
        "pending_preconfirmation_divergence_digest"
    ]["event_id"]

    retry_sender = RichRecordingNotifier()
    retried = SignalNotificationDispatcher(retry_sender, state_path=state_path)
    retried.dispatch_approaching_digest(current)

    assert len(retry_sender.rich_messages) == 1
    assert retry_sender.rich_messages[0][2]["artifact_key"] == pending_event_id
    assert retried.health_snapshot()["preconfirmation_divergence_digest_pending"] is False
    assert (
        retried.health_snapshot()[
            "preconfirmation_divergence_alerted_occurrence_count"
        ]
        == 1
    )


def test_preconfirmation_divergence_dedupe_survives_restart(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "preconfirmation-divergence-restart.json"
    current = {"signals": [preconfirmation_divergence_document()]}
    first_sender = RecordingNotifier()
    first = SignalNotificationDispatcher(first_sender, state_path=state_path)

    first.dispatch_approaching_digest(current)

    restarted_sender = RecordingNotifier()
    restarted = SignalNotificationDispatcher(
        restarted_sender,
        state_path=state_path,
    )
    restarted.dispatch_approaching_digest(current)

    assert len(first_sender.messages) == 1
    assert restarted_sender.messages == []
    assert (
        restarted.health_snapshot()[
            "preconfirmation_divergence_alerted_occurrence_count"
        ]
        == 1
    )


def test_preconfirmation_dedupe_ignores_rebuilt_internal_ids_and_availability(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-rebuild.json",
    )
    initial = preconfirmation_divergence_document()
    rebuilt = preconfirmation_divergence_document()
    rebuilt["signal_id"] = "signal:rebuilt-parent"
    rebuilt["point_id"] = "approaching:rebuilt-parent"
    rebuilt["setup_5m"] = {
        **rebuilt["setup_5m"],
        "point_id": "approaching:rebuilt-parent",
        "terminal_segment_id": "terminal:rebuilt-parent",
        "terminal_segment_end_at": "2026-07-20T10:00:00+08:00",
    }
    [divergence] = rebuilt["preconfirmation_divergences_1m"]
    rebuilt_divergence = {
        **divergence,
        "point_id": "divergence:rebuilt",
        "terminal_segment_id": "terminal:1m:rebuilt",
        "available_at": "2026-07-20T10:01:15+08:00",
        "confirmed_at": "2026-07-20T10:01:15+08:00",
        "terminal_segment_available_at": "2026-07-20T10:01:15+08:00",
    }
    rebuilt["preconfirmation_divergences_1m"] = [rebuilt_divergence]

    dispatcher.dispatch_approaching_digest({"signals": [initial]})
    dispatcher.dispatch_approaching_digest({"signals": [rebuilt]})

    assert len(sender.messages) == 1
    assert (
        dispatcher.health_snapshot()[
            "preconfirmation_divergence_alerted_occurrence_count"
        ]
        == 1
    )


def test_preconfirmation_divergence_never_sends_before_state_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-divergence-persist-failure.json",
    )

    def fail_persist() -> None:
        raise OSError("injected persistence failure")

    monkeypatch.setattr(dispatcher, "_persist", fail_persist)
    with pytest.raises(OSError, match="injected persistence failure"):
        dispatcher.dispatch_approaching_digest(
            {"signals": [preconfirmation_divergence_document()]}
        )

    assert sender.messages == []
    assert (
        dispatcher.health_snapshot()[
            "preconfirmation_divergence_digest_pending"
        ]
        is False
    )


def test_preconfirmation_divergence_is_recorded_for_human_review(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    inbox = RealtimeReviewInbox(tmp_path / "preconfirmation-review.json")
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-review-delivery.json",
        review_inbox=inbox,
    )

    dispatcher.dispatch_approaching_digest(
        {"signals": [preconfirmation_divergence_document()]}
    )

    assert len(sender.messages) == 1
    [event] = inbox.snapshot()["events"]
    assert event["old_stage"] == "approaching"
    assert event["new_stage"] == "approaching"
    assert event["delivery_status"] == "delivered"
    assert event["segment_difference_present"] is True
    assert event["segment_difference_evidence_id"] == "divergence:1"
    assert event["automated_action_authorized"] is False


def test_preconfirmation_divergence_rejects_stale_opposite_and_formal_rows(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "preconfirmation-divergence-ineligible.json",
    )
    stale = preconfirmation_divergence_document(
        1,
        available_at="2026-07-17T10:01:00+08:00",
        observed_at="2026-07-17T10:01:30+08:00",
    )
    opposite = preconfirmation_divergence_document(2)
    opposite["preconfirmation_divergences_1m"][0]["side"] = "sell"
    opposite["warmup"] = {"converged": False}
    formal = preconfirmation_divergence_document(3)
    formal["lifecycle_stage"] = "triggered"
    formal["setup_5m"]["status"] = "confirmed"
    formal["setup_5m"]["actionable"] = True

    dispatcher.dispatch_approaching_digest(
        {"signals": [stale, opposite, formal]}
    )

    assert sender.messages == []
    assert (
        dispatcher.health_snapshot()[
            "preconfirmation_divergence_alerted_occurrence_count"
        ]
        == 0
    )


def test_preconfirmation_divergence_formatter_rejects_bad_side_counts() -> None:
    signal = preconfirmation_divergence_document()
    signal["notification_preconfirmation_divergence_1m"] = (
        signal["preconfirmation_divergences_1m"][0]
    )
    with pytest.raises(ValueError, match="side counts"):
        format_preconfirmation_divergence_digest(
            [signal],
            total_count=2,
            buy_count=1,
            sell_count=0,
        )


def test_approaching_digest_is_compact_explicit_and_deduplicated(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "approaching-digest.json",
    )
    signals = [approaching_document(index) for index in range(1, 11)]

    dispatcher.dispatch_approaching_digest({"signals": signals})
    dispatcher.dispatch_approaching_digest({"signals": signals})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert title == "买卖通知｜结构预警·尚未确认｜10个候选"
    assert lines[0] == (
        "结论：发现 10 个5分钟结构接近形成；"
        "全部尚未确认，不是正式买卖点，不可据此操作"
    )
    assert "买入候选 10｜卖出候选 0｜本条展示 8 个" in lines[1]
    assert sum(line.startswith("候选") for line in lines) == 8
    assert any("其余：还有 2 个候选" in line for line in lines)
    assert any("同一结构只预警一次｜15分钟内最多一条摘要" in line for line in lines)
    assert all("可人工复核执行" not in line for line in lines)
    health = dispatcher.health_snapshot()
    assert health["accepted_event_count"] == 1
    assert health["approaching_alerted_occurrence_count"] == 10
    assert health["approaching_digest_pending"] is False


def test_approaching_digest_uses_a_required_chart_low_priority_outbox_event(
    tmp_path: Path,
) -> None:
    sender = RichRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "approaching-rich.json",
    )

    dispatcher.dispatch_approaching_digest(
        {"signals": [approaching_document()]}
    )

    assert len(sender.rich_messages) == 1
    _title, _lines, context = sender.rich_messages[0]
    assert context["artifact_key"].startswith("sha256:")
    assert context["require_evidence_match"] is False
    assert context["require_chart"] is True
    assert context["delivery_priority"] == 20
    assert len(context["charts"]) == 1
    assert context["charts"][0]["code"] == "SZ.000001"
    assert context["charts"][0]["evidence_required"] is False
    assert context["expires_at"] == "2026-07-20T10:11:30+08:00"


def test_approaching_digest_cooldown_buffers_new_current_structures(
    tmp_path: Path,
) -> None:
    now = [TEST_NOW]
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "approaching-cooldown.json",
        clock=lambda: now[0],
    )
    first = approaching_document(1)
    second = approaching_document(
        2,
        available_at="2026-07-20T10:02:00+08:00",
        observed_at="2026-07-20T10:02:30+08:00",
    )

    dispatcher.dispatch_approaching_digest({"signals": [first]})
    now[0] = datetime(2026, 7, 20, 10, 2, 30, tzinfo=TEST_NOW.tzinfo)
    dispatcher.dispatch_approaching_digest({"signals": [first, second]})
    assert len(sender.messages) == 1
    assert dispatcher.health_snapshot()["approaching_alerted_occurrence_count"] == 1

    now[0] = datetime(2026, 7, 20, 10, 16, 30, tzinfo=TEST_NOW.tzinfo)
    dispatcher.dispatch_approaching_digest({"signals": [first, second]})
    assert len(sender.messages) == 2
    assert sender.messages[1][0].endswith("1个候选")
    assert dispatcher.health_snapshot()["approaching_alerted_occurrence_count"] == 2


def test_approaching_occurrence_dedupe_survives_restart_and_rolling_point_ids(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "approaching-restart.json"
    first_sender = RecordingNotifier()
    first = SignalNotificationDispatcher(first_sender, state_path=state_path)
    initial = approaching_document(1, terminal_segment_id="terminal:stable")
    first.dispatch_approaching_digest({"signals": [initial]})
    assert len(first_sender.messages) == 1

    later = datetime(2026, 7, 20, 10, 20, 0, tzinfo=TEST_NOW.tzinfo)
    rebuilt = approaching_document(
        1,
        available_at="2026-07-20T10:15:00+08:00",
        observed_at="2026-07-20T10:16:00+08:00",
        # A converged rebuild may replace the graph hash and extend the
        # unfinished tail without creating a new user-facing occurrence.
        terminal_segment_id="terminal:rebuilt",
        terminal_segment_end_at="2026-07-20T10:15:00+08:00",
    )
    restarted_sender = RecordingNotifier()
    restarted = SignalNotificationDispatcher(
        restarted_sender,
        state_path=state_path,
        clock=lambda: later,
    )
    restarted.dispatch_approaching_digest({"signals": [rebuilt]})

    assert restarted_sender.messages == []
    assert restarted.health_snapshot()["approaching_alerted_occurrence_count"] == 1

    genuinely_new = approaching_document(
        1,
        available_at="2026-07-20T10:18:00+08:00",
        observed_at="2026-07-20T10:19:00+08:00",
        terminal_segment_id="terminal:new-lane",
        terminal_segment_start_at="2026-07-20T10:05:00+08:00",
        terminal_segment_end_at="2026-07-20T10:15:00+08:00",
    )
    restarted.dispatch_approaching_digest({"signals": [genuinely_new]})

    assert len(restarted_sender.messages) == 1
    assert restarted.health_snapshot()["approaching_alerted_occurrence_count"] == 2


def test_failed_approaching_digest_retries_the_same_persisted_event(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "approaching-retry.json"
    failed_sender = RecordingNotifier([False])
    failed = SignalNotificationDispatcher(failed_sender, state_path=state_path)
    current = {"signals": [approaching_document()]}

    failed.dispatch_approaching_digest(current)
    failed_health = failed.health_snapshot()
    assert failed_health["approaching_digest_pending"] is True
    assert failed_health["approaching_alerted_occurrence_count"] == 0
    pending_event_id = json.loads(state_path.read_text(encoding="utf-8"))[
        "pending_approaching_digest"
    ]["event_id"]

    retry_sender = RichRecordingNotifier()
    retried = SignalNotificationDispatcher(retry_sender, state_path=state_path)
    retried.dispatch_approaching_digest(current)

    assert len(retry_sender.rich_messages) == 1
    assert retry_sender.rich_messages[0][2]["artifact_key"] == pending_event_id
    assert retried.health_snapshot()["approaching_digest_pending"] is False
    assert retried.health_snapshot()["approaching_alerted_occurrence_count"] == 1


def test_approaching_digest_never_sends_before_pending_state_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "approaching-persist-failure.json",
    )

    def fail_persist() -> None:
        raise OSError("injected persistence failure")

    monkeypatch.setattr(dispatcher, "_persist", fail_persist)
    with pytest.raises(OSError, match="injected persistence failure"):
        dispatcher.dispatch_approaching_digest(
            {"signals": [approaching_document()]}
        )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["approaching_digest_pending"] is False


def test_approaching_digest_rejects_stale_unconverged_and_formal_signals(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "approaching-ineligible.json",
    )
    stale = approaching_document(
        1,
        available_at="2026-07-17T10:00:00+08:00",
        observed_at="2026-07-17T10:01:00+08:00",
    )
    unconverged = approaching_document(2)
    unconverged["warmup"] = {"converged": False}
    formal = signal_document("triggered")

    dispatcher.dispatch_approaching_digest(
        {"signals": [stale, unconverged, formal]}
    )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["approaching_alerted_occurrence_count"] == 0


def test_approaching_digest_formatter_rejects_inconsistent_counts() -> None:
    with pytest.raises(ValueError, match="side counts"):
        format_approaching_digest(
            [approaching_document()],
            total_count=2,
            buy_count=1,
            sell_count=0,
        )


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
    assert "时效：日期 2026-07-20｜5分钟确认 10:00:00" in rendered
    assert "末端结构=未封存，仍随新K更新" in rendered
    assert "监听发现 10:01:30" in rendered
    assert "最近已完成K线收盘价：10.25" in rendered
    assert "三类买点（递归层级：L0）" in rendered
    assert "1分钟区间套定位：一类买点" in rendered
    assert "区间套定位：一类买点（递归层级：L0）" not in rendered
    assert "失效：5分钟失效价 9.80（跌破买入结构失效）" in rendered
    assert "30分钟向上（有利）" in rendered
    assert "5分钟=三类买点" in rendered
    assert "1分钟区间套定位：一类买点" in rendered
    assert "可人工复核分批买入" in rendered
    assert "系统不会自动下单" in rendered
    assert (
        "风险参考：结构模型比例上限 1.7%"
        "（按当前价至5分钟失效价；非仓位建议）"
    ) in rendered
    assert "较5分钟锚点 +2.50%" in rendered
    assert "状态：可人工复核执行" in rendered
    assert not any(
        term in f"{title}\n{rendered}"
        for term in ("账户", "现金", "持仓", "虚拟", "组合热度")
    )
    assert rendered.count("非仓位建议") == 1
    assert "进度：等待操作确认→5分钟正式点确认" in rendered
    assert "计划风险倍数" not in rendered
    assert "结构层级" not in rendered


def test_ready_buy_notification_starts_with_an_executable_judgment_card() -> None:
    signal = signal_document("triggered")
    signal["current_price_source"] = "realtime_tick"
    signal["current_price_at"] = "2026-07-20T10:01:29+08:00"

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert title.startswith("买卖通知｜新买点·待人工确认｜")
    assert lines[0] == (
        "结论：可人工复核分批买入；三买回抽已确认，"
        "须满足下方全部执行与风险边界"
    )
    assert lines[1].startswith("标的：SZ.000001｜状态：可人工复核执行")
    assert lines[2] == (
        "判断：5分钟=三类买点（递归层级：L0）已确认｜"
        "末端结构=未封存，仍随新K更新｜"
        "1分钟区间套定位：一类买点已确认（窗口有效）｜风险门=全部通过"
    )
    assert lines[3] == (
        "执行：当前价：10.25（获取 2026-07-20 10:01:29）｜"
        "较5分钟锚点 +2.50%｜1分钟买入上限 10.3｜"
        "有效至 当日 10:02:00｜≤上限，价格条件通过"
    )


def test_notification_content_contract_is_compact_and_decision_first() -> None:
    signal = signal_document("triggered")
    signal["current_price_source"] = "realtime_tick"
    signal["current_price_at"] = "2026-07-20T10:01:29+08:00"

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert [line.split("：", 1)[0] for line in lines] == [
        "结论",
        "标的",
        "判断",
        "执行",
        "失效",
        "风险参考",
        "时效",
        "背景",
        "说明",
    ]
    rendered = "\n".join(lines)
    assert rendered.count("当前价：10.25") == 1
    assert "证据暂不可用" not in rendered
    assert "末端线段：血缘暂不可用" not in rendered
    assert lines[4].endswith("距向下失效 4.39%")
    assert lines[6].count("2026-07-20") == 1
    assert sum(len(value) for value in (title, *lines)) <= 500


@pytest.mark.parametrize(
    ("side", "current_price", "defense_price", "expected"),
    (
        ("buy", 10.25, "9.80", "距向下失效 4.39%"),
        ("buy", 9.70, "9.80", "已跌破失效价 1.03%"),
        ("sell", 10.25, "10.80", "距向上失效 5.37%"),
        ("sell", 10.90, "10.80", "已突破失效价 0.92%"),
    ),
)
def test_invalidation_distance_names_direction_and_crossing_state(
    side: str,
    current_price: float,
    defense_price: str,
    expected: str,
) -> None:
    signal = signal_document("triggered")
    signal["side"] = side
    signal["current_price"] = current_price
    signal["setup_5m"] = {
        **signal["setup_5m"],
        "side": side,
        "invalidation_price": defense_price,
    }

    _title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    invalidation_line = next(line for line in lines if line.startswith("失效："))
    assert expected in invalidation_line


def test_invalidated_notification_explains_what_crossed_and_what_is_void() -> None:
    signal = signal_document("invalidated")
    signal["current_price"] = 9.70
    signal["current_price_source"] = "realtime_tick"
    signal["current_price_at"] = "2026-07-20T10:02:10+08:00"

    _title, lines = format_notification(
        signal,
        old_stage="triggered",
        new_stage="invalidated",
    )

    assert "1分钟区间套定位：原定位已作废" in lines[2]
    assert "风险门=不再适用" in lines[2]
    assert "窗口有效" not in lines[2]
    assert "当前价：9.7" in lines[3]
    assert "旧1分钟定位与旧模型比例同时作废" in lines[3]
    assert lines[4].endswith("已跌破失效价 1.03%")
    assert "原5分钟确认 10:00:00" in lines[6]


def test_compact_risk_gate_expands_any_non_green_dimension() -> None:
    signal = signal_document("triggered")
    signal["higher_timeframe_risk"] = {
        "market_gate": "GREEN",
        "sector_gate": "AMBER",
        "symbol_gate": "RED",
    }

    _title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert "风险门=市场通过／板块谨慎／个股阻断" in lines[2]
    assert "风险门=全部通过" not in lines[2]


def test_judgment_card_keeps_terminal_structure_lock_separate() -> None:
    signal = signal_document("triggered")
    signal["setup_5m"] = {**signal["setup_5m"], "lock_state": "locked"}

    _title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    assert "5分钟=三类买点（递归层级：L0）已确认" in lines[2]
    assert "末端结构=已封存" in lines[2]


def test_buy_above_one_minute_cap_is_fail_closed_in_every_guidance_line() -> None:
    signal = signal_document("triggered")
    signal["current_price"] = 10.31
    signal["current_price_source"] = "realtime_tick"

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    rendered = "\n".join(lines)
    assert title.startswith("买卖通知｜买点确认·禁止追价｜")
    assert lines[0].startswith("结论：禁止追价；当前价已超过1分钟买入上限 10.3")
    assert "状态：禁止追价（超过1分钟买入上限）" in lines[1]
    assert "当前价：10.31" in lines[3]
    assert ">上限，禁止追价" in lines[3]
    assert "风险参考：本次执行比例 0%" in rendered
    assert "结构模型比例上限" not in rendered
    assert "手工分批买入" not in rendered


def test_buy_above_one_minute_cap_is_zeroed_in_review_projection(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    inbox = RealtimeReviewInbox(tmp_path / "cap-review.json")
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "cap-delivery.json",
        review_inbox=inbox,
    )
    signal = signal_document("triggered")
    signal["current_price"] = 10.31
    signal["current_price_source"] = "realtime_tick"

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [signal]})

    [event] = inbox.snapshot()["events"]
    recommendation = event["position_recommendation"]
    assert recommendation["status"] == "BLOCKED"
    assert recommendation["recommended_percent"] == "0"
    assert recommendation["automated_order_authorized"] is False


def test_missing_one_minute_raw_high_never_reuses_five_minute_anchor() -> None:
    signal = signal_document("triggered")
    boundary = dict(signal["entry_execution_boundary"])
    boundary.pop("raw_high")
    signal["entry_execution_boundary"] = boundary

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    rendered = "\n".join(lines)
    assert title.startswith("买卖通知｜买点确认·执行边界缺失｜")
    assert "状态：5分钟信号保留，1分钟执行上限缺失" in lines[1]
    assert "5分钟锚点不得替代" in lines[3]
    assert "本次执行比例 0%（1分钟确认K最高价缺失）" in rendered
    assert "结构模型比例上限" not in rendered
    assert "手工分批买入" not in rendered


@pytest.mark.parametrize(
    "missing_field",
    ("confirmation_bar_closed_at", "entry_valid_until"),
)
def test_incomplete_one_minute_time_boundary_is_fail_closed(
    missing_field: str,
) -> None:
    signal = signal_document("triggered")
    boundary = dict(signal["entry_execution_boundary"])
    boundary.pop(missing_field)
    signal["entry_execution_boundary"] = boundary

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    rendered = "\n".join(lines)
    assert title.startswith("买卖通知｜买点确认·执行边界缺失｜")
    assert "本次执行比例 0%" in rendered
    assert "手工分批买入" not in rendered


def test_five_minute_buy_waiting_for_one_minute_locator_is_not_executable() -> None:
    signal = _without_one_minute_segment(signal_document("triggered"))

    title, lines = format_notification(
        signal,
        old_stage="armed",
        new_stage="triggered",
    )

    rendered = "\n".join(lines)
    assert title.startswith("买卖通知｜买点确认·等待1分钟定位｜")
    assert "5分钟买点已确认" in lines[0]
    assert "等待同向1分钟区间套" in lines[0]
    assert "1分钟区间套定位：待出现" in lines[2]
    assert "未定位前不执行" in lines[3]
    assert "风险参考：暂不计算" in rendered
    assert "手工分批买入" not in rendered


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
    signal["segment_difference_1m"] = {
        **signal["segment_difference_1m"],
        "confirmed_at": "2026-07-20T13:01:00+08:00",
        "available_at": "2026-07-20T13:01:00+08:00",
    }
    signal["entry_execution_boundary"] = {
        "confirmation_bar_closed_at": "2026-07-20T13:01:00+08:00",
        "raw_high": "10.30",
        "entry_valid_until": "2026-07-20T13:02:00+08:00",
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [signal]})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert "新买点·待人工确认" in title
    rendered = "\n".join(lines)
    assert "结构模型比例上限" in rendered
    assert "监听发现 13:01:00（延迟 1分钟）" in rendered
    assert "买点已超过10分钟" not in rendered
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

    assert lines[0] == (
        "结论：本条买入不纳入操作计划；三买离开中枢的价格空间不足一个最小价位"
    )
    assert "谨慎" not in lines[0]


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
    assert "时效：日期 2026-07-20｜5分钟确认 10:01:00" in rendered
    assert "末端结构=未封存，仍随新K更新" in rendered
    assert "信号可用 10:04:00" in rendered
    assert "监听发现 10:04:25" in rendered
    assert "三类买点（递归层级：L1）" in rendered


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
    assert event["delivery_reason"] == (
        "REQUIRED_CHART_TRANSPORT_UNAVAILABLE"
    )
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
    assert context["require_chart"] is True
    assert sender.rich_messages[0][2]["require_evidence_match"] is True


def _without_one_minute_segment(
    signal: dict[str, object],
) -> dict[str, object]:
    previous = dict(signal)
    previous["segment_difference_1m"] = None
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
    assert "1分钟精确定位新出现" in title
    assert "5分钟三类买点＋1分钟区间套一类买点" in title
    assert "进度：5分钟正式点确认→1分钟区间套定位补充" in rendered
    assert "时效：日期 2026-07-20｜1分钟定位确认 10:01:00" in rendered
    assert "监听发现 10:01:30（延迟 30秒）" in rendered
    assert "结论：可人工复核买入；1分钟定位窗口有效" in rendered
    assert "状态：1分钟区间套已确认，精确执行候选已解锁" in rendered
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
    current["segment_difference_1m"] = {
        **current["segment_difference_1m"],
        "point_id": "trigger:rearmed-1m-1buy",
        "anchor_at": "2026-07-20T10:00:00+08:00",
        "available_at": "2026-07-20T10:02:00+08:00",
        "confirmed_at": "2026-07-20T10:02:00+08:00",
    }
    current["entry_execution_boundary"] = {
        "confirmation_bar_closed_at": "2026-07-20T10:02:00+08:00",
        "raw_high": "10.30",
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
    assert "1分钟精确定位新出现" in sender.messages[1][0]
    assert "1分钟定位确认 10:02:00" in "\n".join(
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
    rebuilt["segment_difference_1m"] = {
        **rebuilt["segment_difference_1m"],
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


def test_late_segment_enrichment_is_not_suppressed_by_parent_setup_age(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "stale-segment-enrichment.json",
    )
    current = signal_document("triggered")
    current["observed_at"] = "2026-07-20T10:11:01+08:00"
    current["entry_execution_boundary"] = {
        **current["entry_execution_boundary"],
        "entry_valid_until": "2026-07-20T10:12:00+08:00",
    }

    dispatcher.dispatch_changes(
        {"signals": [_without_one_minute_segment(current)]},
        {"signals": [current]},
    )

    assert len(sender.messages) == 1
    assert dispatcher.health_snapshot()["last_suppressed_reason"] is None


def test_expired_segment_enrichment_is_not_a_realtime_notification(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "expired-segment-enrichment.json",
    )
    current = signal_document("triggered")
    current["entry_execution_boundary"] = {
        **current["entry_execution_boundary"],
        "entry_valid_until": "2026-07-20T10:01:15+08:00",
    }

    dispatcher.dispatch_changes(
        {"signals": [_without_one_minute_segment(current)]},
        {"signals": [current]},
    )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ONE_MINUTE_SEGMENT_EVIDENCE_EXPIRED"
    )


def test_executable_without_current_permission_is_suppressed(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "blocked-executable.json",
    )
    current = signal_document("executable")
    current["entry_allowed"] = False
    current["exit_allowed"] = False

    dispatcher.dispatch_changes(
        {"signals": [signal_document("triggered")]},
        {"signals": [current]},
    )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "CURRENT_EXECUTION_PERMISSION_MISSING"
    )


def test_executable_buy_requires_safe_delivery_margin(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "executable-delivery-margin.json",
    )
    current = signal_document("executable")
    current["entry_execution_boundary"] = {
        **current["entry_execution_boundary"],
        "entry_valid_until": "2026-07-20T10:01:50+08:00",
    }

    dispatcher.dispatch_changes(
        {"signals": [signal_document("triggered")]},
        {"signals": [current]},
    )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ONE_MINUTE_SEGMENT_DELIVERY_MARGIN_INSUFFICIENT"
    )


def test_executable_margin_is_rechecked_after_realtime_quote(
    tmp_path: Path,
) -> None:
    now = [TEST_NOW]
    quote_calls: list[str] = []

    def slow_quote(code: str) -> dict[str, object]:
        quote_calls.append(code)
        now[0] += timedelta(seconds=6)
        return {"last": 10.20}

    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "post-quote-delivery-margin.json",
        clock=lambda: now[0],
        quote_provider=slow_quote,
    )

    dispatcher.dispatch_changes(
        {"signals": [signal_document("triggered")]},
        {"signals": [signal_document("executable")]},
    )

    assert quote_calls == ["SZ.000001"]
    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ONE_MINUTE_SEGMENT_DELIVERY_MARGIN_INSUFFICIENT"
    )


def test_executable_margin_is_rechecked_after_review_persistence(
    tmp_path: Path,
) -> None:
    now = [TEST_NOW]

    class SlowReviewInbox:
        def __init__(self) -> None:
            self.events: list[object] = []

        def record(self, event: object) -> None:
            now[0] += timedelta(seconds=6)
            self.events.append(event)

    inbox = SlowReviewInbox()
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "post-review-delivery-margin.json",
        clock=lambda: now[0],
        review_inbox=inbox,
    )

    dispatcher.dispatch_changes(
        {"signals": [signal_document("triggered")]},
        {"signals": [signal_document("executable")]},
    )

    assert sender.messages == []
    assert len(inbox.events) == 2
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ONE_MINUTE_SEGMENT_DELIVERY_MARGIN_INSUFFICIENT"
    )


def test_executable_precision_alert_uses_one_minute_chart_and_real_deadline(
    tmp_path: Path,
) -> None:
    sender = RichRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "executable-precision-context.json",
    )
    current = signal_document("executable")

    dispatcher.dispatch_changes(
        {"signals": [signal_document("triggered")]},
        {"signals": [current]},
    )

    assert len(sender.rich_messages) == 1
    title, _lines, context = sender.rich_messages[0]
    assert "1分钟精确执行条件满足" in title
    assert context["expires_at"] == "2026-07-20T10:02:00+08:00"
    assert context["minimum_delivery_margin_seconds"] == 10
    assert context["charts"][0]["frequency"] == "1m"
    assert context["charts"][0]["evidence_id"] == (
        "trigger:stable-1m-1buy"
    )


def test_stale_sell_precision_locator_is_not_replayed(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "stale-sell-precision.json",
    )
    current = signal_document("executable")
    current.update({"side": "sell", "entry_allowed": False, "exit_allowed": True})
    current["setup_5m"] = {
        **current["setup_5m"],
        "point_type": "3sell",
        "side": "sell",
        "available_at": "2026-07-20T09:58:00+08:00",
        "confirmed_at": "2026-07-20T09:58:00+08:00",
    }
    current["point_type"] = "3sell"
    current["segment_difference_1m"] = {
        **current["segment_difference_1m"],
        "point_type": "1sell",
        "side": "sell",
        "available_at": "2026-07-20T09:59:00+08:00",
        "confirmed_at": "2026-07-20T09:59:00+08:00",
    }
    current.pop("entry_execution_boundary", None)
    previous = {**current, "lifecycle_stage": "triggered"}

    dispatcher.dispatch_changes(
        {"signals": [previous]},
        {"signals": [current]},
    )

    assert sender.messages == []
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ONE_MINUTE_SEGMENT_EVIDENCE_EXPIRED"
    )


def test_sell_precision_ttl_counts_a_share_market_minutes_across_lunch(
    tmp_path: Path,
) -> None:
    now = datetime.fromisoformat("2026-07-20T13:00:30+08:00")
    sender = RichRecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "sell-lunch-market-time.json",
        clock=lambda: now,
    )
    current = signal_document("executable")
    current.update(
        {
            "side": "sell",
            "point_type": "3sell",
            "entry_allowed": False,
            "exit_allowed": True,
            "observed_at": now.isoformat(),
            "monitor_observed_at": now.isoformat(),
        }
    )
    current["setup_5m"] = {
        **current["setup_5m"],
        "point_type": "3sell",
        "side": "sell",
        "confirmed_at": "2026-07-20T11:25:00+08:00",
        "available_at": "2026-07-20T11:25:00+08:00",
    }
    current["segment_difference_1m"] = {
        **current["segment_difference_1m"],
        "point_type": "1sell",
        "side": "sell",
        "confirmed_at": "2026-07-20T11:29:00+08:00",
        "available_at": "2026-07-20T11:29:00+08:00",
    }
    current.pop("entry_execution_boundary", None)

    dispatcher.dispatch_changes(
        {"signals": [{**current, "lifecycle_stage": "triggered"}]},
        {"signals": [current]},
    )

    assert len(sender.rich_messages) == 1
    _title, _lines, context = sender.rich_messages[0]
    assert context["expires_at"] == "2026-07-20T13:01:00+08:00"
    assert context["minimum_delivery_margin_seconds"] == 10


def test_pending_segment_enrichment_is_revalidated_before_retry(
    tmp_path: Path,
) -> None:
    now = [TEST_NOW]
    sender = RecordingNotifier([False, True])
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "pending-segment-expiry.json",
        clock=lambda: now[0],
    )
    current = signal_document("triggered")
    without_segment = _without_one_minute_segment(current)

    dispatcher.dispatch_changes(
        {"signals": [without_segment]},
        {"signals": [current]},
    )
    assert len(sender.messages) == 1
    assert dispatcher.health_snapshot()["pending_trigger_event_count"] == 1

    now[0] = datetime.fromisoformat("2026-07-20T10:02:30+08:00")
    dispatcher.dispatch_changes(
        {"signals": [current]},
        {"signals": [current]},
    )

    assert len(sender.messages) == 1
    assert dispatcher.health_snapshot()["pending_trigger_event_count"] == 0
    assert dispatcher.health_snapshot()["last_suppressed_reason"] == (
        "ONE_MINUTE_SEGMENT_EVIDENCE_EXPIRED"
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
    current["segment_difference_1m"] = {
        **current["segment_difference_1m"],
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
    assert "1分钟区间套一类买点（趋势背驰）" in title
    assert "1分钟区间套定位：一类买点（趋势背驰）" in rendered
    assert "监听发现 10:01:30（延迟 1分30秒）" in rendered
    assert dispatcher.health_snapshot()["last_suppressed_reason"] is None
    persisted = json.loads(
        (tmp_path / "formation-segment-enrichment.json").read_text(encoding="utf-8")
    )
    audit = persisted["event_audit"][-1]
    assert audit["trigger_divergence_kind"] == "trend"
    assert audit["notification_evidence_at"] == "2026-07-20T10:00:00+08:00"


def test_late_segment_does_not_zero_position_from_parent_setup_age(
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
    assert "1分钟精确定位新出现" not in sender.messages[0][0]
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


def test_late_repeat_of_delivered_segment_does_not_pollute_suppression_audit(
    tmp_path: Path,
) -> None:
    now = [TEST_NOW]
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered-segment-repeat.json",
        clock=lambda: now[0],
    )
    current = signal_document("triggered")
    previous = _without_one_minute_segment(current)

    dispatcher.dispatch_changes(
        {"signals": [previous]},
        {"signals": [current]},
    )
    now[0] = TEST_NOW.replace(second=50)
    dispatcher.dispatch_changes(
        {"signals": [previous]},
        {"signals": [current]},
    )

    assert len(sender.messages) == 1
    health = dispatcher.health_snapshot()
    assert health["delivered_event_count"] == 1
    assert health["suppressed_count"] == 0
    assert health["last_suppressed_reason"] is None
    persisted = json.loads(
        (tmp_path / "delivered-segment-repeat.json").read_text(encoding="utf-8")
    )
    assert [row["status"] for row in persisted["event_audit"]] == ["delivered"]


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
    assert "1分钟精确定位新出现" in sender.messages[1][0]
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
    assert "1分钟精确定位新出现" not in sender.messages[1][0]
    assert "1分钟区间套定位：一类买点" in "\n".join(sender.messages[1][1])
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
        # Availability is causal metadata from this reconstruction, not the
        # physical point occurrence identity.
        "available_at": "2026-07-20T10:00:15+08:00",
        "confirmed_at": "2026-07-20T10:00:15+08:00",
    }
    second["segment_difference_1m"] = {
        **second["segment_difference_1m"],
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


def test_v1_trigger_ledger_migrates_without_replaying_rebuilt_point(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "legacy-trigger-ledger.json"
    first = shared_trigger_signal("signal:first-lane", "1buy")
    first["setup_5m"] = {
        **first["setup_5m"],
        "terminal_segment_end_at": first["setup_5m"]["anchor_at"],
    }
    first_sender = RecordingNotifier()
    SignalNotificationDispatcher(
        first_sender,
        state_path=state_path,
    ).dispatch_changes(
        {"signals": [{**first, "lifecycle_stage": "armed"}]},
        {"signals": [first]},
    )
    legacy_id = _legacy_trigger_occurrence_event_id(first, "triggered")
    assert legacy_id is not None
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted["delivered_event_ids"] = [legacy_id]
    persisted["last_success_event_id"] = legacy_id
    persisted["event_audit"][-1]["event_id"] = legacy_id
    state_path.write_text(json.dumps(persisted), encoding="utf-8")

    rebuilt = shared_trigger_signal("signal:rebuilt-lane", "1buy")
    rebuilt["point_id"] = "setup:rebuilt"
    rebuilt["setup_5m"] = {
        **rebuilt["setup_5m"],
        "point_id": "setup:rebuilt",
        "available_at": "2026-07-20T10:00:15+08:00",
        "confirmed_at": "2026-07-20T10:00:15+08:00",
        "terminal_segment_end_at": rebuilt["setup_5m"]["anchor_at"],
    }
    second_sender = RecordingNotifier()
    SignalNotificationDispatcher(
        second_sender,
        state_path=state_path,
    ).dispatch_changes(
        {"signals": [{**rebuilt, "lifecycle_stage": "armed"}]},
        {"signals": [rebuilt]},
    )

    assert first_sender.messages
    assert second_sender.messages == []
    migrated = json.loads(state_path.read_text(encoding="utf-8"))
    assert legacy_id not in migrated["delivered_event_ids"]
    assert len(migrated["delivered_event_ids"]) == 1


def test_distinct_one_minute_triggers_are_not_coalesced(tmp_path: Path) -> None:
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "delivered.json",
    )
    first = shared_trigger_signal("signal:first-trigger", "1buy")
    second = shared_trigger_signal("signal:second-trigger", "2buy")
    second["segment_difference_1m"] = {
        **second["segment_difference_1m"],
        "point_id": "trigger:distinct-1m-1buy",
        "available_at": "2026-07-20T10:02:00+08:00",
        "confirmed_at": "2026-07-20T10:02:00+08:00",
    }
    second["observed_at"] = "2026-07-20T10:02:30+08:00"
    second["entry_execution_boundary"] = {
        "confirmation_bar_closed_at": "2026-07-20T10:02:00+08:00",
        "raw_high": "10.30",
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
    second["segment_difference_1m"] = {
        **second["segment_difference_1m"],
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


def test_delayed_armed_transition_remains_notifiable_while_structure_is_current(
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

    assert len(sender.messages) == 1
    assert dispatcher.health_snapshot()["last_suppressed_reason"] is None


def test_late_trigger_first_seen_without_prior_tracking_remains_notifiable(
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

    assert len(sender.messages) == 1
    assert dispatcher.health_snapshot()["last_suppressed_reason"] is None


def test_late_detection_keeps_current_setup_waiting_for_one_minute_locator(
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
    delayed = _without_one_minute_segment(signal_document("triggered"))
    delayed["observed_at"] = "2026-07-20T10:10:00+08:00"

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [delayed]})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    rendered = "\n".join(lines)
    assert title.startswith("买卖通知｜买点确认·延迟发现｜")
    assert "结构模型比例上限" not in rendered
    assert "风险参考：暂不计算" in rendered
    assert "等待同向1分钟区间套" in rendered
    assert "监听发现 10:10:51（延迟 10分51秒）" in rendered
    assert "发现时效" not in rendered


def test_late_sell_confirmation_is_not_presented_as_a_new_sell_point(
    tmp_path: Path,
) -> None:
    now = datetime(
        2026,
        7,
        20,
        10,
        20,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    sender = RecordingNotifier()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "late-sell-confirmation.json",
        clock=lambda: now,
    )
    delayed = _without_one_minute_segment(signal_document("triggered"))
    delayed.update(
        {
            "side": "sell",
            "point_type": "3sell",
            "entry_allowed": False,
            "exit_allowed": False,
            "observed_at": now.isoformat(),
        }
    )
    delayed["setup_5m"] = {
        **delayed["setup_5m"],
        "point_type": "3sell",
        "side": "sell",
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [delayed]})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert "卖点确认·延迟发现" in title
    assert "新卖点" not in title
    assert "监听发现 10:20:00（延迟 20分钟）" in "\n".join(lines)


def test_late_detection_starts_a_new_transport_retry_window(
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
    delayed = _without_one_minute_segment(signal_document("triggered"))
    delayed["observed_at"] = "2026-07-20T10:10:00+08:00"

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [delayed]})

    assert len(sender.messages) == 1
    assert quote_calls == ["SZ.000001"]
    assert dispatcher.health_snapshot()["last_suppressed_reason"] is None


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
    signal["segment_difference_1m"] = {
        **signal["segment_difference_1m"],
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
    signal["segment_difference_1m"] = {
        **signal["segment_difference_1m"],
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
    assert "1分钟区间套定位：三类买点" in "\n".join(sender.messages[0][1])


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
        "raw_high": "10.30",
        "entry_valid_until": "2026-07-20T10:01:15+08:00",
    }

    dispatcher.dispatch_changes(snapshot("armed"), {"signals": [expired]})

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    assert "5分钟三类买点" in title
    assert "段差已定位" not in title
    assert "1分钟区间套定位：一类买点历史已确认（窗口已过）" in "\n".join(lines)


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
    assert "本次执行比例 0%（旧1分钟定位窗口已过）" in rendered
    assert "不追价，等待新的1分钟区间套" in rendered


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
            "segment_difference_1m": {
                **sell["segment_difference_1m"],
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
            "segment_difference_1m": {
                **sell["segment_difference_1m"],
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
    rendered = "\n".join(sender.messages[0][1])
    assert "失效：5分钟失效价 9.80、9.60（跌破买入结构失效）" in rendered
    assert not any(
        text in rendered
        for text in ("距向下失效", "距向上失效", "已跌破失效价", "已突破失效价")
    )


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
        ("armed", "等待操作确认"),
        ("triggered", "5分钟正式点确认"),
        ("executable", "当前精确执行条件满足"),
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
    assert f"{label}→{label}" in lines[1]
    assert f"{stage}→{stage}" not in lines[1]


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
    assert lines[0] == (
        "结论：可人工复核分批买入；三买回抽已确认，"
        "须满足下方全部执行与风险边界"
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
    sell["segment_difference_1m"] = {}

    title, lines = format_notification(
        sell,
        old_stage="armed",
        new_stage="triggered",
    )
    assert title.endswith("5分钟三类卖点")
    assert "失效：5分钟失效价 10.80（突破卖出结构失效）" in "\n".join(lines)
    assert lines[0] == "结论：结构卖出提醒已确认；请核对卖点级别与结构仍然有效"

    _title, invalidated_lines = format_notification(
        sell,
        old_stage="triggered",
        new_stage="invalidated",
    )
    assert invalidated_lines[0] == "结论：取消该结构计划"


def test_sell_notification_explains_level_relation_and_invalidation_rule() -> None:
    sell = signal_document("triggered")
    sell.update(
        {
            "side": "sell",
            "point_type": "1sell",
            "entry_allowed": False,
            "exit_allowed": True,
            "exit_action": "exit_full",
            "setup_5m": {
                **sell["setup_5m"],
                "point_type": "1sell",
                "side": "sell",
                "invalidation_price": "10.80",
            },
            "segment_difference_1m": {
                **sell["segment_difference_1m"],
                "point_type": "1sell",
                "side": "sell",
            },
        }
    )
    recommendation = build_position_recommendation(
        side="sell",
        recommendation="READY",
        risk_multiplier="1",
        context_risk_scale="1",
        entry_price="10.25",
        structural_stop="10.80",
        exit_action="exit_full",
    ).document()
    sell["position_recommendation"] = recommendation
    sell["notification_position_recommendation"] = recommendation

    title, lines = format_notification(
        sell,
        old_stage="armed",
        new_stage="triggered",
    )

    rendered = "\n".join(lines)
    assert "新卖点·退出复核" in title
    assert "同级或更高级别" in lines[0]
    assert "优先按完整退出规则人工复核" in lines[0]
    assert "向上失效" in lines[0]
    assert "1分钟区间套定位：一类卖点已确认（仅精确定位）" in lines[2]
    assert "5分钟失效价 10.80" in lines[4]
    assert "退出规则由卖点与持有结构级别关系决定" in lines[3]
    assert "结构退出比例 100%" in rendered


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
    assert "状态：仅观察，结构风险待核对" in lines[1]
    assert "失效：5分钟失效价 待结构确认（跌破买入结构失效）" in "\n".join(lines)


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
    second["segment_difference_1m"] = {
        **second["segment_difference_1m"],
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


def test_executable_quote_failure_is_not_labeled_as_currently_executable(
    tmp_path: Path,
) -> None:
    sender = RecordingNotifier()

    def unavailable_quote(_code: str):
        raise RuntimeError("quote unavailable")

    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "executable-quote-failure.json",
        quote_provider=unavailable_quote,
    )

    dispatcher.dispatch_changes(snapshot("triggered"), snapshot("executable"))

    assert len(sender.messages) == 1
    title, lines = sender.messages[0]
    rendered = "\n".join(lines)
    assert "1分钟定位·实时价格待核验" in title
    assert "1分钟精确执行条件满足" not in title
    assert "实时价格未取得" in rendered
    assert "当前价格条件" in rendered
    assert "进度：5分钟正式点确认→1分钟定位有效·实时价格待核验" in rendered


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


def test_failed_send_expires_after_transport_retry_ttl(
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
        31,
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
