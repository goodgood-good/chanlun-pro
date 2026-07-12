from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import ast
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.monitor import (
    DecisionSupportRuntime,
    MonitorConfig,
    register_decision_support_jobs,
)


def ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


def _candidate(event_id: str):
    return SimpleNamespace(
        accepted=True,
        event=SimpleNamespace(event_id=event_id),
        rule_evaluation=SimpleNamespace(safe_to_proceed=True),
    )


@dataclass
class _ScanResult:
    trend_candidates: tuple[object, ...] = ()
    reversal_candidates: tuple[object, ...] = ()
    code: str = "ok"
    failures: tuple[object, ...] = ()


class _Scanner:
    def __init__(self, results: list[object] | None = None) -> None:
        self.results = list(results or [_ScanResult()])
        self.calls: list[datetime] = []

    def scan_closed_bar(self, bar_closed_at: datetime):
        self.calls.append(bar_closed_at)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _Scheduler:
    def __init__(self) -> None:
        self.jobs: list[tuple[object, dict[str, object]]] = []

    def add_job(self, func, **kwargs):
        self.jobs.append((func, kwargs))
        return SimpleNamespace(id=kwargs["id"])


class _StrategyRunProbe:
    def __init__(self) -> None:
        self.run_id = "paper-run-probe"
        self.epoch = 7
        self.strategy_run_fingerprint = "sha256:" + "f" * 64
        self.closed = False
        self.active_operation: str | None = None
        self.events: list[tuple[str, str]] = []

    @contextmanager
    def mutation_lease(self, operation: str):
        if self.closed:
            raise RuntimeError("strategy_run_not_active")
        self.active_operation = operation
        self.events.append(("enter", operation))
        try:
            yield object()
        finally:
            self.events.append(("exit", operation))
            self.active_operation = None

    def require_current_mutation_lease(self) -> None:
        if self.closed:
            raise RuntimeError("strategy_run_not_active")
        if self.active_operation is None:
            raise RuntimeError("strategy_run_mutation_lease_required")
        self.events.append(("require", self.active_operation))


def test_monitor_is_disabled_by_default_and_rejects_auto_order() -> None:
    config = MonitorConfig.from_mapping(None)

    assert config.enabled is False
    assert config.auto_order_enabled is False
    with pytest.raises(ValueError, match="auto_order_enabled"):
        MonitorConfig.from_mapping(
            {"enabled": True, "auto_order_enabled": True}
        )


def test_demo_config_explicitly_disables_orders() -> None:
    path = Path("src/chanlun/config.py.demo")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and target.id == "DECISION_SUPPORT"
    }

    config = values["DECISION_SUPPORT"]
    assert config["enabled"] is False
    assert config["paper_enabled"] is True
    assert config["auto_order_enabled"] is False
    assert config["markets"] == ["a"]


def test_scan_cycle_requires_closed_bar_and_recovers_after_exception() -> None:
    scanner = _Scanner([RuntimeError("provider down"), _ScanResult()])
    runtime = DecisionSupportRuntime(
        scanner,
        lambda event_id: event_id,
        config=MonitorConfig(enabled=True),
    )

    not_closed = runtime.scan_cycle(ts("2026-07-13T10:33:00+08:00"))
    failed = runtime.scan_cycle(ts("2026-07-13T10:35:30+08:00"))
    recovered = runtime.scan_cycle(ts("2026-07-13T10:35:40+08:00"))

    assert not_closed.code == "bar_not_closed"
    assert failed.code == "scan_failed"
    assert failed.bar_closed_at == ts("2026-07-13T10:35:00+08:00")
    assert failed.detail == "RuntimeError"
    assert recovered.code == "scan_complete"
    assert recovered.bar_closed_at == ts("2026-07-13T10:35:00+08:00")
    assert scanner.calls == [
        ts("2026-07-13T10:35:00+08:00"),
        ts("2026-07-13T10:35:00+08:00"),
    ]
    assert runtime.health().scan_failures == 1


@pytest.mark.parametrize(
    ("scan_code", "failures", "expected_detail"),
    (
        ("partial_failure", (), "partial_failure"),
        ("ok", (SimpleNamespace(reason="broken-symbol"),), "scanner_failures_present"),
    ),
)
def test_incomplete_scan_never_enqueues_current_cycle_events(
    scan_code,
    failures,
    expected_detail,
) -> None:
    reviewed: list[str] = []
    runtime = DecisionSupportRuntime(
        _Scanner(
            [
                _ScanResult(
                    trend_candidates=(_candidate("event-1"),),
                    code=scan_code,
                    failures=failures,
                )
            ]
        ),
        reviewed.append,
        config=MonitorConfig(enabled=True),
    )
    bar_closed_at = ts("2026-07-13T10:35:00+08:00")

    result = runtime.scan_cycle(bar_closed_at)

    assert result.code == "scan_incomplete"
    assert result.bar_closed_at == bar_closed_at
    assert result.detail == expected_detail
    assert result.queued_reviews == 0
    assert result.queue_overflow == 0
    assert runtime.health().queue_depth == 0
    assert runtime.health().scan_failures == 1
    assert runtime.review_cycle().code == "review_idle"
    assert reviewed == []


def test_review_queue_overflow_fails_scan_atomically() -> None:
    scanner = _Scanner(
        [
            _ScanResult(
                trend_candidates=(_candidate("event-1"), _candidate("event-1")),
                reversal_candidates=(_candidate("event-2"),),
            )
        ]
    )
    reviewed: list[str] = []
    runtime = DecisionSupportRuntime(
        scanner,
        reviewed.append,
        config=MonitorConfig(enabled=True, review_queue_limit=1),
    )

    result = runtime.scan_cycle(ts("2026-07-13T10:35:00+08:00"))

    assert result.code == "scan_incomplete"
    assert result.bar_closed_at == ts("2026-07-13T10:35:00+08:00")
    assert result.detail == "review_queue_overflow"
    assert result.queued_reviews == 0
    assert result.queue_overflow == 1
    assert runtime.health().queue_depth == 0
    assert runtime.health().scan_failures == 1
    assert runtime.review_cycle().code == "review_idle"
    assert reviewed == []


def test_restart_restores_pending_reviews_without_duplicates() -> None:
    reviewed: list[str] = []
    runtime = DecisionSupportRuntime(
        _Scanner(),
        reviewed.append,
        config=MonitorConfig(enabled=True, review_queue_limit=3),
        pending_review_loader=lambda: ("event-1", "event-1", "event-2"),
    )

    restored = runtime.restore_pending_reviews()
    runtime.restore_pending_reviews()
    first = runtime.review_cycle()
    second = runtime.review_cycle()

    assert restored == 2
    assert first.event_id == "event-1"
    assert second.event_id == "event-2"
    assert reviewed == ["event-1", "event-2"]


def test_bound_pending_review_restore_holds_exact_lease_and_rejects_closed_run(
) -> None:
    runtime = DecisionSupportRuntime(
        _Scanner(),
        lambda event_id: event_id,
        config=MonitorConfig(enabled=True),
        pending_review_loader=lambda: ("event-1",),
    )
    active = _StrategyRunProbe()
    runtime.bind_strategy_run(active)

    assert runtime.restore_pending_reviews() == 1
    assert active.events == [
        ("enter", "decision_support_runtime.restore_pending_reviews"),
        ("require", "decision_support_runtime.restore_pending_reviews"),
        ("exit", "decision_support_runtime.restore_pending_reviews"),
    ]
    assert runtime.health().queue_depth == 1

    active.closed = True
    with pytest.raises(RuntimeError, match="strategy_run_not_active"):
        runtime.restore_pending_reviews()

    assert runtime.health().queue_depth == 1


def test_review_failure_retries_once_then_dead_letters_without_churn() -> None:
    calls: list[str] = []

    def always_fails(event_id: str) -> None:
        calls.append(event_id)
        raise RuntimeError("deterministic failure")

    runtime = DecisionSupportRuntime(
        _Scanner([_ScanResult(trend_candidates=(_candidate("event-1"),))]),
        always_fails,
        config=MonitorConfig(enabled=True),
    )
    runtime.scan_cycle(ts("2026-07-13T10:35:00+08:00"))

    first = runtime.review_cycle(ts("2026-07-13T10:35:01+08:00"))
    second = runtime.review_cycle(ts("2026-07-13T10:35:02+08:00"))
    third = runtime.review_cycle(ts("2026-07-13T10:35:03+08:00"))

    assert first.code == "review_failed_retrying"
    assert second.code == "review_abandoned"
    assert third.code == "review_idle"
    assert calls == ["event-1", "event-1"]
    assert runtime.health().queue_depth == 0
    assert runtime.health().review_failures == 2


def test_pending_review_restore_fails_closed_on_queue_overflow() -> None:
    runtime = DecisionSupportRuntime(
        _Scanner(),
        lambda event_id: event_id,
        config=MonitorConfig(enabled=True, review_queue_limit=1),
        pending_review_loader=lambda: ("event-1", "event-2"),
    )

    with pytest.raises(RuntimeError, match="pending review restore overflow"):
        runtime.restore_pending_reviews()

    assert runtime.health().queue_depth == 1
    assert runtime.health().queue_overflow == 1


def test_scheduler_registration_is_opt_in_and_bounded() -> None:
    disabled_scheduler = _Scheduler()
    disabled_runtime = DecisionSupportRuntime(
        _Scanner(),
        lambda event_id: event_id,
        config=MonitorConfig(),
    )
    enabled_scheduler = _Scheduler()
    enabled_runtime = DecisionSupportRuntime(
        _Scanner(),
        lambda event_id: event_id,
        config=MonitorConfig(enabled=True, scan_interval_seconds=30),
    )

    assert register_decision_support_jobs(
        disabled_scheduler,
        disabled_runtime,
    ) == {}
    jobs = register_decision_support_jobs(enabled_scheduler, enabled_runtime)

    assert set(jobs) == {"scan", "review"}
    assert {kwargs["id"] for _, kwargs in enabled_scheduler.jobs} == {
        "decision_support_scan",
        "decision_support_review",
    }
    assert all(kwargs["max_instances"] == 1 for _, kwargs in enabled_scheduler.jobs)
    assert all(kwargs["coalesce"] is True for _, kwargs in enabled_scheduler.jobs)
