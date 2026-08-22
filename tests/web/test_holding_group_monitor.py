from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import cl_app.services.holding_group_monitor as monitor_module
from cl_app.services.holding_group_monitor import (
    HoldingGroupMonitorConfig,
    HoldingGroupMonitorService,
    build_non_a_monitor_universe,
)
from cl_app.services.realtime_review_inbox import RealtimeReviewInbox
from chanlun.decision_support.trading_system.strict_realtime_monitor import (
    StrictPhysicalMonitorState,
    collect_strict_monitor_events,
)
from chanlun.decision_support.trading_system.strict_realtime_monitor import (
    StrictRealtimeMonitorEvent as MonitorEvent,
)
from chanlun.decision_support.trading_system.position_recommendation import (
    build_position_recommendation,
)


CN = ZoneInfo("Asia/Shanghai")


class _Notifier:
    available = True

    def __init__(self, results: list[bool] | None = None) -> None:
        self.messages: list[tuple[str, list[str]]] = []
        self.results = list(results or [])

    def send(self, title: str, lines: list[str]) -> bool:
        self.messages.append((title, list(lines)))
        return self.results.pop(0) if self.results else True


class _RichNotifier(_Notifier):
    def __init__(self) -> None:
        super().__init__()
        self.rich_messages = []

    def send_rich(self, title, lines, context) -> bool:
        self.rich_messages.append((title, list(lines), dict(context)))
        return True


class _State:
    def __init__(self, code: str, _exchange, **levels) -> None:
        self.code = code
        self.levels = levels
        self.consecutive_refresh_failures = 0
        self.warmup_ready = True
        self.direction = "down"
        self.signal_time = "2026-08-04 10:00:00"

    def big_dir(self) -> str:
        return self.direction


def _strict_sell(code: str, name: str, signal_time: str) -> MonitorEvent:
    return MonitorEvent(
        code=code,
        name=name,
        side="sell",
        kind="strict_sell_point",
        bs_type="1sell",
        signal_time=signal_time,
        price=10.0,
        big_dir="down",
        reason="5m sell point",
        op_level="5m",
        mid_level="1m",
        big_level="30m",
        evidence_id=f"test:{code}:1sell:{signal_time}",
        recursive_level=0,
        anchor_time=signal_time,
        confirmed_time=signal_time,
        position_recommendation=build_position_recommendation(
            side="sell",
            recommendation="CAUTION",
            risk_multiplier="0",
            context_risk_scale="0.50",
            entry_price="10",
            structural_stop="10.2",
            exit_action="none",
        ).document(),
        setup_bs_type="1sell",
        setup_evidence_id=f"test:{code}:5m:1sell:{signal_time}",
        setup_recursive_level=0,
        setup_anchor_time=signal_time,
        setup_confirmed_time=signal_time,
        setup_available_time=signal_time,
    )


def _segment_sell_update(
    code: str,
    name: str,
    *,
    setup_time: str = "2026-08-04T10:00:00+08:00",
    segment_time: str = "2026-08-04T10:01:00+08:00",
) -> MonitorEvent:
    return MonitorEvent(
        code=code,
        name=name,
        side="sell",
        kind="strict_segment_difference_update",
        bs_type="1sell",
        signal_time=segment_time,
        price=10.0,
        big_dir="down",
        reason="strict_1m_segment_difference_enrichment",
        op_level="5m",
        mid_level="1m",
        big_level="30m",
        evidence_id=f"test:{code}:5m:1sell:{setup_time}",
        recursive_level=0,
        anchor_time=setup_time,
        confirmed_time=setup_time,
        signal_role="SEGMENT_DIFFERENCE_1M",
        position_recommendation=build_position_recommendation(
            side="sell",
            recommendation="CAUTION",
            risk_multiplier="0",
            context_risk_scale="0.50",
            entry_price="10",
            structural_stop="10.2",
            exit_action="none",
        ).document(),
        setup_bs_type="1sell",
        setup_evidence_id=f"test:{code}:5m:1sell:{setup_time}",
        setup_recursive_level=0,
        setup_anchor_time=setup_time,
        setup_confirmed_time=setup_time,
        setup_available_time=setup_time,
        segment_difference_point_type="2sell",
        segment_difference_evidence_id=f"test:{code}:1m:2sell:{segment_time}",
        segment_difference_recursive_level=0,
        segment_difference_anchor_time="2026-08-04T10:00:30+08:00",
        segment_difference_confirmed_time="2026-08-04T10:00:50+08:00",
        segment_difference_available_time=segment_time,
    )


def _event_collector(states, *, names, holdings, **_kwargs):
    assert holdings == set(states)
    return [_strict_sell(code, names[code], states[code].signal_time) for code in states]


def _big_down_collector(states, *, names, holdings, **_kwargs):
    assert holdings == set(states)
    return [
        MonitorEvent(
            code=code,
            name=names[code],
            side="risk",
            kind="strict_30m_context_warning",
            bs_type="",
            signal_time=state.signal_time,
            price=10.0,
            big_dir="down",
            reason="30m turned down",
            op_level="5m",
            mid_level="1m",
            big_level="30m",
            evidence_id=f"big-down:{state.signal_time}",
            signal_role="CONTEXT_WARNING_30M",
        )
        for code, state in states.items()
        if state.big_dir() == "down"
    ]


def _service(
    tmp_path: Path,
    positions,
    *,
    market_open=True,
    notifier: _Notifier | None = None,
    event_collector=_event_collector,
    state_factory=_State,
    clock=None,
    review_inbox=None,
    exchange_factory=None,
):
    notifier = notifier or _Notifier()
    exchange_calls: list[str] = []

    def exchange_provider(market):
        exchange_calls.append(market.value)
        return (
            exchange_factory(market)
            if exchange_factory is not None
            else SimpleNamespace(market=market.value)
        )

    service = HoldingGroupMonitorService(
        positions_provider=lambda: list(positions),
        notifier=notifier,
        state_root=tmp_path,
        exchange_provider=exchange_provider,
        market_open_provider=lambda _exchange, _market, _now: market_open,
        state_factory=state_factory,
        event_collector=event_collector,
        clock=clock or (lambda: datetime(2026, 8, 4, 10, 1, tzinfo=CN)),
        config=HoldingGroupMonitorConfig(max_workers=4),
        review_inbox=review_inbox,
    )
    return service, notifier, exchange_calls


def _register(service):
    class _Scheduler:
        def add_job(self, func, **kwargs):
            return SimpleNamespace(id=kwargs["id"], func=func)

    service.register_job(_Scheduler())


def test_production_defaults_use_strict_physical_timeframe_authority(tmp_path):
    service = HoldingGroupMonitorService(
        positions_provider=lambda: [],
        notifier=None,
        state_root=tmp_path,
    )

    assert service._state_factory is StrictPhysicalMonitorState
    assert service._event_collector is collect_strict_monitor_events


def test_first_slow_warmup_publishes_us_universe_before_completion(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def slow_collector(_states, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return []

    service, _notifier, _exchange_calls = _service(
        tmp_path,
        [
            {"market": "us", "code": "QCOM.US", "name": "高通"},
            {"market": "us", "code": "QQQ.US", "name": "纳指100ETF"},
        ],
        event_collector=slow_collector,
    )
    _register(service)
    worker = threading.Thread(target=service.run_once)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        health = service.health_snapshot()
        assert health["ready"] is False
        assert health["status"] == "warming_up"
        assert health["awaiting_count"] == 2
        assert [row["code"] for row in health["positions"]] == [
            "QCOM.US",
            "QQQ.US",
        ]
        assert all(
            row["status"] == "awaiting_first_run"
            for row in health["positions"]
        )
    finally:
        release.set()
        worker.join(timeout=5)
    assert worker.is_alive() is False


def test_every_non_a_holding_is_routed_to_its_own_market(tmp_path):
    positions = [
        {"market": "us", "code": "TSLA.US", "name": "特斯拉"},
        {"market": "hk", "code": "HK.00700", "name": "腾讯控股"},
    ]
    service, notifier, exchange_calls = _service(tmp_path, positions)

    result = service.run_once()

    assert result["declared_count"] == 2
    assert result["monitored_count"] == 2
    assert result["covered_count"] == 2
    assert result["failed_count"] == 0
    assert sorted(exchange_calls) == ["hk", "us"]
    assert len(notifier.messages) == 2
    for title, lines in notifier.messages:
        assert title.startswith("买卖通知｜卖出复核｜人工关注｜")
        assert title.endswith("5分钟一类卖点")
        assert len(lines) == 1
        assert "5分钟一类卖点" in lines[0]
        assert "2026-08-04 10:00:00" in lines[0]
        assert "时间：操作确认 2026-08-04 10:00:00" in lines[0]
        assert "监听发现 2026-08-04 10:01:00" in lines[0]
        assert "最近1分钟收盘价：10.000" in lines[0]
        assert "同级或更高级别卖点按完整退出规则复核" in lines[0]
        assert "1分钟段差" in lines[0]
        assert "1分钟段差未出现（不阻断5分钟信号）" in lines[0]
        assert "关系未确认前不生成退出比例" in lines[0]
        assert "25%" not in lines[0]
        assert "30分钟向下" in lines[0]
        assert "操作：优先复核卖出或退出条件" in lines[0]
        assert "参考卖出比例" not in lines[0]
        assert "辅助结构线索" not in lines[0]
        assert "time=" not in lines[0]
        assert "sell point" not in lines[0]
    assert {row["code"] for row in result["positions"]} == {
        "TSLA.US",
        "HK.00700",
    }
    assert all(row["status"] == "monitoring" for row in result["positions"])
    assert all(row["op_level"] == "5m" for row in result["positions"])
    assert all(row["mid_level"] == "1m" for row in result["positions"])
    assert all(row["big_level"] == "30m" for row in result["positions"])


def test_same_round_notifications_are_split_into_readable_three_event_batches(
    tmp_path,
) -> None:
    positions = [
        {"market": "us", "code": f"TEST{index}.US", "name": f"测试{index}"}
        for index in range(7)
    ]
    service, notifier, _exchange_calls = _service(tmp_path, positions)

    result = service.run_once()

    assert result["sent_count"] == 7
    assert len(notifier.messages) == 3
    assert [len(lines) for _title, lines in notifier.messages] == [3, 3, 1]
    assert notifier.messages[0][0].endswith("美股｜3条")
    assert notifier.messages[1][0].endswith("美股｜3条")
    assert notifier.messages[2][0].endswith("5分钟一类卖点")
    assert notifier.messages[0][1][0].startswith("[1/3]")
    assert notifier.messages[0][1][-1].startswith("[3/3]")


def test_buy_sell_and_zero_percent_events_never_share_one_message() -> None:
    signal_time = "2026-08-04 10:00:00"
    sell = _strict_sell("SELL.US", "卖出", signal_time)
    buy = MonitorEvent(
        code="BUY.US",
        name="买入",
        side="buy",
        kind="strict_buy_point",
        bs_type="3buy",
        signal_time=signal_time,
        price=10.0,
        big_dir="up",
        reason="5m buy point",
        evidence_id="test:BUY.US:3buy",
        recursive_level=0,
        anchor_time=signal_time,
        confirmed_time=signal_time,
        structure_anchor_price=10.0,
        structure_invalidation_price=9.5,
        position_recommendation=build_position_recommendation(
            side="buy",
            recommendation="READY",
            risk_multiplier="0.75",
            context_risk_scale="1.00",
            entry_price="10",
            structural_stop="9.5",
            exit_action="none",
            structure_anchor_price="10",
        ).document(),
    )
    protected = MonitorEvent(
        code="CHASE.US",
        name="追价保护",
        side="buy",
        kind="strict_buy_point",
        bs_type="3buy",
        signal_time=signal_time,
        price=10.8,
        big_dir="up",
        reason="5m buy point",
        evidence_id="test:CHASE.US:3buy",
        recursive_level=0,
        anchor_time=signal_time,
        confirmed_time=signal_time,
        structure_anchor_price=10.0,
        structure_invalidation_price=9.5,
        position_recommendation=build_position_recommendation(
            side="buy",
            recommendation="READY",
            risk_multiplier="0.75",
            context_risk_scale="1.00",
            entry_price="10.8",
            structural_stop="9.5",
            exit_action="none",
            structure_anchor_price="10",
        ).document(),
    )

    batches = monitor_module._notification_event_batches(
        [buy, protected, _big_down_collector(
            {"RISK.US": _State("RISK.US", None)},
            names={"RISK.US": "风险"},
            holdings={"RISK.US"},
        )[0], sell],
        maximum=3,
    )

    assert [[event.code for event in batch] for batch in batches] == [
        ["SELL.US"],
        ["RISK.US"],
        ["BUY.US"],
        ["CHASE.US"],
    ]


def test_buy_position_copy_is_an_unadjusted_structural_risk_upper_bound() -> None:
    recommendation = build_position_recommendation(
        side="buy",
        recommendation="READY",
        risk_multiplier="0.75",
        context_risk_scale="1.00",
        entry_price="10",
        structural_stop="9.5",
        exit_action="none",
        structure_anchor_price="10",
    ).document()
    line = monitor_module._notification_position_line(
        SimpleNamespace(side="buy", position_recommendation=recommendation)
    )

    assert line == (
        "风险参考：结构模型比例上限 7.5%（模型比较值）"
    )
    assert "只可下调" not in line


def test_recursive_us_notification_separates_every_event_time() -> None:
    event = MonitorEvent(
        code="QQQ.US",
        name="纳指100ETF",
        side="buy",
        kind="strict_buy_point",
        bs_type="3buy",
        signal_time="2026-08-15T02:13:00+08:00",
        price=581.25,
        big_dir="up",
        reason="strict_confirmed_3buy",
        evidence_id="point:qqq:l1:3buy",
        recursive_level=0,
        anchor_time="2026-08-12T01:15:00+08:00",
        confirmed_time="2026-08-14T22:45:00+08:00",
        position_recommendation=build_position_recommendation(
            side="buy",
            recommendation="READY",
            risk_multiplier="0.75",
            context_risk_scale="1.00",
            entry_price="581.25",
            structural_stop="570",
            exit_action="none",
        ).document(),
        setup_bs_type="3buy",
        setup_evidence_id="point:qqq:5m:l0:3buy",
        setup_recursive_level=0,
        setup_anchor_time="2026-08-12T01:00:00+08:00",
        setup_confirmed_time="2026-08-14T22:40:00+08:00",
        setup_available_time="2026-08-14T22:40:00+08:00",
        segment_difference_point_type="1buy",
        segment_difference_evidence_id="point:qqq:1m:l0:1buy",
        segment_difference_recursive_level=0,
        segment_difference_anchor_time="2026-08-15T02:10:00+08:00",
        segment_difference_confirmed_time="2026-08-15T02:11:00+08:00",
        segment_difference_available_time="2026-08-15T02:12:00+08:00",
        segment_difference_divergence_kind="trend",
    )

    line = monitor_module._notification_line(
        event,
        detected_at=datetime(2026, 8, 15, 2, 14, 5, tzinfo=CN),
    )

    assert "5分钟三类买点（L0）" in line
    assert "操作确认 2026-08-14 22:45:00" in line
    assert "信号可用 2026-08-15 02:13:00" in line
    assert "监听发现 2026-08-15 02:14:05" in line
    assert "最近1分钟收盘价：581.250" in line
    assert "1分钟段差一类买点（趋势背驰）（L0）" in line


def test_notification_labels_five_minute_price_fallback_honestly() -> None:
    event = _strict_sell("QCOM.US", "高通", "2026-08-15T22:20:00+08:00")
    event.price_source = "latest_completed_5m_close"

    line = monitor_module._notification_line(event)

    assert "最近5分钟收盘价：10.000" in line
    assert "最近1分钟收盘价" not in line


def test_segment_enrichment_notification_is_distinct_and_uses_one_minute_chart(
    tmp_path,
) -> None:
    event = _segment_sell_update("TSLA.US", "特斯拉")
    line = monitor_module._notification_line(
        event,
        detected_at="2026-08-04T10:01:20+08:00",
    )
    title = monitor_module._notification_title("us", [event])
    service, _notifier, _exchange_calls = _service(tmp_path, [])
    payload = service._event_notification_payload("us", [event])

    assert "1分钟卖出段差补充" in title
    assert "5分钟一类卖点＋1分钟二类卖点" in title
    assert "状态：1分钟段差新出现，仅补充定位" in line
    assert "5分钟操作确认 2026-08-04 10:00:00" in line
    assert "1分钟段差可用 2026-08-04 10:01:00" in line
    assert "只有当前有效时才进入精确执行候选" in line
    assert monitor_module._notification_bucket(event) == "segment_sell"
    assert monitor_module._delivery_identity(event) == event.delivery_identity
    assert payload["review_events"][0]["new_stage"] == "segment_enriched"
    assert payload["charts"][0]["frequency"] == "1m"
    assert payload["charts"][0]["point_type"] == "2sell"
    assert payload["charts"][0]["evidence_id"] == (
        event.segment_difference_evidence_id
    )


def test_segment_enrichment_uses_later_confluence_for_delay_and_expiry(
    tmp_path,
) -> None:
    earlier_segment = _segment_sell_update(
        "TSLA.US",
        "特斯拉",
        setup_time="2026-08-04T10:00:00+08:00",
        segment_time="2026-08-04T09:40:00+08:00",
    )
    line = monitor_module._notification_line(
        earlier_segment,
        detected_at="2026-08-04T10:01:30+08:00",
    )

    assert "监听发现 2026-08-04 10:01:30（延迟 1分30秒）" in line

    now = [datetime(2026, 8, 4, 10, 1, tzinfo=CN)]
    service, _notifier, _exchange_calls = _service(
        tmp_path,
        [],
        clock=lambda: now[0],
    )
    later_segment = _segment_sell_update(
        "TSLA.US",
        "特斯拉",
        setup_time="2026-08-04T09:40:00+08:00",
        segment_time="2026-08-04T10:00:00+08:00",
    )
    payload = service._event_notification_payload("us", [later_segment])

    assert service._pending_notification_expired(payload) is False
    now[0] = datetime(2026, 8, 4, 10, 10, 1, tzinfo=CN)
    assert service._pending_notification_expired(payload) is True


def test_service_preserves_five_minute_price_fallback_when_one_minute_is_down(
    tmp_path,
) -> None:
    def collector(states, *, names, holdings, **_kwargs):
        events = _event_collector(states, names=names, holdings=holdings)
        for event in events:
            event.price_source = "latest_completed_5m_close"
        return events

    inbox = RealtimeReviewInbox(tmp_path / "fallback-review.json")
    service, notifier, _exchange_calls = _service(
        tmp_path / "monitor",
        [{"market": "us", "code": "QCOM.US", "name": "高通"}],
        event_collector=collector,
        review_inbox=inbox,
    )

    result = service.run_once()

    assert result["sent_count"] == 1
    assert "最近5分钟收盘价：10.000" in notifier.messages[0][1][0]
    [event] = inbox.snapshot()["events"]
    assert event["current_price_source"] == "latest_completed_5m_close"


def test_us_notification_uses_realtime_tick_as_current_price(tmp_path):
    inbox = RealtimeReviewInbox(tmp_path / "tick-review.json")
    service, notifier, _exchange_calls = _service(
        tmp_path / "monitor",
        [{"market": "us", "code": "QCOM.US", "name": "高通"}],
        review_inbox=inbox,
        exchange_factory=lambda market: SimpleNamespace(
            market=market.value,
            ticks=lambda codes: {
                code: SimpleNamespace(last=145.67) for code in codes
            },
        ),
    )

    result = service.run_once()

    assert result["sent_count"] == 1
    assert (
        "当前价：145.670（获取 2026-08-04 10:01:00）"
        in notifier.messages[0][1][0]
    )
    [event] = inbox.snapshot()["events"]
    assert event["current_price"] == 145.67
    assert event["current_price_source"] == "realtime_tick"


def test_cross_market_quote_failure_keeps_buy_alert_but_fails_closed_ratio(
    tmp_path,
) -> None:
    def collector(states, *, names, **_kwargs):
        state = states["QCOM.US"]
        return [
            MonitorEvent(
                code="QCOM.US",
                name=names["QCOM.US"],
                side="buy",
                kind="strict_buy_point",
                bs_type="3buy",
                signal_time=state.signal_time,
                price=150.0,
                big_dir="up",
                reason="5m third buy",
                op_level="5m",
                mid_level="1m",
                big_level="30m",
                evidence_id="test:QCOM.US:3buy:quote-unavailable",
                recursive_level=0,
                anchor_time=state.signal_time,
                confirmed_time=state.signal_time,
                structure_anchor_price=150.0,
                structure_invalidation_price=145.0,
                setup_bs_type="3buy",
                setup_evidence_id="test:QCOM.US:5m:3buy:quote-unavailable",
                setup_recursive_level=0,
                setup_anchor_time=state.signal_time,
                setup_confirmed_time=state.signal_time,
                setup_available_time=state.signal_time,
            )
        ]

    def unavailable_ticks(_codes):
        raise RuntimeError("quote unavailable")

    inbox = RealtimeReviewInbox(tmp_path / "quote-failure-review.json")
    service, notifier, _exchange_calls = _service(
        tmp_path / "monitor",
        [{"market": "us", "code": "QCOM.US", "name": "高通"}],
        event_collector=collector,
        review_inbox=inbox,
        exchange_factory=lambda market: SimpleNamespace(
            market=market.value,
            ticks=unavailable_ticks,
        ),
    )

    result = service.run_once()

    assert result["sent_count"] == 1
    rendered = notifier.messages[0][1][0]
    assert "最近1分钟收盘价：150.000" in rendered
    assert "不使用已完成K线价格生成买入比例" in rendered
    assert "结构模型比例上限" not in rendered
    assert "回抽确认后再复合买入条件" not in rendered
    [event] = inbox.snapshot()["events"]
    assert event["position_recommendation"]["status"] == "UNRESOLVED"
    assert event["position_recommendation"]["basis"] == "REALTIME_PRICE_UNAVAILABLE"
    assert event["position_recommendation"]["recommended_percent"] is None


def test_us_realtime_event_is_always_copied_to_human_review(tmp_path):
    inbox = RealtimeReviewInbox(
        tmp_path / "review-inbox.json",
        clock=lambda: datetime(2026, 8, 4, 10, 1, tzinfo=CN),
    )
    service, _notifier, _exchange_calls = _service(
        tmp_path / "monitor",
        [{"market": "us", "code": "QCOM.US", "name": "高通"}],
        review_inbox=inbox,
    )

    result = service.run_once()

    assert result["notification_delivery"]["success_count"] == 1
    review = inbox.snapshot()
    assert review["event_count"] == 1
    event = review["events"][0]
    assert event["market"] == "us"
    assert event["code"] == "QCOM.US"
    assert event["point_type"] == "1sell"
    assert event["current_price"] == 10.0
    assert event["delivery_status"] == "delivered"
    assert event["structure_confirmed_at"] == "2026-08-04T10:00:00+08:00"
    assert event["signal_available_at"] == "2026-08-04T10:00:00+08:00"
    assert event["detected_at"] == "2026-08-04T10:01:00+08:00"
    assert event["delivered_at"] == "2026-08-04T10:01:00+08:00"
    assert event["selection_sources"] == ["MANUAL_ATTENTION_MONITOR"]
    assert event["is_manual_attention"] is True
    assert "is_holding" not in event
    assert event["real_order_transport_enabled"] is False


def test_us_notification_waits_until_review_record_is_durable(tmp_path):
    real_inbox = RealtimeReviewInbox(
        tmp_path / "review-inbox.json",
        clock=lambda: datetime(2026, 8, 4, 10, 1, tzinfo=CN),
    )

    class FlakyInbox:
        def __init__(self):
            self.record_calls = 0

        def record(self, event):
            self.record_calls += 1
            if self.record_calls == 1:
                raise OSError("review disk unavailable")
            real_inbox.record(event)

        def update_delivery(self, *args, **kwargs):
            real_inbox.update_delivery(*args, **kwargs)

    notifier = _Notifier()
    service, _notifier, _exchange_calls = _service(
        tmp_path / "monitor",
        [{"market": "us", "code": "QCOM.US", "name": "高通"}],
        notifier=notifier,
        review_inbox=FlakyInbox(),
    )

    first = service.run_once()
    assert first["sent_count"] == 0
    assert notifier.messages == []
    assert first["notification_delivery"]["pending_event_count"] == 1
    assert first["notification_delivery"]["last_failure_reason"] == (
        "REVIEW_INBOX_RECORD_FAILED"
    )

    second = service.run_once()
    assert second["sent_count"] == 1
    assert len(notifier.messages) == 1
    assert second["notification_delivery"]["pending_event_count"] == 0
    assert real_inbox.snapshot()["events"][0]["delivery_status"] == "delivered"


def test_us_review_update_retries_without_resending_dingtalk(tmp_path):
    real_inbox = RealtimeReviewInbox(
        tmp_path / "review-inbox.json",
        clock=lambda: datetime(2026, 8, 4, 10, 1, tzinfo=CN),
    )

    class FlakyInbox:
        def __init__(self):
            self.update_calls = 0

        def record(self, event):
            real_inbox.record(event)

        def update_delivery(self, *args, **kwargs):
            self.update_calls += 1
            if self.update_calls == 1:
                raise OSError("review update unavailable")
            real_inbox.update_delivery(*args, **kwargs)

    notifier = _Notifier()
    service, _notifier, _exchange_calls = _service(
        tmp_path / "monitor",
        [{"market": "us", "code": "QCOM.US", "name": "高通"}],
        notifier=notifier,
        review_inbox=FlakyInbox(),
    )

    first = service.run_once()
    assert first["sent_count"] == 1
    assert len(notifier.messages) == 1
    assert first["notification_delivery"]["pending_event_count"] == 1
    assert first["notification_delivery"][
        "review_projection_pending_event_count"
    ] == 1

    second = service.run_once()
    assert second["sent_count"] == 0
    assert len(notifier.messages) == 1
    assert second["notification_delivery"]["pending_event_count"] == 0
    assert real_inbox.snapshot()["events"][0]["delivery_status"] == "delivered"


def test_cross_market_alert_passes_symbol_aligned_chart_context(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    notifier = _RichNotifier()
    service, notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        notifier=notifier,
    )

    assert service.run_once()["sent_count"] == 1

    assert notifier.messages == []
    assert len(notifier.rich_messages) == 1
    chart = notifier.rich_messages[0][2]["charts"][0]
    assert chart["market"] == "us"
    assert chart["code"] == "TSLA.US"
    assert chart["name"] == "特斯拉"
    assert "strict_sell_point|TSLA.US" in chart["artifact_key"]
    assert chart["point_type"] == "1sell"
    assert chart["signal_time"] == "2026-08-04 10:00:00"
    assert chart["observed_at"] == "2026-08-04T10:01:00+08:00"
    assert chart["evidence_required"] is True
    assert notifier.rich_messages[0][2]["delivery_priority"] == 0
    assert notifier.rich_messages[0][2]["require_evidence_match"] is True


def test_holding_pending_priority_survives_durable_outbox_handoff() -> None:
    priority = monitor_module._pending_notification_delivery_priority
    assert priority(
        {
            "review_events": [{"side": "sell"}],
            "identities": [],
            "transition_codes": [],
        }
    ) == 0
    assert priority(
        {
            "review_events": [],
            "identities": ["strict_30m_context_warning|QCOM.US"],
            "transition_codes": ["QCOM.US"],
        }
    ) == 1
    assert priority(
        {
            "review_events": [
                {
                    "side": "buy",
                    "position_recommendation": {
                        "status": "RECOMMENDED",
                        "reason_codes": [],
                    },
                }
            ],
            "identities": [],
            "transition_codes": [],
        }
    ) == 2
    assert priority(
        {
            "review_events": [
                {
                    "side": "buy",
                    "position_recommendation": {
                        "status": "BLOCKED",
                        "reason_codes": ["BUY_SIGNAL_DISCOVERY_TOO_LATE_NO_CHASE"],
                    },
                }
            ],
            "identities": [],
            "transition_codes": [],
        }
    ) == 3
    assert priority(
        {
            "review_events": [],
            "identities": ["strict_buy_point|QCOM.US"],
            "transition_codes": [],
        }
    ) == 4


def test_same_signal_is_not_notified_twice_even_after_restart(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    service, notifier, _exchange_calls = _service(tmp_path, positions)

    first = service.run_once()
    second = service.run_once()
    restarted, restarted_notifier, _ = _service(tmp_path, positions)
    third = restarted.run_once()

    assert first["sent_count"] == 1
    assert second["sent_count"] == 0
    assert third["sent_count"] == 0
    assert len(notifier.messages) == 1
    assert restarted_notifier.messages == []


def test_qcom_semantic_buy_signal_is_not_republished_after_evidence_id_rebuild(
    tmp_path,
):
    positions = [{"market": "us", "code": "QCOM.US", "name": "高通"}]

    def collector(evidence_id):
        def collect(states, *, names, **_kwargs):
            state = states["QCOM.US"]
            return [
                MonitorEvent(
                    code="QCOM.US",
                    name=names["QCOM.US"],
                    side="buy",
                    kind="strict_buy_point",
                    bs_type="3buy",
                    signal_time=state.signal_time,
                    price=150.0,
                    big_dir="up",
                    reason="5m third buy",
                    op_level="5m",
                    mid_level="1m",
                    big_level="30m",
                    evidence_id=evidence_id,
                    recursive_level=0,
                    anchor_time=state.signal_time,
                    confirmed_time=state.signal_time,
                    setup_bs_type="3buy",
                    setup_evidence_id=f"setup:{evidence_id}",
                    setup_recursive_level=0,
                    setup_anchor_time=state.signal_time,
                    setup_confirmed_time=state.signal_time,
                    setup_available_time=state.signal_time,
                )
            ]

        return collect

    first, first_notifier, _ = _service(
        tmp_path,
        positions,
        event_collector=collector("sha256:old-evidence"),
    )
    assert first.run_once()["sent_count"] == 1

    restarted, restarted_notifier, _ = _service(
        tmp_path,
        positions,
        event_collector=collector("sha256:rebuilt-evidence"),
    )
    assert restarted.run_once()["sent_count"] == 0
    assert len(first_notifier.messages) == 1
    assert restarted_notifier.messages == []


def test_failed_delivery_is_not_deduplicated_and_retries(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    notifier = _Notifier([False, True])
    emitted: set[str] = set()

    def one_shot_collector(states, *, names, **_kwargs):
        events = []
        for code, state in states.items():
            if code in emitted:
                continue
            emitted.add(code)
            events.append(_strict_sell(code, names[code], state.signal_time))
        return events

    service, notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        notifier=notifier,
        event_collector=one_shot_collector,
    )

    first = service.run_once()
    second = service.run_once()

    assert first["sent_count"] == 0
    assert first["failed_count"] == 1
    assert first["notification_delivery"]["failure_count"] == 1
    assert first["notification_delivery"]["pending_event_count"] == 1
    assert second["sent_count"] == 1
    assert second["failed_count"] == 0
    assert second["notification_delivery"]["success_count"] == 1
    assert second["notification_delivery"]["pending_event_count"] == 0
    assert len(notifier.messages) == 2


def test_failed_delivery_expires_after_the_realtime_window(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    notifier = _Notifier([False, True])
    now = [datetime(2026, 8, 4, 10, 1, tzinfo=CN)]
    inbox = RealtimeReviewInbox(
        tmp_path / "review_inbox.json",
        clock=lambda: now[0],
    )
    service, notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        notifier=notifier,
        clock=lambda: now[0],
        review_inbox=inbox,
    )

    first = service.run_once()
    assert first["notification_delivery"]["pending_event_count"] == 1
    now[0] = now[0].replace(minute=11)

    second = service.run_once()

    assert second["sent_count"] == 0
    assert second["notification_delivery"]["pending_event_count"] == 0
    assert second["notification_delivery"]["expired_event_count"] == 1
    assert second["notification_delivery"]["success_count"] == 0
    assert len(notifier.messages) == 1
    review_event = inbox.snapshot()["events"][0]
    assert review_event["delivery_status"] == "expired"
    assert review_event["delivery_reason"] == "NOTIFICATION_DELIVERY_EXPIRED"


def test_us_signal_just_past_freshness_is_sent_as_delayed_review_within_grace(
    tmp_path,
):
    positions = [{"market": "us", "code": "TSLA.US", "name": "Tesla"}]
    now = [datetime(2026, 8, 4, 10, 11, tzinfo=CN)]
    inbox = RealtimeReviewInbox(
        tmp_path / "stale-review.json",
        clock=lambda: now[0],
    )
    service, notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        clock=lambda: now[0],
        review_inbox=inbox,
    )

    first = service.run_once()
    second = service.run_once()

    assert first["sent_count"] == 1
    assert first["expired_count"] == 0
    assert second["sent_count"] == 0
    assert len(notifier.messages) == 1
    [review_event] = inbox.snapshot()["events"]
    assert review_event["delivery_status"] == "delivered"
    assert review_event["signal_available_at"] == "2026-08-04T10:00:00+08:00"


def test_us_buy_notification_reprices_live_event_and_blocks_chasing(tmp_path) -> None:
    def buy_collector(states, *, names, **_kwargs):
        return [
            MonitorEvent(
                code=code,
                name=names[code],
                side="buy",
                kind="strict_buy_point",
                bs_type="3buy",
                signal_time=state.signal_time,
                price=10.80,
                big_dir="up",
                reason="strict_5m_3buy_trade_signal",
                evidence_id=f"test:{code}:3buy",
                recursive_level=0,
                anchor_time=state.signal_time,
                confirmed_time=state.signal_time,
                structure_anchor_price=10.00,
                structure_invalidation_price=9.50,
            )
            for code, state in states.items()
        ]

    inbox = RealtimeReviewInbox(tmp_path / "buy-review.json")
    service, notifier, _exchange_calls = _service(
        tmp_path / "monitor",
        [{"market": "us", "code": "QCOM.US", "name": "高通"}],
        event_collector=buy_collector,
        review_inbox=inbox,
    )

    result = service.run_once()

    assert result["sent_count"] == 1
    title, lines = notifier.messages[0]
    assert title.startswith("买卖通知｜买点确认·0%保护｜")
    assert "风险参考：本条买入不纳入操作计划" in lines[0]
    assert "不追价，等待新的5分钟结构" in lines[0]
    [review_event] = inbox.snapshot()["events"]
    assert review_event["position_recommendation"]["recommended_percent"] == "0"


def test_us_failed_delivery_expires_from_signal_time_not_queue_time(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "Tesla"}]
    notifier = _Notifier([False, True])
    now = [datetime(2026, 8, 4, 10, 1, 30, tzinfo=CN)]
    service, notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        notifier=notifier,
        clock=lambda: now[0],
    )

    first = service.run_once()
    now[0] = datetime(2026, 8, 4, 10, 10, 1, tzinfo=CN)
    second = service.run_once()

    assert first["notification_delivery"]["pending_event_count"] == 1
    assert second["sent_count"] == 0
    assert second["expired_count"] == 1
    assert len(notifier.messages) == 1


def test_dry_run_never_masquerades_as_verified_delivery(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    notifier = _Notifier()
    notifier.dry_run = True
    service, _notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        notifier=notifier,
    )

    delivery = service.run_once()["notification_delivery"]

    assert delivery["delivery_mode"] == "DRY_RUN"
    assert delivery["operationally_verified"] is False
    assert delivery["status"] == "simulated"
    assert delivery["reason_code"] == "DRY_RUN_DELIVERY_ONLY"
    assert delivery["success_count"] == 0
    assert delivery["simulated_success_count"] == 1


def test_big_down_context_warning_is_a_transition_not_a_sell_signal(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    service, notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        event_collector=_big_down_collector,
    )

    assert service.run_once()["sent_count"] == 1
    assert service.run_once()["sent_count"] == 0

    state = service._states[("us", "TSLA.US")]
    state.direction = "up"
    state.signal_time = "2026-08-04 10:15:00"
    assert service.run_once()["sent_count"] == 0
    state.direction = "down"
    state.signal_time = "2026-08-04 10:30:00"
    assert service.run_once()["sent_count"] == 1

    restarted, restarted_notifier, _ = _service(
        tmp_path,
        positions,
        event_collector=_big_down_collector,
    )
    assert restarted.run_once()["sent_count"] == 0
    assert len(notifier.messages) == 2
    assert restarted_notifier.messages == []
    assert all(
        title.startswith("买卖通知｜环境风险提示｜人工关注｜")
        and "30分钟环境转弱风险（不是买卖点）" in title
        and "等待5分钟卖点达到操作确认后再决定卖出" in lines[0]
        for title, lines in notifier.messages
    )


def test_failed_notification_outbox_survives_app_restart(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    failed_notifier = _Notifier([False])
    first, _failed_notifier, _ = _service(
        tmp_path,
        positions,
        notifier=failed_notifier,
    )
    failed = first.run_once()
    assert failed["notification_delivery"]["pending_event_count"] == 1

    restarted_notifier = _Notifier()
    restarted, restarted_notifier, _ = _service(
        tmp_path,
        positions,
        notifier=restarted_notifier,
        event_collector=lambda *_args, **_kwargs: [],
    )
    recovered = restarted.run_once()

    assert recovered["sent_count"] == 1
    assert recovered["notification_delivery"]["pending_event_count"] == 0
    assert len(restarted_notifier.messages) == 1
    assert "一类卖点" in restarted_notifier.messages[0][1][0]


def test_outbox_is_not_erased_when_symbol_leaves_manual_holdings(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    service, notifier, _ = _service(
        tmp_path,
        positions,
        notifier=_Notifier([False]),
    )
    assert service.run_once()["notification_delivery"]["pending_event_count"] == 1

    positions.clear()
    result = service.run_once()

    assert result["declared_count"] == 0
    assert result["sent_count"] == 1
    assert result["notification_delivery"]["pending_event_count"] == 0
    assert len(notifier.messages) == 2


def test_pending_delivery_is_drained_even_after_market_closes(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    notifier = _Notifier([False, True])
    market_open = [True]
    service = HoldingGroupMonitorService(
        positions_provider=lambda: list(positions),
        notifier=notifier,
        state_root=tmp_path,
        exchange_provider=lambda market: SimpleNamespace(market=market.value),
        market_open_provider=lambda *_args: market_open[0],
        state_factory=_State,
        event_collector=_event_collector,
        clock=lambda: datetime(2026, 8, 4, 10, 1, tzinfo=CN),
    )

    assert service.run_once()["notification_delivery"]["pending_event_count"] == 1
    market_open[0] = False

    recovered = service.run_once()

    assert recovered["sent_count"] == 1
    assert recovered["closed_count"] == 1
    assert recovered["notification_delivery"]["pending_event_count"] == 0
    assert len(notifier.messages) == 2


def test_failed_or_warming_state_never_publishes_stale_structure_event(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    service, notifier, _exchange_calls = _service(tmp_path, positions)
    service.run_once()
    notifier.messages.clear()
    state = service._states[("us", "TSLA.US")]
    state.signal_time = "2026-08-04 10:30:00"
    state.warmup_ready = False

    result = service.run_once()

    assert result["sent_count"] == 0
    assert result["positions"][0]["status"] == "warming_up"
    assert result["awaiting_count"] == 1
    assert notifier.messages == []


def test_warming_direction_does_not_suppress_first_context_warning(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "Tesla"}]

    class _InitiallyWarmingState(_State):
        def __init__(self, code, exchange, **levels):
            super().__init__(code, exchange, **levels)
            self.warmup_ready = False

    service, notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        event_collector=_big_down_collector,
        state_factory=_InitiallyWarmingState,
    )

    warming = service.run_once()
    assert warming["sent_count"] == 0
    assert service._runtime_ledger.previous_direction("us", "TSLA.US") is None

    service._states[("us", "TSLA.US")].warmup_ready = True
    ready = service.run_once()
    assert ready["sent_count"] == 1
    assert len(notifier.messages) == 1
    assert service._runtime_ledger.previous_direction("us", "TSLA.US") == "down"


def test_warmup_that_does_not_converge_becomes_an_observable_outage(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    service, notifier, _exchange_calls = _service(tmp_path, positions)
    service.run_once()
    notifier.messages.clear()
    state = service._states[("us", "TSLA.US")]
    state.warmup_ready = False
    state.consecutive_warmup_incomplete = 3

    result = service.run_once()

    assert result["failed_count"] == 1
    assert result["positions"][0]["reason_code"] == ("MULTI_TIMEFRAME_WARMUP_STALLED")
    assert result["health_alert_count"] == 0
    assert notifier.messages == []
    assert service._runtime_ledger.outage_active("us", "TSLA.US") is True


def test_outage_and_recovery_are_tracked_without_notifications(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    service, notifier, _exchange_calls = _service(
        tmp_path,
        positions,
        event_collector=lambda *_args, **_kwargs: [],
    )
    service.run_once()
    state = service._states[("us", "TSLA.US")]
    state.consecutive_refresh_failures = 3

    failed = service.run_once()
    assert failed["health_alert_count"] == 0
    assert failed["failed_count"] == 1
    assert notifier.messages == []
    assert service._runtime_ledger.outage_active("us", "TSLA.US") is True

    state.consecutive_refresh_failures = 0
    recovered = service.run_once()
    assert recovered["health_alert_count"] == 0
    assert recovered["failed_count"] == 0
    assert notifier.messages == []
    assert service._runtime_ledger.outage_active("us", "TSLA.US") is False


def test_closed_market_keeps_holding_visible_but_does_not_scan(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    service, notifier, exchange_calls = _service(
        tmp_path,
        positions,
        market_open=False,
    )
    _register(service)

    result = service.run_once()

    assert exchange_calls == ["us"]
    assert notifier.messages == []
    assert result["monitored_count"] == 0
    assert result["covered_count"] == 1
    assert result["closed_count"] == 1
    assert result["positions"][0]["status"] == "market_closed"
    assert service.health_snapshot()["ready"] is True


def test_us_auxiliary_lane_does_not_treat_premarket_stale_bars_as_live(
    monkeypatch,
) -> None:
    monkeypatch.setattr(monitor_module, "market_now_trading", lambda *_args: True)
    exchange = object()

    assert (
        monitor_module._default_market_open(
            exchange,
            "us",
            datetime(2026, 8, 4, 19, 0, tzinfo=CN),
        )
        is False
    )
    assert (
        monitor_module._default_market_open(
            exchange,
            "us",
            datetime(2026, 8, 4, 22, 0, tzinfo=CN),
        )
        is True
    )


def test_unknown_market_is_reported_instead_of_silently_dropped(tmp_path):
    positions = [{"market": "unknown", "code": "X", "name": "未知"}]
    service, _notifier, exchange_calls = _service(tmp_path, positions)
    _register(service)

    result = service.run_once()
    health = service.health_snapshot()

    assert exchange_calls == []
    assert result["declared_count"] == 1
    assert result["monitored_count"] == 0
    assert result["failed_count"] == 1
    assert result["positions"][0]["reason_code"] == "UNSUPPORTED_MARKET"
    assert health["ready"] is False
    assert health["reason_code"] == "HOLDING_MONITOR_DEGRADED"


def test_scheduler_registration_is_idempotent(tmp_path):
    service, _notifier, _exchange_calls = _service(tmp_path, [])

    class _Scheduler:
        def __init__(self) -> None:
            self.jobs = {}

        def add_job(self, func, **kwargs):
            self.jobs[kwargs["id"]] = (func, kwargs)
            return SimpleNamespace(id=kwargs["id"])

    scheduler = _Scheduler()
    first = service.register_job(scheduler)
    second = service.register_job(scheduler)

    assert first == "holding_group_realtime_monitor"
    assert second == first
    assert list(scheduler.jobs) == [first]
    assert scheduler.jobs[first][1]["name"] == ("人工关注分组跨市场实时监听")
    assert scheduler.jobs[first][1]["executor"] == "realtime_monitor"
    assert service.health_snapshot()["job_registered"] is True


def test_us_every_group_is_monitored_but_other_markets_remain_holding_only():
    universe = build_non_a_monitor_universe(
        {
            "我的关注": [
                {"market": "us", "code": "AAPL.US", "name": "苹果"},
                {"market": "hk", "code": "HK.00700", "name": "腾讯"},
            ],
            "三买": [
                {"market": "us", "code": "AAPL.US", "name": "苹果"},
                {"market": "us", "code": "TSLA.US", "name": "特斯拉"},
            ],
            "我的持仓": [
                {"market": "us", "code": "TSLA.US", "name": "特斯拉"},
                {"market": "hk", "code": "HK.00700", "name": "腾讯"},
                {"market": "a", "code": "SZ.300826", "name": "测绘股份"},
            ],
        }
    )

    assert [(row["market"], row["code"]) for row in universe] == [
        ("hk", "HK.00700"),
        ("us", "AAPL.US"),
        ("us", "TSLA.US"),
    ]
    by_code = {row["code"]: row for row in universe}
    assert by_code["AAPL.US"]["groups"] == ["三买", "我的关注"]
    assert by_code["AAPL.US"]["is_holding"] is False
    assert by_code["AAPL.US"]["monitoring_scope"] == "WATCHLIST"
    assert by_code["TSLA.US"]["groups"] == ["三买", "我的持仓"]
    assert by_code["TSLA.US"]["is_holding"] is True
    assert by_code["HK.00700"]["is_holding"] is True


def test_watched_us_signal_is_worded_as_general_attention(tmp_path):
    positions = [
        {
            "market": "us",
            "code": "AAPL.US",
            "name": "苹果",
            "groups": ["我的关注"],
            "is_holding": False,
        }
    ]

    def collector(states, *, names, holdings, **_kwargs):
        assert holdings == set()
        return [
            MonitorEvent(
                code="AAPL.US",
                name=names["AAPL.US"],
                side="buy",
                kind="strict_buy_point",
                bs_type="3buy",
                signal_time=states["AAPL.US"].signal_time,
                price=200.0,
                big_dir="up",
                reason="5m third buy",
                op_level="5m",
                mid_level="1m",
                big_level="30m",
                evidence_id=f"test:AAPL.US:3buy:{states['AAPL.US'].signal_time}",
                recursive_level=0,
                anchor_time=states["AAPL.US"].signal_time,
                confirmed_time=states["AAPL.US"].signal_time,
                setup_bs_type="3buy",
                setup_evidence_id=(
                    f"test:AAPL.US:5m:3buy:{states['AAPL.US'].signal_time}"
                ),
                setup_recursive_level=0,
                setup_anchor_time=states["AAPL.US"].signal_time,
                setup_confirmed_time=states["AAPL.US"].signal_time,
                setup_available_time=states["AAPL.US"].signal_time,
            )
        ]

    service, notifier, _ = _service(tmp_path, positions, event_collector=collector)
    result = service.run_once()

    assert result["positions"][0]["monitoring_scope"] == "WATCHLIST"
    title, lines = notifier.messages[0]
    assert "普通关注" in title
    assert "人工关注" not in title
    assert "实时价格未取得" in lines[0]
    assert "不使用已完成K线价格生成买入比例" in lines[0]
    assert "回抽确认后再复合买入条件" not in lines[0]


def test_membership_change_can_pull_registered_job_forward(tmp_path):
    service, _notifier, _exchange_calls = _service(tmp_path, [])

    class _Scheduler:
        def __init__(self) -> None:
            self.modified = []
            self.wake_count = 0

        def add_job(self, _func, **_kwargs):
            return SimpleNamespace(id=_kwargs["id"])

        def modify_job(self, job_id, **kwargs):
            self.modified.append((job_id, kwargs))

        def wakeup(self):
            self.wake_count += 1

    scheduler = _Scheduler()
    service.register_job(scheduler)

    assert service.request_refresh() is True
    assert scheduler.modified[0][0] == "holding_group_realtime_monitor"
    assert scheduler.modified[0][1]["next_run_time"].tzinfo is not None
    assert scheduler.wake_count == 1
