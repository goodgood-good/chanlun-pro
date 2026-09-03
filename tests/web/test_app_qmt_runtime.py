from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import threading
from zoneinfo import ZoneInfo

import pytest

from cl_app.services.app_qmt_runtime import (
    APP_QMT_CONTRACT_ID,
    AppQmtRuntimeController,
    QMT_DAILY_JOB_ID,
    QMT_MONITOR_JOB_ID,
    QMT_OBSERVATION_SCHEMA,
)
from cl_app.services import app_qmt_runtime


CN = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[2]


def test_atomic_json_retries_transient_windows_share_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "owner.json"
    target.write_text('{"generation":1}\n', encoding="utf-8")
    original_replace = app_qmt_runtime.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            error = PermissionError("temporarily opened without delete sharing")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        original_replace(source, destination)

    monkeypatch.setattr(app_qmt_runtime.os, "name", "nt")
    monkeypatch.setattr(app_qmt_runtime.os, "replace", flaky_replace)

    app_qmt_runtime._atomic_json(target, {"generation": 2})

    assert attempts == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 2}


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}

    def add_job(self, function, **kwargs) -> None:
        self.jobs[str(kwargs["id"])] = {"function": function, **kwargs}


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _observation(
    *,
    action: str,
    ready: bool,
    started_at: str | None = None,
    changed: bool = False,
    process_count: int | None = None,
) -> dict[str, object]:
    return {
        "schema": QMT_OBSERVATION_SCHEMA,
        "observed_at": "2026-08-03T08:00:00+08:00",
        "action": action.upper(),
        "ready": ready,
        "status": "ready" if ready else "not_ready",
        "reason_code": "READY" if ready else "QMT_MAIN_PROCESS_MISSING",
        "changed": changed,
        "qmt_executable": "D:/qmt/bin.x64/XtItClient.exe",
        "qmt_directory": "D:/qmt/bin.x64",
        "market_data_port": 58610,
        "market_data_rpc_ready": ready,
        "automatic_control_ready": True,
        "uncontrollable_process_ids": [],
        "log_retention_days": 30,
        "log_max_total_bytes": 104857600,
        "main_process_count": 1 if ready else 0,
        "process_count": (1 if ready else 0)
        if process_count is None
        else process_count,
        "main_started_at": started_at,
        "processes": [],
        "error": None,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }


class FakeRunner:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.actions: list[str] = []

    def __call__(self, command, **_kwargs):
        action = str(command[command.index("-Action") + 1])
        self.actions.append(action)
        payload = self.responses.pop(0)
        return subprocess.CompletedProcess(
            command,
            0 if payload.get("ready") is True else 3,
            stdout=json.dumps(payload),
            stderr="",
        )


def _controller(
    tmp_path: Path,
    *,
    clock: MutableClock,
    runner: FakeRunner,
    before_change=None,
    after_change=None,
) -> tuple[AppQmtRuntimeController, FakeScheduler]:
    scheduler = FakeScheduler()
    controller = AppQmtRuntimeController(
        scheduler=scheduler,
        repository_root=ROOT,
        clock=clock,
        runner=runner,
        helper_script=ROOT / "ops" / "manage_qmt_runtime.ps1",
        state_path=tmp_path / "state.json",
        owner_path=tmp_path / "owner.json",
        before_change=before_change,
        after_change=after_change,
        warmup_seconds=0,
    )
    return controller, scheduler


def test_qmt_owner_rejects_live_peer_and_reclaims_stale_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 7, 50, tzinfo=CN))
    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=FakeRunner([]),
    )
    foreign_pid = 123456
    owner = {
        "owner": "APP_RUNTIME",
        "pid": foreign_pid,
        "project_root": str(ROOT),
    }
    controller.owner_path.write_text(json.dumps(owner), encoding="utf-8")

    monkeypatch.setattr(app_qmt_runtime, "pid_alive", lambda pid: pid == foreign_pid)
    with pytest.raises(RuntimeError, match="another live app process"):
        controller._claim_owner(clock.value)

    monkeypatch.setattr(app_qmt_runtime, "pid_alive", lambda _pid: False)
    controller._claim_owner(clock.value)
    claimed = json.loads(controller.owner_path.read_text(encoding="utf-8"))
    assert claimed["pid"] == os.getpid()
    assert claimed["owner"] == "APP_RUNTIME"
    assert claimed["live_status"] == "LIVE_DISABLED"


def test_startup_ensures_missing_qmt_then_registers_owned_jobs(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 7, 50, tzinfo=CN))
    runner = FakeRunner(
        [
            _observation(action="Status", ready=False),
            _observation(
                action="Ensure",
                ready=True,
                started_at="2026-08-03T07:50:05+08:00",
                changed=True,
            ),
        ]
    )
    controller, scheduler = _controller(tmp_path, clock=clock, runner=runner)

    controller.startup()
    controller.register_jobs()

    assert runner.actions == ["Status", "Ensure"]
    assert set(scheduler.jobs) == {QMT_DAILY_JOB_ID, QMT_MONITOR_JOB_ID}
    assert scheduler.jobs[QMT_DAILY_JOB_ID]["hour"] == 8
    assert scheduler.jobs[QMT_DAILY_JOB_ID]["minute"] == 30
    assert scheduler.jobs[QMT_DAILY_JOB_ID]["name"] == (
        "QMT 工作日启动维护（应用托管）"
    )
    assert scheduler.jobs[QMT_MONITOR_JOB_ID]["minutes"] == 1
    assert scheduler.jobs[QMT_MONITOR_JOB_ID]["name"] == ("QMT 运行状态与故障恢复监控")
    assert scheduler.jobs[QMT_DAILY_JOB_ID]["executor"] == "qmt_runtime"
    assert scheduler.jobs[QMT_MONITOR_JOB_ID]["executor"] == "qmt_runtime"
    snapshot = controller.snapshot()
    assert snapshot["contract_id"] == APP_QMT_CONTRACT_ID
    assert snapshot["execution_owner"] == "APP_RUNTIME"
    assert snapshot["ready"] is True
    assert snapshot["operationally_verified"] is True
    assert snapshot["log_retention_days"] == 30
    assert snapshot["log_max_total_bytes"] == 104857600
    assert snapshot["live_status"] == "LIVE_DISABLED"
    assert controller.owner_path.is_file()

    controller.stop()
    assert not controller.owner_path.exists()


def test_snapshot_exposes_in_flight_runtime_change(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 8, 30, tzinfo=CN))
    started = threading.Event()
    release = threading.Event()

    class BlockingRunner:
        def __call__(self, command, **_kwargs):
            started.set()
            assert release.wait(timeout=5)
            payload = _observation(
                action="Restart",
                ready=True,
                started_at="2026-08-03T08:30:05+08:00",
                changed=True,
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(payload),
                stderr="",
            )

    scheduler = FakeScheduler()
    controller = AppQmtRuntimeController(
        scheduler=scheduler,
        repository_root=ROOT,
        clock=clock,
        runner=BlockingRunner(),
        helper_script=ROOT / "ops" / "manage_qmt_runtime.ps1",
        state_path=tmp_path / "state.json",
        owner_path=tmp_path / "owner.json",
        warmup_seconds=0,
    )
    operation = threading.Thread(
        target=controller._operate,
        args=("Restart",),
        kwargs={"notify_change": False},
    )
    operation.start()
    assert started.wait(timeout=5)
    try:
        during = controller.snapshot()
        assert during["operation_in_progress"] is True
        assert during["operation_action"] == "RESTART"
        assert during["operation_started_at"] == "2026-08-03T08:30:00.000000+08:00"
    finally:
        release.set()
        operation.join(timeout=5)

    assert not operation.is_alive()
    after = controller.snapshot()
    assert after["operation_in_progress"] is False
    assert after["operation_action"] is None
    assert after["operation_started_at"] is None


def test_catchup_restarts_an_old_qmt_but_adopts_a_fresh_start(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 9, 0, tzinfo=CN))
    old_runner = FakeRunner(
        [
            _observation(
                action="Status",
                ready=True,
                started_at="2026-08-02T12:00:00+08:00",
            ),
            _observation(
                action="Restart",
                ready=True,
                started_at="2026-08-03T09:00:05+08:00",
                changed=True,
            ),
        ]
    )
    controller, _scheduler = _controller(
        tmp_path / "old", clock=clock, runner=old_runner
    )
    controller.startup()
    controller.register_jobs()

    assert old_runner.actions == ["Status", "Restart"]
    assert controller.snapshot()["daily_status"] == "SUCCEEDED"

    fresh_runner = FakeRunner(
        [
            _observation(
                action="Status",
                ready=True,
                started_at="2026-08-03T08:40:00+08:00",
            )
        ]
    )
    fresh, _scheduler = _controller(
        tmp_path / "fresh", clock=clock, runner=fresh_runner
    )
    fresh.startup()
    fresh.register_jobs()

    assert fresh_runner.actions == ["Status"]
    assert fresh.snapshot()["daily_status"] == "ADOPTED"


def test_startup_cleans_an_orphaned_qmt_installation(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 8, 2, 18, 0, tzinfo=CN))
    runner = FakeRunner(
        [
            _observation(action="Status", ready=False, process_count=1),
            _observation(
                action="Restart",
                ready=True,
                started_at="2026-08-02T18:00:05+08:00",
                changed=True,
            ),
        ]
    )
    controller, _scheduler = _controller(tmp_path, clock=clock, runner=runner)

    controller.startup()

    assert runner.actions == ["Status", "Restart"]


def test_startup_adopts_healthy_rpc_even_when_process_is_elevated(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 2, 18, 0, tzinfo=CN))
    healthy = _observation(
        action="Status",
        ready=True,
        started_at="2026-08-02T08:30:00+08:00",
        process_count=2,
    )
    healthy["automatic_control_ready"] = False
    healthy["uncontrollable_process_ids"] = [1234, 1235]
    runner = FakeRunner([healthy])
    controller, _scheduler = _controller(tmp_path, clock=clock, runner=runner)

    controller.startup()
    controller.register_jobs()

    assert runner.actions == ["Status"]
    snapshot = controller.snapshot()
    assert snapshot["ready"] is True
    assert snapshot["market_data_rpc_ready"] is True
    assert snapshot["automatic_control_ready"] is False


def test_catchup_adopts_old_healthy_elevated_qmt_without_restart(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 9, 0, tzinfo=CN))
    healthy = _observation(
        action="Status",
        ready=True,
        started_at="2026-08-02T08:30:00+08:00",
        process_count=2,
    )
    healthy["automatic_control_ready"] = False
    healthy["uncontrollable_process_ids"] = [1234, 1235]
    runner = FakeRunner([healthy])
    controller, _scheduler = _controller(tmp_path, clock=clock, runner=runner)

    controller.startup()
    controller.register_jobs()

    assert runner.actions == ["Status"]
    snapshot = controller.snapshot()
    assert snapshot["ready"] is True
    assert snapshot["daily_status"] == "ADOPTED"
    assert snapshot["daily_reason_code"] == (
        "QMT_HEALTHY_UNCONTROLLABLE_ADOPTED"
    )


def test_startup_requires_manual_restart_for_uncontrollable_broken_rpc(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 2, 18, 0, tzinfo=CN))
    broken = _observation(action="Status", ready=False, process_count=2)
    broken.update(
        reason_code="QMT_MANUAL_RESTART_REQUIRED",
        main_process_count=1,
        automatic_control_ready=False,
        uncontrollable_process_ids=[1234, 1235],
    )
    runner = FakeRunner([broken])
    controller, _scheduler = _controller(tmp_path, clock=clock, runner=runner)

    with pytest.raises(RuntimeError, match="QMT_MANUAL_RESTART_REQUIRED"):
        controller.startup()

    assert runner.actions == ["Status"]


def test_monitor_repairs_qmt_and_reconnects_native_screening(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 11, 0, tzinfo=CN))
    runner = FakeRunner(
        [
            _observation(
                action="Status",
                ready=True,
                started_at="2026-08-03T08:30:00+08:00",
            ),
            _observation(action="Status", ready=False),
            _observation(
                action="Ensure",
                ready=True,
                started_at="2026-08-03T11:00:05+08:00",
                changed=True,
            ),
        ]
    )
    callbacks: list[tuple[str, str]] = []
    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=runner,
        before_change=lambda action: callbacks.append(("before", action)),
        after_change=lambda action: callbacks.append(("after", action)),
    )
    controller.startup()
    controller.register_jobs()

    assert controller.monitor() is True
    assert runner.actions == ["Status", "Status", "Ensure"]
    assert callbacks == [("before", "ENSURE"), ("after", "ENSURE")]


def test_monitor_recovery_is_cooled_down_after_failure(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 11, 0, tzinfo=CN))
    runner = FakeRunner(
        [
            _observation(
                action="Status",
                ready=True,
                started_at="2026-08-03T08:30:00+08:00",
            ),
            _observation(action="Status", ready=False),
            _observation(action="Ensure", ready=False),
            _observation(action="Status", ready=False),
        ]
    )
    controller, _scheduler = _controller(tmp_path, clock=clock, runner=runner)
    controller.startup()
    controller.register_jobs()

    assert controller.monitor() is False
    assert controller.monitor() is False
    assert runner.actions == ["Status", "Status", "Ensure", "Status"]


def test_snapshot_fails_closed_when_process_observation_is_stale(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 11, 0, tzinfo=CN))
    runner = FakeRunner(
        [
            _observation(
                action="Status",
                ready=True,
                started_at="2026-08-03T08:30:00+08:00",
            )
        ]
    )
    controller, _scheduler = _controller(tmp_path, clock=clock, runner=runner)
    controller.startup()
    controller.register_jobs()
    assert controller.snapshot()["ready"] is True

    clock.value = datetime(2026, 8, 3, 11, 4, tzinfo=CN)
    stale = controller.snapshot()

    assert stale["ready"] is False
    assert stale["reason_code"] == "QMT_RUNTIME_OBSERVATION_STALE"
