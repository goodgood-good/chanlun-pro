from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import cl_app.services.holding_group_monitor as monitor_module
from cl_app.services.holding_group_monitor import (
    HoldingGroupMonitorConfig,
    HoldingGroupMonitorService,
    build_non_a_monitor_universe,
)
from chanlun.recursive_bt.monitor.live_monitor import MonitorEvent


CN = ZoneInfo("Asia/Shanghai")


class _Notifier:
    available = True

    def __init__(self, results: list[bool] | None = None) -> None:
        self.messages: list[tuple[str, list[str]]] = []
        self.results = list(results or [])

    def send(self, title: str, lines: list[str]) -> bool:
        self.messages.append((title, list(lines)))
        return self.results.pop(0) if self.results else True


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


def _small_sell(code: str, name: str, signal_time: str) -> MonitorEvent:
    return MonitorEvent(
        code=code,
        name=name,
        side="sell",
        kind="small_sell",
        bs_type="1sell",
        signal_time=signal_time,
        price=10.0,
        big_dir="down",
        reason="1m sell point",
        op_level="1m",
        mid_level="5m",
        big_level="30m",
    )


def _event_collector(states, *, names, holdings, **_kwargs):
    assert holdings == set(states)
    return [
        _small_sell(code, names[code], states[code].signal_time)
        for code in states
    ]


def _big_down_collector(states, *, names, holdings, **_kwargs):
    assert holdings == set(states)
    return [
        MonitorEvent(
            code=code,
            name=names[code],
            side="exit",
            kind="big_down_exit",
            bs_type="",
            signal_time=state.signal_time,
            price=10.0,
            big_dir="down",
            reason="30m turned down",
            op_level="1m",
            mid_level="5m",
            big_level="30m",
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
):
    notifier = notifier or _Notifier()
    exchange_calls: list[str] = []

    def exchange_provider(market):
        exchange_calls.append(market.value)
        return SimpleNamespace(market=market.value)

    service = HoldingGroupMonitorService(
        positions_provider=lambda: list(positions),
        notifier=notifier,
        state_root=tmp_path,
        exchange_provider=exchange_provider,
        market_open_provider=lambda _exchange, _market, _now: market_open,
        state_factory=state_factory,
        event_collector=event_collector,
        clock=lambda: datetime(2026, 8, 4, 10, 1, tzinfo=CN),
        config=HoldingGroupMonitorConfig(max_workers=4),
    )
    return service, notifier, exchange_calls


def _register(service):
    class _Scheduler:
        def add_job(self, func, **kwargs):
            return SimpleNamespace(id=kwargs["id"], func=func)

    service.register_job(_Scheduler())


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
        assert title.startswith("买卖通知｜持仓股｜")
        assert title.endswith("1分钟一类卖点")
        assert len(lines) == 1
        assert "1分钟一类卖点" in lines[0]
        assert "2026-08-04 10:00:00" in lines[0]
        assert "参考价 10.000" in lines[0]
        assert "30分钟向下" in lines[0]
        assert "建议：优先考虑减仓" in lines[0]
        assert "参考卖出比例" not in lines[0]
        assert "辅助结构线索" not in lines[0]
        assert "time=" not in lines[0]
        assert "sell point" not in lines[0]
    assert {row["code"] for row in result["positions"]} == {
        "TSLA.US",
        "HK.00700",
    }
    assert all(row["status"] == "monitoring" for row in result["positions"])
    assert all(row["op_level"] == "1m" for row in result["positions"])
    assert all(row["mid_level"] == "5m" for row in result["positions"])
    assert all(row["big_level"] == "30m" for row in result["positions"])


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
            events.append(_small_sell(code, names[code], state.signal_time))
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


def test_big_down_alert_is_a_transition_not_every_completed_bar(tmp_path):
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


def test_outbox_is_pruned_when_symbol_leaves_manual_holdings(tmp_path):
    positions = [{"market": "us", "code": "TSLA.US", "name": "特斯拉"}]
    service, _notifier, _ = _service(
        tmp_path,
        positions,
        notifier=_Notifier([False]),
    )
    assert service.run_once()["notification_delivery"]["pending_event_count"] == 1

    positions.clear()
    result = service.run_once()

    assert result["declared_count"] == 0
    assert result["notification_delivery"]["pending_event_count"] == 0


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
    assert result["positions"][0]["reason_code"] == (
        "MULTI_TIMEFRAME_WARMUP_STALLED"
    )
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

    assert monitor_module._default_market_open(
        exchange,
        "us",
        datetime(2026, 8, 4, 19, 0, tzinfo=CN),
    ) is False
    assert monitor_module._default_market_open(
        exchange,
        "us",
        datetime(2026, 8, 4, 22, 0, tzinfo=CN),
    ) is True


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


def test_watched_us_signal_is_not_worded_as_a_holding(tmp_path):
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
                kind="small_buy",
                bs_type="3buy",
                signal_time=states["AAPL.US"].signal_time,
                price=200.0,
                big_dir="up",
                reason="1m third buy",
                op_level="1m",
                mid_level="5m",
                big_level="30m",
            )
        ]

    service, notifier, _ = _service(
        tmp_path, positions, event_collector=collector
    )
    result = service.run_once()

    assert result["positions"][0]["monitoring_scope"] == "WATCHLIST"
    title, lines = notifier.messages[0]
    assert "关注股" in title
    assert "持仓股" not in title
    assert "考虑分批买入" in lines[0]
    assert "增持" not in lines[0]


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
