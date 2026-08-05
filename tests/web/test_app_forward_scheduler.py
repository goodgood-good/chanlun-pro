from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.v3_trading_session import (
    build_trading_session_evidence,
)
from cl_app.services.app_forward_scheduler import (
    APP_FORWARD_CONTRACT_ID,
    AppForwardSchedulerController,
    CAPTURE_JOB_ID,
    EVALUATE_JOB_ID,
    RECONCILE_JOB_ID,
    STARTUP_JOB_ID,
    evaluation_readiness_from_health,
)
from cl_app.services.forward_scheduler import (
    validate_forward_scheduler_snapshot,
)


CN = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[2]


class FakeScheduler:
    def __init__(self) -> None:
        self.running = True
        self.jobs: dict[str, dict[str, object]] = {}

    def add_job(self, function, **kwargs) -> None:
        self.jobs[str(kwargs["id"])] = {"function": function, **kwargs}


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _calendar(*, session: date, observed_at: datetime):
    if session.weekday() >= 5:
        return build_trading_session_evidence(
            session=session,
            observed_at=observed_at,
            query_attempted=False,
            query_succeeded=False,
        )
    return build_trading_session_evidence(
        session=session,
        observed_at=observed_at,
        returned_sessions=(session,),
        published_through=session,
        query_attempted=True,
        query_succeeded=True,
    )


def _controller(
    tmp_path: Path,
    *,
    clock: MutableClock,
    runner,
    capture_readiness_provider=None,
    evaluation_readiness_provider=None,
) -> tuple[AppForwardSchedulerController, FakeScheduler]:
    qmt = tmp_path / "qmt"
    (qmt / "Sector" / "Temple" / "GICS").mkdir(parents=True)
    scheduler = FakeScheduler()
    controller = AppForwardSchedulerController(
        scheduler=scheduler,
        repository_root=ROOT,
        forward_root=tmp_path / "forward",
        qmt_local_data_dir=qmt,
        trading_session_provider=_calendar,
        capture_readiness_provider=capture_readiness_provider,
        evaluation_readiness_provider=evaluation_readiness_provider,
        clock=clock,
        runner=runner,
        python_executable=sys.executable,
        state_path=tmp_path / "state.json",
        owner_path=tmp_path / "owner.json",
    )
    return controller, scheduler


def test_registers_exact_due_jobs_and_app_readiness_contract(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 8, 0, tzinfo=CN))
    controller, scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=lambda *_args, **_kwargs: None,
    )

    controller.register_jobs()

    assert set(scheduler.jobs) == {
        CAPTURE_JOB_ID,
        EVALUATE_JOB_ID,
        RECONCILE_JOB_ID,
        STARTUP_JOB_ID,
    }
    assert scheduler.jobs[CAPTURE_JOB_ID]["hour"] == 9
    assert scheduler.jobs[CAPTURE_JOB_ID]["minute"] == 10
    assert scheduler.jobs[CAPTURE_JOB_ID]["name"] == (
        "V3 前向模拟盘前快照采集（应用托管）"
    )
    assert scheduler.jobs[EVALUATE_JOB_ID]["hour"] == 15
    assert scheduler.jobs[EVALUATE_JOB_ID]["minute"] == 20
    assert scheduler.jobs[EVALUATE_JOB_ID]["name"] == (
        "V3 前向模拟盘后评估（应用托管）"
    )
    assert scheduler.jobs[EVALUATE_JOB_ID]["misfire_grace_time"] == 8 * 60 * 60
    assert scheduler.jobs[RECONCILE_JOB_ID]["minutes"] == 5
    assert scheduler.jobs[RECONCILE_JOB_ID]["name"] == (
        "V3 前向模拟失败恢复协调"
    )
    assert scheduler.jobs[STARTUP_JOB_ID]["name"] == (
        "V3 前向模拟启动一致性检查"
    )

    _start, evaluate_end = controller._window("EVALUATE", date(2026, 8, 3))
    assert evaluate_end == datetime(2026, 8, 3, 23, 0, tzinfo=CN)

    snapshot = validate_forward_scheduler_snapshot(controller.snapshot())
    assert snapshot["contract_id"] == APP_FORWARD_CONTRACT_ID
    assert snapshot["execution_owner"] == "APP_RUNTIME"
    assert snapshot["ready"] is True
    assert snapshot["operationally_verified"] is False
    assert snapshot["live_status"] == "LIVE_DISABLED"
    assert controller.owner_path.is_file()

    controller.stop()
    assert not controller.owner_path.exists()


def test_windows_safe_pid_probe_accepts_live_and_rejects_stale_pid() -> None:
    assert AppForwardSchedulerController._pid_alive(os.getpid()) is True
    assert AppForwardSchedulerController._pid_alive(2_147_483_647) is False
    assert AppForwardSchedulerController._pid_alive(None) is False


def test_capture_runs_fresh_cli_once_and_persists_idempotent_success(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 9, 10, tzinfo=CN))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((list(command), dict(kwargs)))
        return subprocess.CompletedProcess(command, 0, stdout='{"ok":true}', stderr="")

    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=runner,
    )
    controller.register_jobs()

    assert controller.capture_due() is True
    assert controller.capture_due() is True
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == str(Path(sys.executable).resolve())
    assert command[-3:] == ["capture", "--source", "auto"]
    assert command[command.index("--root") + 1] == str(
        (tmp_path / "forward").resolve()
    )
    assert command[command.index("--session") + 1] == "2026-08-03"
    assert kwargs["cwd"] == str(ROOT)
    assert kwargs["check"] is False

    snapshot = validate_forward_scheduler_snapshot(controller.snapshot())
    capture = next(
        task for task in snapshot["tasks"] if task["phase"] == "CAPTURE"
    )
    assert capture["phase_status"] == "SUCCEEDED"
    assert capture["attempt_count"] == 1
    assert capture["operationally_verified"] is True


def test_zero_exit_is_not_success_until_capture_event_is_observable(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 9, 10, tzinfo=CN))
    delivered = {"ready": False, "reason_code": "CAPTURE_MISSING_AFTER_DUE"}
    publish_on_next_run = {"value": False}
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(list(command))
        if publish_on_next_run["value"]:
            delivered.update(ready=True, reason_code="READY")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=runner,
        capture_readiness_provider=lambda **_kwargs: dict(delivered),
    )
    controller.register_jobs()

    assert controller.capture_due() is False
    failed = next(
        task
        for task in controller.snapshot()["tasks"]
        if task["phase"] == "CAPTURE"
    )
    assert failed["phase_status"] == "RETRY_PENDING"
    assert failed["last_run_reason_code"] == (
        "FORWARD_PHASE_POSTCONDITION_RETRY_PENDING"
    )

    publish_on_next_run["value"] = True
    clock.value = datetime(2026, 8, 3, 9, 15, tzinfo=CN)
    assert controller.capture_due() is True
    assert len(calls) == 2
    succeeded = next(
        task
        for task in controller.snapshot()["tasks"]
        if task["phase"] == "CAPTURE"
    )
    assert succeeded["phase_status"] == "SUCCEEDED"
    assert succeeded["attempt_count"] == 2


def test_restart_adopts_existing_same_session_capture_after_window(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 15, 20, tzinfo=CN))
    calls: list[object] = []
    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=lambda *args, **_kwargs: calls.append(args),
        capture_readiness_provider=lambda **_kwargs: {
            "ready": True,
            "reason_code": "READY",
        },
        evaluation_readiness_provider=lambda **_kwargs: {
            "ready": False,
            "terminal": False,
            "reason_code": "COVERAGE_PENDING",
        },
    )
    controller.register_jobs()

    controller.reconcile()

    assert calls == []
    capture = next(
        task
        for task in controller.snapshot()["tasks"]
        if task["phase"] == "CAPTURE"
    )
    assert capture["phase_status"] == "SUCCEEDED"
    assert capture["last_run_reason_code"] == "CAPTURE_ALREADY_DELIVERED"


def test_evaluate_retries_inside_frozen_window_then_succeeds(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 15, 20, tzinfo=CN))
    return_codes = iter((3, 0))
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            next(return_codes),
            stdout="gate",
            stderr="",
        )

    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=runner,
    )
    controller.register_jobs()

    assert controller.evaluate_due() is False
    clock.value = datetime(2026, 8, 3, 15, 25, tzinfo=CN)
    controller.reconcile()

    assert len(calls) == 2
    assert calls[0][-1] == "evaluate"
    snapshot = controller.snapshot()
    evaluate = next(
        task for task in snapshot["tasks"] if task["phase"] == "EVALUATE"
    )
    assert evaluate["phase_status"] == "SUCCEEDED"
    assert evaluate["attempt_count"] == 2


def test_evaluate_waits_for_shared_readiness_without_polluting_cli_ledger(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 15, 20, tzinfo=CN))
    readiness = {"ready": False, "terminal": False, "reason_code": "COVERAGE_PENDING"}
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(list(command))
        readiness.update(
            ready=True,
            already_complete=True,
            terminal=True,
            reason_code="EVALUATION_ALREADY_DELIVERED",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=runner,
        evaluation_readiness_provider=lambda **_kwargs: dict(readiness),
    )
    controller.register_jobs()

    assert controller.evaluate_due() is False
    assert calls == []
    waiting = next(
        task
        for task in controller.snapshot()["tasks"]
        if task["phase"] == "EVALUATE"
    )
    assert waiting["phase_status"] == "WAITING"
    assert waiting["attempt_count"] == 0

    readiness["ready"] = True
    readiness["reason_code"] = "READY"
    clock.value = datetime(2026, 8, 3, 15, 25, tzinfo=CN)
    controller.reconcile()

    assert len(calls) == 1
    completed = next(
        task
        for task in controller.snapshot()["tasks"]
        if task["phase"] == "EVALUATE"
    )
    assert completed["phase_status"] == "SUCCEEDED"


def test_existing_evaluation_delivery_is_adopted_without_cli_replay(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 15, 20, tzinfo=CN))
    calls: list[object] = []
    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=lambda *args, **_kwargs: calls.append(args),
        evaluation_readiness_provider=lambda **_kwargs: {
            "ready": True,
            "already_complete": True,
            "terminal": True,
            "reason_code": "EVALUATION_ALREADY_DELIVERED",
        },
    )
    controller.register_jobs()

    assert controller.evaluate_due() is True
    assert calls == []
    completed = next(
        task
        for task in controller.snapshot()["tasks"]
        if task["phase"] == "EVALUATE"
    )
    assert completed["phase_status"] == "SUCCEEDED"


def test_shared_health_gate_requires_close_coverage_and_archive() -> None:
    observed_at = datetime(2026, 8, 3, 15, 20, tzinfo=CN)
    health = {
        "status": "ready",
        "components": {
            "trading_screening": {
                "ready": True,
                "market_data_as_of": "2026-08-03T15:00:00+08:00",
                "coverage_cycle_complete": True,
                "pending_symbol_count": 0,
            },
            "forward_archive": {"ready": True, "reason_code": "READY"},
            "forward_delivery": {
                "evaluation_ready": False,
                "reason_code": "EVALUATION_PENDING",
            },
        },
    }

    ready = evaluation_readiness_from_health(
        health,
        session=date(2026, 8, 3),
        observed_at=observed_at,
    )
    assert ready["ready"] is True
    assert ready["reason_code"] == "READY"

    health["components"]["trading_screening"]["pending_symbol_count"] = 1
    waiting = evaluation_readiness_from_health(
        health,
        session=date(2026, 8, 3),
        observed_at=observed_at,
    )
    assert waiting["ready"] is False
    assert waiting["reason_code"] == "SCREENING_COVERAGE_PENDING"
    assert waiting["terminal"] is False


def test_shared_health_gate_makes_missing_capture_terminal() -> None:
    verdict = evaluation_readiness_from_health(
        {
            "status": "not_ready",
            "components": {
                "forward_delivery": {
                    "evaluation_ready": False,
                    "reason_code": "CAPTURE_MISSING_AFTER_DUE",
                }
            },
        },
        session=date(2026, 8, 3),
        observed_at=datetime(2026, 8, 3, 15, 20, tzinfo=CN),
    )

    assert verdict == {
        "ready": False,
        "terminal": True,
        "reason_code": "CAPTURE_MISSING_AFTER_DUE",
    }


def test_non_trading_session_records_no_sample_without_running_tool(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 2, 16, 0, tzinfo=CN))
    calls: list[object] = []
    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=lambda *args, **_kwargs: calls.append(args),
    )
    controller.register_jobs()

    controller.reconcile()

    assert calls == []
    snapshot = controller.snapshot()
    assert {task["phase_status"] for task in snapshot["tasks"]} == {
        "NO_SAMPLE"
    }
    assert snapshot["operationally_verified"] is False
    assert snapshot["live_status"] == "LIVE_DISABLED"


def test_legacy_running_record_is_retried_after_process_restart(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 9, 10, tzinfo=CN))
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=runner,
    )
    controller.register_jobs()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": "chanlun-v3-app-forward-runtime-state/v1",
                "execution_owner": "APP_RUNTIME",
                "updated_at": None,
                "phases": {
                    "CAPTURE": {
                        "phase": "CAPTURE",
                        "session": "2026-08-03",
                        "status": "RUNNING",
                        "reason_code": "FORWARD_PHASE_RUNNING",
                        "attempt_count": 1,
                        "last_attempt_at": "2026-08-03T09:10:00+08:00",
                        "completed_at": None,
                        "result_code": None,
                    }
                },
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }
        ),
        encoding="utf-8",
    )

    assert controller.capture_due() is True
    assert len(calls) == 1
