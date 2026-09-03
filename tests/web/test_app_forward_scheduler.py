from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.forward_paper import (
    FORWARD_PAPER_CONTRACT_SCHEMA,
    FORWARD_PAPER_EVENT_SCHEMA,
    FORWARD_PAPER_LEDGER_SCHEMA,
    load_forward_contract,
    load_forward_paper_ledger,
)
from chanlun.decision_support.trading_system.trading_session import (
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
    prepare_forward_paper_ledger_contract,
)
from cl_app.services.forward_scheduler import (
    validate_forward_scheduler_snapshot,
)
from cl_app.services import app_forward_scheduler as app_forward_scheduler_subject


CN = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[2]
PARAMETER_SNAPSHOT = (
    ROOT / "config" / "decision_support" / "human_review_parameters.json"
)


def test_atomic_json_retries_transient_windows_share_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "owner.json"
    target.write_text('{"generation":1}\n', encoding="utf-8")
    original_replace = app_forward_scheduler_subject.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            error = PermissionError("temporarily opened without delete sharing")
            error.winerror = 5  # type: ignore[attr-defined]
            raise error
        original_replace(source, destination)

    monkeypatch.setattr(app_forward_scheduler_subject.os, "name", "nt")
    monkeypatch.setattr(app_forward_scheduler_subject.os, "replace", flaky_replace)

    app_forward_scheduler_subject._atomic_json(target, {"generation": 2})

    assert attempts == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 2}


def _superseded_forward_ledger() -> dict[str, object]:
    """构造一个安全但不再属于当前统一策略的历史账本。"""

    contract_stable: dict[str, object] = {
        "strategy_parameter_set_id": "sha256:" + "1" * 64,
        "strategy_parameter_snapshot_sha256": "sha256:" + "2" * 64,
        "selection_path": "SUPERSEDED_SECTOR_ONLY_PATH",
        "strategic_frequency": "30m",
        "tactical_frequency": "5m",
        "segment_difference_frequency": "1m",
        "initial_cash": "1000000",
        "slot_count": 5,
        "slot_fraction": "0.18",
        "account_exposure_cap": "0.90",
        "tactical_ratio": "0.25",
        "technical_mode": "HUMAN_REVIEW_SCREENING",
        "tick_data_used": False,
        "signal_bar_fill_allowed": False,
        "real_account_access": False,
        "real_order_transport": False,
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
        "schema": FORWARD_PAPER_CONTRACT_SCHEMA,
    }
    contract = {
        **contract_stable,
        "contract_id": sha256_json(contract_stable),
    }
    evidence = {"reason": "SUPERSEDED_BUT_AUTHENTICATED"}
    event_stable: dict[str, object] = {
        "schema": FORWARD_PAPER_EVENT_SCHEMA,
        "session": "2026-08-03",
        "recorded_at": "2026-08-03T08:00:00+08:00",
        "phase": "CONTROL",
        "status": "PAPER_STARTED",
        "contract_id": contract["contract_id"],
        "strategy_parameter_set_id": contract["strategy_parameter_set_id"],
        "previous_event_sha256": None,
        "evidence": evidence,
        "evidence_sha256": sha256_json(evidence),
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "paper_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    event = {**event_stable, "event_sha256": sha256_json(event_stable)}
    ledger_stable: dict[str, object] = {
        "schema": FORWARD_PAPER_LEDGER_SCHEMA,
        "contract": contract,
        "events": [event],
        "paper_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    return {
        **ledger_stable,
        "content_sha256": sha256_json(ledger_stable),
    }


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


def test_superseded_forward_ledger_is_verified_archived_and_rotated_once(
    tmp_path: Path,
) -> None:
    root = tmp_path / "forward"
    root.mkdir()
    ledger_path = root / "forward_paper_ledger.json"
    previous = _superseded_forward_ledger()
    previous_bytes = (
        json.dumps(previous, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    ledger_path.write_bytes(previous_bytes)

    rotated = prepare_forward_paper_ledger_contract(
        forward_root=root,
        parameter_snapshot=PARAMETER_SNAPSHOT,
    )

    assert rotated["status"] == "ROTATED"
    assert rotated["reason_code"] == "SUPERSEDED_FORWARD_CONTRACT_ARCHIVED"
    receipt = rotated["rotation"]
    assert receipt["archived_event_count"] == 1
    assert receipt["old_events_carried_forward"] is False
    archive_path = root / receipt["archived_ledger"]
    assert archive_path.read_bytes() == previous_bytes
    current_contract = load_forward_contract(PARAMETER_SNAPSHOT)
    current = load_forward_paper_ledger(ledger_path, contract=current_contract)
    assert current["events"] == ()
    assert current["contract"]["contract_id"] == current_contract.contract_id

    unchanged = prepare_forward_paper_ledger_contract(
        forward_root=root,
        parameter_snapshot=PARAMETER_SNAPSHOT,
    )
    assert unchanged["status"] == "CURRENT"
    assert unchanged["rotation"] is None
    assert archive_path.read_bytes() == previous_bytes


def test_tampered_superseded_forward_ledger_is_never_rotated(tmp_path: Path) -> None:
    root = tmp_path / "forward"
    root.mkdir()
    ledger_path = root / "forward_paper_ledger.json"
    payload = _superseded_forward_ledger()
    payload["events"][0]["evidence"]["reason"] = "FORGED"
    original = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ledger_path.write_bytes(original)

    with pytest.raises(ValueError, match="event hash changed"):
        prepare_forward_paper_ledger_contract(
            forward_root=root,
            parameter_snapshot=PARAMETER_SNAPSHOT,
        )

    assert ledger_path.read_bytes() == original
    assert not (root / "ledger_archives").exists()


def test_scheduler_contract_rotation_invalidates_prior_phase_state(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 3, 8, 0, tzinfo=CN))
    controller, _scheduler = _controller(
        tmp_path,
        clock=clock,
        runner=lambda *_args, **_kwargs: None,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": "chanlun-app-forward-runtime-state",
                "execution_owner": "APP_RUNTIME",
                "updated_at": "2026-08-03T07:00:00+08:00",
                "forward_contract_id": "sha256:" + "0" * 64,
                "phases": {
                    "CAPTURE": {
                        "phase": "CAPTURE",
                        "session": "2026-08-03",
                        "status": "SUCCEEDED",
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

    controller.register_jobs()

    snapshot = controller.snapshot()
    assert (
        snapshot["forward_contract_id"]
        == load_forward_contract(PARAMETER_SNAPSHOT).contract_id
    )
    assert snapshot["forward_ledger_contract"]["ready"] is True
    assert {task["phase_status"] for task in snapshot["tasks"]} == {"PENDING"}


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
        "统一策略前向模拟盘前快照采集（应用托管）"
    )
    assert scheduler.jobs[EVALUATE_JOB_ID]["hour"] == 15
    assert scheduler.jobs[EVALUATE_JOB_ID]["minute"] == 20
    assert scheduler.jobs[EVALUATE_JOB_ID]["name"] == (
        "统一策略前向模拟盘后评估（应用托管）"
    )
    assert scheduler.jobs[EVALUATE_JOB_ID]["misfire_grace_time"] == 8 * 60 * 60
    assert scheduler.jobs[RECONCILE_JOB_ID]["minutes"] == 5
    assert scheduler.jobs[RECONCILE_JOB_ID]["name"] == ("统一策略前向模拟失败恢复协调")
    assert scheduler.jobs[STARTUP_JOB_ID]["name"] == ("统一策略前向模拟启动一致性检查")
    assert {scheduler.jobs[job_id]["executor"] for job_id in scheduler.jobs} == {
        "forward_research"
    }

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
    assert command[command.index("--root") + 1] == str((tmp_path / "forward").resolve())
    assert command[command.index("--session") + 1] == "2026-08-03"
    assert kwargs["cwd"] == str(ROOT)
    assert kwargs["check"] is False

    snapshot = validate_forward_scheduler_snapshot(controller.snapshot())
    capture = next(task for task in snapshot["tasks"] if task["phase"] == "CAPTURE")
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
        task for task in controller.snapshot()["tasks"] if task["phase"] == "CAPTURE"
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
        task for task in controller.snapshot()["tasks"] if task["phase"] == "CAPTURE"
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
        task for task in controller.snapshot()["tasks"] if task["phase"] == "CAPTURE"
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
    evaluate = next(task for task in snapshot["tasks"] if task["phase"] == "EVALUATE")
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
        task for task in controller.snapshot()["tasks"] if task["phase"] == "EVALUATE"
    )
    assert waiting["phase_status"] == "WAITING"
    assert waiting["attempt_count"] == 0

    readiness["ready"] = True
    readiness["reason_code"] = "READY"
    clock.value = datetime(2026, 8, 3, 15, 25, tzinfo=CN)
    controller.reconcile()

    assert len(calls) == 1
    completed = next(
        task for task in controller.snapshot()["tasks"] if task["phase"] == "EVALUATE"
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
        task for task in controller.snapshot()["tasks"] if task["phase"] == "EVALUATE"
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
                "screening_scope_mode": "VALIDATION_COHORT",
                "effective_monitor_universe_limit": 12,
                "discovered_symbol_count": 12,
                "large_scope_authorized": False,
                "full_coverage_refresh_enabled": False,
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

    # A completed old full-market snapshot cannot authorize a subprocess while
    # the current app is running the default twelve-symbol validation scope.
    health["components"]["trading_screening"]["pending_symbol_count"] = 0
    health["components"]["trading_screening"]["discovered_symbol_count"] = 5086
    stale_full = evaluation_readiness_from_health(
        health,
        session=date(2026, 8, 3),
        observed_at=observed_at,
    )
    assert stale_full["ready"] is False
    assert stale_full["reason_code"] == "SCREENING_SCOPE_UNAUTHORIZED"


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
    assert {task["phase_status"] for task in snapshot["tasks"]} == {"NO_SAMPLE"}
    assert snapshot["operationally_verified"] is False
    assert snapshot["live_status"] == "LIVE_DISABLED"


def test_interrupted_running_record_is_retried_after_process_restart(
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
                "schema": "chanlun-app-forward-runtime-state",
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
