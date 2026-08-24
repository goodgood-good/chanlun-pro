from __future__ import annotations

from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from cl_app.services.realtime_review_inbox import (
    EVENT_SCHEMA,
    SCHEMA,
    RealtimeReviewInbox,
    a_share_notification_event,
    monitor_notification_event,
    segment_difference_boundary_status,
    segment_difference_evidence_status,
)


CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 15, 10, 30, tzinfo=CN)


def _a_share_event(*, event_id: str = "event:a", status: str = "pending"):
    return a_share_notification_event(
        event_id=event_id,
        document={
            "code": "SH.688132",
            "name": "测试股票",
            "side": "buy",
            "market": "a",
            "observed_at": "2026-08-15T10:29:30+08:00",
            "current_price": 18.88,
            "selection_sources": ["QMT_SECTOR_TRIGGER"],
            "context_30m": {"direction": "up"},
            "setup_5m": {
                "point_id": "point:688132:5m:3buy",
                "point_type": "3buy",
                "recursive_level": 0,
                "anchor_at": "2026-08-15T10:20:00+08:00",
                "confirmed_at": "2026-08-15T10:25:00+08:00",
                "available_at": "2026-08-15T10:25:00+08:00",
                "lock_state": "pending",
                "anchor_price": "18.80",
                "invalidation_price": "18.20",
            },
            "position_recommendation": {
                "side": "buy",
                "status": "UNRESOLVED",
                "basis": "ACCOUNT_EQUITY",
                "recommended_ratio": None,
                "recommended_percent": None,
                "label": "规范结构仓位",
                "reason_codes": ["CANONICAL_REVIEW"],
                "conditional_options": [],
                "manual_confirmation_required": True,
                "automated_order_authorized": False,
            },
            "notification_position_recommendation": {
                "side": "buy",
                "status": "BLOCKED",
                "basis": "NO_TRADE",
                "recommended_ratio": "0",
                "recommended_percent": "0",
                "label": "按当前价重算结构风险参考",
                "reason_codes": ["CURRENT_PRICE_REVIEW"],
                "conditional_options": [],
                "manual_confirmation_required": True,
                "automated_order_authorized": False,
            },
            "segment_difference_1m": {
                "point_id": "point:688132:3buy",
                "point_type": "1buy",
                "confirmed_at": "2026-08-15T10:27:00+08:00",
                "available_at": "2026-08-15T10:29:00+08:00",
                "recursive_level": 0,
                "anchor_at": "2026-08-15T10:26:00+08:00",
                "divergence_kind": "trend",
            },
        },
        old_stage="armed",
        new_stage="triggered",
        delivery_status=status,
        recorded_at=NOW,
    )


def test_a_share_projection_is_review_only_and_has_four_chart_periods():
    event = _a_share_event()

    assert event["schema"] == EVENT_SCHEMA
    assert event["notification_id"].startswith("sha256:")
    assert event["market"] == "a"
    assert event["point_type"] == "3buy"
    assert event["signal_time"] == "2026-08-15T10:25:00+08:00"
    assert event["structure_anchor_time"] == "2026-08-15T10:20:00+08:00"
    assert event["structure_confirmed_at"] == "2026-08-15T10:25:00+08:00"
    assert event["setup_lock_state"] == "pending"
    assert event["signal_available_at"] == "2026-08-15T10:25:00+08:00"
    assert event["detected_at"] == "2026-08-15T10:29:30+08:00"
    assert event["delivery_updated_at"] == NOW.isoformat()
    assert event["delivered_at"] is None
    assert event["current_price"] == 18.88
    assert event["reference_price"] == "18.80"
    assert event["position_recommendation"]["label"] == "按当前价重算结构风险参考"
    assert event["position_recommendation"]["recommended_percent"] == "0"
    assert event["current_price_source"] == "latest_completed_1m_close"
    assert event["source_frequency"] == "5m"
    assert event["trade_frequency"] == "5m"
    assert event["segment_difference_frequency"] == "1m"
    assert event["segment_difference_present"] is True
    assert event["segment_difference_status"] == "current"
    assert event["segment_difference_current"] is True
    assert event["segment_difference_evidence_status"] == "present"
    assert event["segment_difference_boundary_status"] == "current"
    assert event["segment_difference_divergence_kind"] == "trend"
    assert event["segment_difference_valid_until"] is None
    assert event["signal_qualification"] == (
        "30m_context_5m_trade_signal_1m_segment_optional"
    )
    assert set(event["chart_urls"]) == {"d", "30m", "5m", "1m"}
    assert all("code=SH.688132" in value for value in event["chart_urls"].values())
    assert event["review_required"] is True
    assert event["automated_action_authorized"] is False
    assert event["real_order_transport_enabled"] is False
    assert event["live_status"] == "LIVE_DISABLED"


def test_cross_market_projection_preserves_us_identity_and_source_frequency():
    recorded_at = datetime(2026, 8, 15, 22, 26, tzinfo=CN)
    event = monitor_notification_event(
        market="us",
        event=SimpleNamespace(
            code="QCOM.US",
            name="高通",
            side="sell",
            bs_type="3sell",
            signal_time="2026-08-15T10:20:00-04:00",
            confirmed_time="2026-08-15T10:20:00-04:00",
            detected_time="2026-08-15T10:25:35-04:00",
            price=145.2,
            big_dir="down",
            mid_dir="down",
            op_level="5m",
            mid_level="1m",
            evidence_id="point:qcom:5m:3sell",
            recursive_level=0,
            anchor_time="2026-08-15T10:15:00-04:00",
            setup_bs_type="3sell",
            setup_evidence_id="point:qcom:5m:3sell",
            setup_recursive_level=0,
            setup_anchor_time="2026-08-15T10:15:00-04:00",
            setup_confirmed_time="2026-08-15T10:20:00-04:00",
            setup_available_time="2026-08-15T10:20:00-04:00",
            structure_anchor_price=144.8,
            is_holding=True,
            delivery_identity="delivery:qcom:1sell",
        ),
        delivery_status="delivered",
        recorded_at=recorded_at,
    )

    assert event["market"] == "us"
    assert event["point_type"] == "3sell"
    assert event["trigger_point_type"] is None
    assert event["source_frequency"] == "5m"
    assert event["trade_frequency"] == "5m"
    assert event["segment_difference_present"] is False
    assert event["segment_difference_status"] == "absent"
    assert event["segment_difference_current"] is False
    assert event["selection_sources"] == ["MANUAL_ATTENTION_MONITOR"]
    assert event["is_manual_attention"] is True
    assert "is_holding" not in event
    assert event["delivery_status"] == "delivered"
    assert event["current_price"] == 145.2
    assert event["reference_price"] == 144.8
    assert event["current_price_source"] == "latest_completed_1m_close"
    assert event["signal_qualification"] == (
        "confirmed_5m_trade_signal_with_optional_1m_segment"
    )
    assert event["evidence_id"] == "point:qcom:5m:3sell"
    assert event["structure_anchor_time"] == "2026-08-15T22:15:00+08:00"
    assert event["structure_confirmed_at"] == "2026-08-15T22:20:00+08:00"
    assert event["setup_lock_state"] == "unknown"
    assert event["signal_available_at"] == "2026-08-15T22:20:00+08:00"
    assert event["detected_at"] == "2026-08-15T22:25:35+08:00"
    assert event["delivered_at"] == recorded_at.isoformat()


def test_review_projection_rejects_recursive_30m_context_as_5m_trade_signal():
    document = {
        "code": "SH.600113",
        "side": "sell",
        "market": "a",
        "observed_at": NOW.isoformat(),
        "setup_5m": {
            "point_id": "point:600113:5m:l1:3sell",
            "point_type": "3sell",
            "recursive_level": 1,
            "available_at": NOW.isoformat(),
        },
    }

    with pytest.raises(ValueError, match="physical 5m level L0"):
        a_share_notification_event(
            event_id="event:600113:l1:3sell",
            document=document,
            old_stage="armed",
            new_stage="triggered",
            delivery_status="pending",
            recorded_at=NOW,
        )

    event = SimpleNamespace(
        code="SH.600113",
        name="浙江东日",
        side="sell",
        bs_type="3sell",
        signal_time=NOW.isoformat(),
        confirmed_time=NOW.isoformat(),
        detected_time=NOW.isoformat(),
        price=32.0,
        op_level="5m",
        evidence_id="point:600113:5m:l1:3sell",
        recursive_level=1,
        delivery_identity="event:600113:l1:3sell",
    )
    with pytest.raises(ValueError, match="5m trade evidence is incomplete"):
        monitor_notification_event(
            market="a",
            event=event,
            delivery_status="pending",
            recorded_at=NOW,
        )


def test_cross_market_projection_carries_optional_one_minute_segment() -> None:
    event = monitor_notification_event(
        market="us",
        event=SimpleNamespace(
            code="QCOM.US",
            name="高通",
            side="buy",
            bs_type="2buy",
            signal_time="2026-08-15T10:20:00-04:00",
            confirmed_time="2026-08-15T10:20:00-04:00",
            detected_time="2026-08-15T10:20:15-04:00",
            price=145.2,
            big_dir="up",
            op_level="5m",
            mid_level="1m",
            evidence_id="point:qcom:5m:2buy",
            recursive_level=0,
            anchor_time="2026-08-15T10:15:00-04:00",
            structure_anchor_price=144.8,
            structure_invalidation_price=143.0,
            position_recommendation={
                "side": "buy",
                "status": "RECOMMENDED",
                "basis": "ACCOUNT_EQUITY_UPPER_BOUND",
                "label": "建议买入比例：按5分钟结构锚点测算，账户权益的 4% 以内",
                "reason_codes": ["STRUCTURAL_RISK_BUDGET_SIZED"],
                "manual_confirmation_required": True,
                "automated_order_authorized": False,
            },
            segment_difference_point_type="1buy",
            segment_difference_evidence_id="point:qcom:1m:1buy",
            segment_difference_recursive_level=0,
            segment_difference_anchor_time="2026-08-15T10:17:00-04:00",
            segment_difference_confirmed_time="2026-08-15T10:18:00-04:00",
            segment_difference_available_time="2026-08-15T10:19:00-04:00",
            segment_difference_divergence_kind="trend",
            is_holding=False,
            delivery_identity="delivery:qcom:2buy",
        ),
        delivery_status="pending",
        recorded_at=NOW,
    )

    assert event["source_frequency"] == "5m"
    assert event["segment_difference_present"] is True
    assert event["segment_difference_status"] == "unavailable"
    assert event["segment_difference_current"] is False
    assert event["segment_difference_evidence_status"] == "present"
    assert event["segment_difference_boundary_status"] == "unavailable"
    assert event["segment_difference_point_type"] == "1buy"
    assert event["segment_difference_evidence_id"] == "point:qcom:1m:1buy"
    assert event["segment_difference_recursive_level"] == 0
    assert event["segment_difference_available_at"] == ("2026-08-15T22:19:00+08:00")
    assert event["segment_difference_divergence_kind"] == "trend"


def test_cross_market_segment_enrichment_preserves_parent_and_event_times(
    tmp_path,
) -> None:
    recorded_at = datetime(2026, 8, 15, 22, 20, 30, tzinfo=CN)
    event = monitor_notification_event(
        market="us",
        event=SimpleNamespace(
            code="QCOM.US",
            name="高通",
            side="buy",
            kind="strict_segment_difference_update",
            signal_role="SEGMENT_DIFFERENCE_1M",
            bs_type="2buy",
            signal_time="2026-08-15T22:20:00+08:00",
            confirmed_time="2026-08-15T22:10:00+08:00",
            detected_time="2026-08-15T22:20:15+08:00",
            price=145.2,
            big_dir="up",
            op_level="5m",
            mid_level="1m",
            evidence_id="point:qcom:5m:2buy",
            recursive_level=0,
            anchor_time="2026-08-15T22:00:00+08:00",
            setup_bs_type="2buy",
            setup_evidence_id="point:qcom:5m:2buy",
            setup_recursive_level=0,
            setup_anchor_time="2026-08-15T22:00:00+08:00",
            setup_confirmed_time="2026-08-15T22:10:00+08:00",
            setup_available_time="2026-08-15T22:10:00+08:00",
            structure_anchor_price=144.8,
            structure_invalidation_price=143.0,
            position_recommendation={
                "side": "buy",
                "status": "UNRESOLVED",
                "basis": "STRUCTURAL_RISK_INPUT_UNRESOLVED",
                "recommended_ratio": None,
                "recommended_percent": None,
                "label": "结构风险参考待人工核对",
                "reason_codes": ["STRUCTURAL_RISK_INPUT_UNRESOLVED"],
                "conditional_options": [],
                "manual_confirmation_required": True,
                "automated_order_authorized": False,
            },
            segment_difference_point_type="1buy",
            segment_difference_evidence_id="point:qcom:1m:1buy",
            segment_difference_recursive_level=0,
            segment_difference_anchor_time="2026-08-15T22:18:00+08:00",
            segment_difference_confirmed_time="2026-08-15T22:19:00+08:00",
            segment_difference_available_time="2026-08-15T22:20:00+08:00",
            delivery_identity="strict_segment_difference_update:qcom",
        ),
        delivery_status="pending",
        recorded_at=recorded_at,
    )

    assert event["old_stage"] == "triggered"
    assert event["new_stage"] == "segment_enriched"
    assert event["signal_time"] == "2026-08-15T22:20:00+08:00"
    assert event["signal_available_at"] == "2026-08-15T22:10:00+08:00"
    assert event["structure_confirmed_at"] == "2026-08-15T22:10:00+08:00"
    assert event["segment_difference_valid_until"] is None
    assert event["segment_difference_boundary_status"] == "unavailable"
    assert event["segment_difference_status"] == "unavailable"
    assert event["signal_qualification"] == (
        "confirmed_5m_trade_signal_with_new_1m_segment_enrichment"
    )
    inbox = RealtimeReviewInbox(tmp_path / "segment-enrichment.json")
    inbox.record(event)
    assert inbox.snapshot()["events"][0]["new_stage"] == "segment_enriched"


def test_expired_a_share_segment_is_preserved_as_audit_evidence_not_current():
    document = {
        "code": "SH.601231",
        "name": "环旭电子",
        "side": "buy",
        "market": "a",
        "observed_at": "2026-08-17T13:57:22+08:00",
        "decision_reasons": ["ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"],
        "setup_5m": {
            "point_id": "point:601231:5m:1buy",
            "point_type": "1buy",
            "anchor_at": "2026-07-30T13:05:00+08:00",
            "confirmed_at": "2026-08-17T13:55:00+08:00",
            "available_at": "2026-08-17T13:55:00+08:00",
        },
        "segment_difference_1m": {
            "point_id": "point:601231:1m:2buy",
            "point_type": "2buy",
            "anchor_at": "2026-07-30T13:04:00+08:00",
            "confirmed_at": "2026-08-03T11:12:00+08:00",
            "available_at": "2026-08-03T11:12:00+08:00",
            "recursive_level": 0,
        },
        "entry_execution_boundary": {
            "entry_valid_until": "2026-08-03T11:13:00+08:00",
        },
    }

    event = a_share_notification_event(
        event_id="event:expired-segment",
        document=document,
        old_stage="armed",
        new_stage="triggered",
        delivery_status="delivered",
        recorded_at=datetime(2026, 8, 17, 13, 57, 34, tzinfo=CN),
    )

    assert event["segment_difference_present"] is True
    assert event["segment_difference_status"] == "expired"
    assert event["segment_difference_current"] is False
    assert event["segment_difference_evidence_status"] == "present"
    assert event["segment_difference_boundary_status"] == "expired"
    assert event["segment_difference_valid_until"] == ("2026-08-03T11:13:00+08:00")
    assert event["segment_difference_point_type"] == "2buy"


def test_segment_evidence_and_buy_entry_boundary_are_independent_axes() -> None:
    sell = {
        "side": "sell",
        "observed_at": "2026-08-17T10:05:00+08:00",
        "segment_difference_1m": {"side": "sell", "point_type": "1sell"},
    }
    expired_buy = {
        "side": "buy",
        "observed_at": "2026-08-17T10:05:00+08:00",
        "segment_difference_1m": {"side": "buy", "point_type": "1buy"},
        "entry_execution_boundary": {
            "entry_valid_until": "2026-08-17T10:04:00+08:00",
        },
    }

    assert segment_difference_evidence_status(sell) == "present"
    assert segment_difference_boundary_status(sell) == "not_applicable"
    assert segment_difference_evidence_status(expired_buy) == "present"
    assert segment_difference_boundary_status(expired_buy) == "expired"


def test_buy_boundary_uses_review_time_instead_of_snapshot_observation_time() -> None:
    buy = {
        "side": "buy",
        "observed_at": "2026-08-17T10:05:30+08:00",
        "segment_difference_1m": {"side": "buy", "point_type": "1buy"},
        "entry_execution_boundary": {
            "entry_valid_until": "2026-08-17T10:06:00+08:00",
        },
    }

    assert segment_difference_boundary_status(buy) == "current"
    assert (
        segment_difference_boundary_status(
            buy,
            evaluated_at="2026-08-17T10:06:30+08:00",
        )
        == "expired"
    )


def test_review_event_expires_boundary_at_recording_time() -> None:
    document = {
        "code": "SH.688132",
        "name": "测试股票",
        "side": "buy",
        "market": "a",
        "observed_at": "2026-08-15T10:29:30+08:00",
        "setup_5m": {
            "point_id": "point:688132:5m:3buy",
            "point_type": "3buy",
            "recursive_level": 0,
            "confirmed_at": "2026-08-15T10:25:00+08:00",
            "available_at": "2026-08-15T10:25:00+08:00",
        },
        "segment_difference_1m": {
            "point_id": "point:688132:1m:1buy",
            "point_type": "1buy",
            "side": "buy",
            "recursive_level": 0,
            "confirmed_at": "2026-08-15T10:29:00+08:00",
            "available_at": "2026-08-15T10:29:00+08:00",
        },
        "entry_execution_boundary": {
            "entry_valid_until": "2026-08-15T10:30:00+08:00",
        },
    }

    event = a_share_notification_event(
        event_id="event:delivery-clock-expired",
        document=document,
        old_stage="triggered",
        new_stage="segment_enriched",
        delivery_status="delivered",
        detected_at="2026-08-15T10:29:30+08:00",
        recorded_at=datetime(2026, 8, 15, 10, 30, 30, tzinfo=CN),
    )

    assert event["segment_difference_status"] == "expired"
    assert event["segment_difference_boundary_status"] == "expired"
    assert event["segment_difference_current"] is False


def test_inbox_persists_updates_delivery_and_never_stores_credentials(tmp_path):
    path = tmp_path / "realtime_review_inbox.json"
    clock_values = iter([NOW + timedelta(minutes=1)])
    inbox = RealtimeReviewInbox(path, clock=lambda: next(clock_values))
    event = _a_share_event()

    inbox.record(event)
    inbox.update_delivery(["event:a"], status="failed", reason="network")

    snapshot = inbox.snapshot()
    assert snapshot["schema"] == SCHEMA
    assert snapshot["event_count"] == 1
    assert snapshot["events"][0]["delivery_status"] == "failed"
    assert snapshot["events"][0]["delivery_reason"] == "network"
    assert snapshot["events"][0]["detected_at"] == ("2026-08-15T10:29:30+08:00")
    assert (
        snapshot["events"][0]["delivery_updated_at"]
        == (NOW + timedelta(minutes=1)).isoformat()
    )
    assert snapshot["events"][0]["delivered_at"] is None
    assert snapshot["automated_order_authorized"] is False
    assert snapshot["real_order_transport_enabled"] is False

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert "webhook" not in json.dumps(persisted).lower()
    assert "access_token" not in json.dumps(persisted).lower()
    reloaded = RealtimeReviewInbox(path)
    assert reloaded.snapshot()["events"] == snapshot["events"]


def test_transport_expiry_preserves_structural_position_recommendation(
    tmp_path,
):
    path = tmp_path / "expired-buy-inbox.json"
    clock_values = iter([NOW + timedelta(minutes=11)])
    inbox = RealtimeReviewInbox(path, clock=lambda: next(clock_values))
    event = _a_share_event(event_id="event:expired-buy")
    event["position_recommendation"] = {
        "side": "buy",
        "status": "RECOMMENDED",
        "basis": "STRUCTURAL_RISK_MODEL_UPPER_BOUND",
        "recommended_ratio": "0.08",
        "recommended_percent": "8.00",
        "label": "结构风险参考比例 8% 以内",
        "reason_codes": ["CURRENT_PRICE_RISK_SIZING"],
        "conditional_options": [],
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }

    inbox.record(event)
    inbox.update_delivery(
        ["event:expired-buy"],
        status="expired",
        reason="SIGNAL_DELIVERY_WINDOW_EXPIRED",
    )

    [projected] = inbox.snapshot()["events"]
    assert projected["delivery_status"] == "expired"
    assert projected["position_recommendation"]["status"] == "RECOMMENDED"
    assert projected["position_recommendation"]["recommended_ratio"] == "0.08"
    assert projected["position_recommendation"]["recommended_percent"] == "8.00"
    assert "position_recommendation_at_detection" not in projected
    assert RealtimeReviewInbox(path).snapshot()["events"] == [projected]


def test_inbox_is_bounded_and_rejects_non_review_events(tmp_path):
    inbox = RealtimeReviewInbox(tmp_path / "inbox.json", max_events=2)
    for index in range(3):
        event = _a_share_event(event_id=f"event:{index}")
        event_time = (NOW + timedelta(minutes=index)).isoformat()
        event["signal_time"] = event_time
        event["signal_available_at"] = event_time
        event["detected_at"] = event_time
        event["recorded_at"] = event_time
        event["delivery_updated_at"] = event_time
        inbox.record(event)

    snapshot = inbox.snapshot()
    assert snapshot["event_count"] == 2
    assert [row["signal_time"] for row in snapshot["events"]] == [
        (NOW + timedelta(minutes=2)).isoformat(),
        (NOW + timedelta(minutes=1)).isoformat(),
    ]

    invalid = _a_share_event(event_id="invalid")
    invalid["automated_action_authorized"] = True
    with pytest.raises(ValueError, match="event is invalid"):
        inbox.record(invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_frequency", "1m"),
        ("segment_difference_point_type", "1sell"),
        ("segment_difference_recursive_level", 1),
        ("segment_difference_divergence_kind", "cycle"),
        ("setup_lock_state", "complete"),
    ),
)
def test_inbox_rejects_wrong_trade_level_or_opposite_segment(
    tmp_path,
    field,
    value,
) -> None:
    inbox = RealtimeReviewInbox(tmp_path / f"invalid-{field}.json")
    invalid = _a_share_event(event_id=f"invalid:{field}")
    invalid[field] = value

    with pytest.raises(ValueError, match="event is invalid"):
        inbox.record(invalid)


def test_inbox_rejects_position_ratio_without_numeric_recommendation(
    tmp_path,
) -> None:
    inbox = RealtimeReviewInbox(tmp_path / "invalid-ratio.json")
    invalid = _a_share_event(event_id="invalid:ratio")
    invalid["position_recommendation"] = {
        "side": "buy",
        "status": "RECOMMENDED",
        "basis": "ACCOUNT_EQUITY_UPPER_BOUND",
        "recommended_ratio": None,
        "recommended_percent": None,
        "label": "建议买入比例：待定",
        "reason_codes": ["MALFORMED_TEST_RATIO"],
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }

    with pytest.raises(ValueError, match="event is invalid"):
        inbox.record(invalid)


def test_legacy_records_are_migrated_without_erasing_notification_history(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-inbox.json"
    legacy = _a_share_event()
    for key in (
        "structure_anchor_time",
        "structure_confirmed_at",
        "signal_available_at",
        "detected_at",
        "delivery_updated_at",
        "delivered_at",
        "setup_lock_state",
        "segment_difference_status",
        "segment_difference_current",
        "segment_difference_evidence_status",
        "segment_difference_boundary_status",
        "segment_difference_valid_until",
    ):
        legacy.pop(key, None)
    path.write_text(
        json.dumps({"schema": SCHEMA, "events": [legacy]}),
        encoding="utf-8",
    )

    [migrated] = RealtimeReviewInbox(path).snapshot()["events"]

    assert migrated["signal_available_at"] == legacy["signal_time"]
    assert migrated["structure_confirmed_at"] == legacy["signal_time"]
    assert migrated["structure_anchor_time"] == "2026-08-15T10:20:00+08:00"
    assert migrated["detected_at"] == legacy["observed_at"]
    assert migrated["delivery_updated_at"] == legacy["recorded_at"]
    assert migrated["delivered_at"] is None
    assert migrated["setup_lock_state"] == "unknown"
    assert migrated["segment_difference_present"] is True
    assert migrated["segment_difference_status"] == "unknown"
    assert migrated["segment_difference_current"] is False
    assert migrated["segment_difference_evidence_status"] == "present"
    assert migrated["segment_difference_boundary_status"] == "unknown"


def test_legacy_account_coupled_guidance_is_rewritten_as_structure_only(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-account-guidance.json"
    legacy_buy = _a_share_event(event_id="event:legacy-account-buy")
    legacy_buy["position_recommendation"] = {
        "side": "buy",
        "status": "RECOMMENDED",
        "basis": "ACCOUNT_EQUITY_UPPER_BOUND",
        "recommended_ratio": "0.1",
        "recommended_percent": "10",
        "label": "建议买入比例：账户权益的 10% 以内，组合热度只可下调",
        "reason_codes": [
            "STRUCTURAL_RISK_BUDGET_SIZED",
            "PORTFOLIO_CAPS_REQUIRE_MANUAL_REVIEW",
        ],
        "conditional_options": [],
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }
    legacy_buy["position_recommendation_at_detection"] = dict(
        legacy_buy["position_recommendation"]
    )
    legacy_sell = _a_share_event(event_id="event:legacy-account-sell")
    legacy_sell["side"] = "sell"
    legacy_sell["point_type"] = "3sell"
    legacy_sell["segment_difference_point_type"] = "1sell"
    legacy_sell["segment_difference_boundary_status"] = "not_applicable"
    legacy_sell["position_recommendation"] = {
        "side": "sell",
        "status": "CONDITIONAL",
        "basis": "CURRENT_POSITION_STRUCTURE_REQUIRED",
        "recommended_ratio": None,
        "recommended_percent": None,
        "label": "须核对当前持仓归属，再决定卖出比例",
        "reason_codes": ["POSITION_STRUCTURE_REQUIRED_FOR_SELL_RATIO"],
        "conditional_options": [
            {
                "condition": "FIVE_MINUTE_SAME_OR_HIGHER_LEVEL_EXIT",
                "recommended_ratio": "1",
                "recommended_percent": "100",
            },
        ],
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }
    path.write_text(
        json.dumps(
            {"schema": SCHEMA, "events": [legacy_buy, legacy_sell]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    events = RealtimeReviewInbox(path).snapshot()["events"]
    by_side = {event["side"]: event["position_recommendation"] for event in events}

    assert by_side["buy"]["status"] == "RECOMMENDED"
    assert by_side["buy"]["basis"] == "STRUCTURAL_RISK_MODEL_UPPER_BOUND"
    assert by_side["buy"]["recommended_percent"] == "10"
    migrated_buy = next(event for event in events if event["side"] == "buy")
    assert migrated_buy["position_recommendation_at_detection"]["basis"] == (
        "STRUCTURAL_RISK_MODEL_UPPER_BOUND"
    )
    assert by_side["sell"]["status"] == "CONDITIONAL"
    assert by_side["sell"]["basis"] == "STRUCTURAL_EXIT_LEVEL_REQUIRED"
    persisted = path.read_text(encoding="utf-8")
    for forbidden in (
        "账户",
        "持仓",
        "仓位",
        "权益",
        "组合热度",
        "ACCOUNT_EQUITY",
        "CURRENT_POSITION_STRUCTURE_REQUIRED",
        "POSITION_STRUCTURE_REQUIRED_FOR_SELL_RATIO",
        "PORTFOLIO_CAPS_REQUIRE_MANUAL_REVIEW",
    ):
        assert forbidden not in persisted


def test_new_account_coupled_guidance_is_rejected_instead_of_silently_stored(
    tmp_path,
) -> None:
    inbox = RealtimeReviewInbox(tmp_path / "new-account-guidance.json")
    event = _a_share_event(event_id="event:new-account-guidance")
    event["position_recommendation"] = {
        "side": "buy",
        "status": "UNRESOLVED",
        "basis": "ACCOUNT_EQUITY",
        "recommended_ratio": None,
        "recommended_percent": None,
        "label": "请结合账户情况人工核对",
        "reason_codes": ["POSITION_RATIO_INPUT_UNRESOLVED"],
        "conditional_options": [],
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }

    with pytest.raises(ValueError, match="event is invalid"):
        inbox.record(event)

    clean = _a_share_event(event_id="event:clean-current-legacy-history")
    clean["position_recommendation_at_detection"] = dict(
        event["position_recommendation"]
    )
    with pytest.raises(ValueError, match="event is invalid"):
        inbox.record(clean)


def test_persisted_one_minute_l1_is_discarded_as_invalid_segment_evidence(
    tmp_path,
) -> None:
    path = tmp_path / "invalid-one-minute-l1.json"
    invalid = _a_share_event()
    invalid["segment_difference_recursive_level"] = 1
    path.write_text(
        json.dumps({"schema": SCHEMA, "events": [invalid]}),
        encoding="utf-8",
    )

    assert RealtimeReviewInbox(path).snapshot()["events"] == []
    assert json.loads(path.read_text(encoding="utf-8"))["events"] == []


def test_delivery_timestamp_is_recorded_only_after_success(tmp_path) -> None:
    delivered_at = NOW + timedelta(seconds=45)
    inbox = RealtimeReviewInbox(
        tmp_path / "delivery.json",
        clock=lambda: delivered_at,
    )
    inbox.record(_a_share_event())

    inbox.update_delivery(["event:a"], status="delivered")

    [event] = inbox.snapshot()["events"]
    assert event["detected_at"] == "2026-08-15T10:29:30+08:00"
    assert event["signal_available_at"] == "2026-08-15T10:25:00+08:00"
    assert event["delivered_at"] == delivered_at.isoformat()
    assert event["delivery_updated_at"] == delivered_at.isoformat()


def test_inbox_drops_persisted_recursive_context_from_current_review(tmp_path) -> None:
    path = tmp_path / "invalid-recursive-context.json"
    invalid = _a_share_event()
    invalid["recursive_level"] = 1
    path.write_text(
        json.dumps({"schema": SCHEMA, "events": [invalid]}),
        encoding="utf-8",
    )

    assert RealtimeReviewInbox(path).snapshot()["events"] == []


def test_legacy_us_detection_uses_first_inbox_record_not_signal_time(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-us.json"
    signal_time = "2026-08-15T22:25:00+08:00"
    first_recorded_at = "2026-08-15T22:25:40+08:00"
    delivered_at = "2026-08-15T22:26:05+08:00"
    legacy = monitor_notification_event(
        market="us",
        event=SimpleNamespace(
            code="QCOM.US",
            name="高通",
            side="sell",
            bs_type="1sell",
            signal_time=signal_time,
            price=145.2,
            big_dir="down",
            mid_dir="down",
            op_level="5m",
            mid_level="1m",
            evidence_id="point:qcom:legacy",
            recursive_level=0,
            anchor_time=signal_time,
            setup_bs_type="1sell",
            setup_evidence_id="point:qcom:5m:legacy",
            setup_recursive_level=0,
            setup_anchor_time=signal_time,
            setup_confirmed_time=signal_time,
            setup_available_time=signal_time,
            structure_anchor_price=144.8,
            is_holding=True,
            delivery_identity="delivery:qcom:legacy",
        ),
        delivery_status="delivered",
        recorded_at=datetime.fromisoformat(delivered_at),
    )
    legacy["observed_at"] = signal_time
    legacy["source"] = "CROSS_MARKET_HOLDING_MONITOR"
    legacy["first_recorded_at"] = first_recorded_at
    for key in (
        "structure_anchor_time",
        "structure_confirmed_at",
        "signal_available_at",
        "detected_at",
        "delivery_updated_at",
        "delivered_at",
    ):
        legacy.pop(key, None)
    path.write_text(
        json.dumps({"schema": SCHEMA, "events": [legacy]}),
        encoding="utf-8",
    )

    [migrated] = RealtimeReviewInbox(path).snapshot()["events"]

    assert migrated["signal_available_at"] == signal_time
    assert migrated["detected_at"] == first_recorded_at
    assert migrated["delivered_at"] == delivered_at
