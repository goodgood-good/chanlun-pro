from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

from cl_app.services import forward_scheduler as module
from cl_app.services.forward_scheduler import ForwardSchedulerProbe


def _payload(*, ready: bool) -> dict[str, object]:
    reason_codes = [] if ready else ["SCHEDULED_TASK_PRINCIPAL_MISMATCH"]
    operational_reason_codes = (
        [] if ready else ["SCHEDULED_TASK_PRINCIPAL_MISMATCH"]
    )
    tasks = [
        {
            "name": name,
            "phase": phase,
            "ready": ready,
            "configuration_ready": ready,
            "operationally_verified": ready,
            "operational_status": "verified" if ready else "not_verified",
            "status": "ready" if ready else "not_ready",
            "reason_codes": list(reason_codes),
            "operational_reason_codes": list(operational_reason_codes),
        }
        for name, phase in (
            ("Chanlun-V3-Forward-Capture", "CAPTURE"),
            ("Chanlun-V3-Forward-Evaluate", "EVALUATE"),
        )
    ]
    return {
        "schema": module.SCHEMA,
        "contract_id": module.CONTRACT_ID,
        "observed_at": "2026-07-31T09:00:00+08:00",
        "ready": ready,
        "status": "ready" if ready else "not_ready",
        "reason_code": "READY" if ready else reason_codes[0],
        "reason_codes": list(reason_codes),
        "configuration_ready": ready,
        "operationally_verified": ready,
        "operational_status": "verified" if ready else "not_verified",
        "operational_reason_codes": list(operational_reason_codes),
        "first_success_after_registration": ready,
        "registered_at": "2026-07-31T08:55:00+08:00",
        "pinned_python_executable": "D:\\software\\Python310\\python.exe",
        "upstream_qmt": {
            "schema": "chanlun-qmt-restart-scheduler-readiness/v1",
            "ready": ready,
            "status": "ready" if ready else "not_ready",
            "reason_code": "READY" if ready else "SCHEDULED_TASK_ACTION_MISMATCH",
            "reason_codes": (
                [] if ready else ["SCHEDULED_TASK_ACTION_MISMATCH"]
            ),
            "configuration_ready": ready,
            "operationally_verified": ready,
            "operational_status": "verified" if ready else "not_verified",
            "operational_reason_codes": (
                [] if ready else ["SCHEDULED_TASK_ACTION_MISMATCH"]
            ),
            "upstream_ready_now": ready,
            "upstream_reason_code": "READY" if ready else "WEB_NOT_READY_NOW",
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        },
        "tasks": tasks,
        "task_count": len(tasks),
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }


def test_probe_accepts_fail_closed_exit_and_caches_exact_observation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "audit.ps1"
    script.write_text("# read-only fixture", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments,
            3,
            stdout=json.dumps(_payload(ready=False)),
            stderr="",
        )

    monkeypatch.setattr(module.shutil, "which", lambda _name: "powershell.exe")
    clock = iter((10.0, 11.0, 12.0))
    probe = ForwardSchedulerProbe(
        audit_script=script,
        ttl_seconds=30,
        runner=runner,
        monotonic=lambda: next(clock),
    )

    first = probe.snapshot()
    first["reason_code"] = "FORGED"
    second = probe.snapshot()
    refreshed = probe.snapshot(force_refresh=True)

    assert second["ready"] is False
    assert second["reason_code"] == "SCHEDULED_TASK_PRINCIPAL_MISMATCH"
    assert refreshed["reason_code"] == "SCHEDULED_TASK_PRINCIPAL_MISMATCH"
    assert len(calls) == 2


def test_probe_rejects_relabelled_safety_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "audit.ps1"
    script.write_text("# read-only fixture", encoding="utf-8")
    forged = deepcopy(_payload(ready=True))
    forged["automated_order_authorized"] = True

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=json.dumps(forged),
            stderr="",
        )

    monkeypatch.setattr(module.shutil, "which", lambda _name: "powershell.exe")
    result = ForwardSchedulerProbe(
        audit_script=script,
        runner=runner,
    ).snapshot()

    assert result["ready"] is False
    assert result["status"] == "unresolved"
    assert result["reason_code"] == "SCHEDULED_TASK_OBSERVATION_UNAVAILABLE"
    assert result["automated_order_authorized"] is False
    assert result["live_status"] == "LIVE_DISABLED"


def test_probe_rejects_aggregate_task_verdict_mismatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "audit.ps1"
    script.write_text("# read-only fixture", encoding="utf-8")
    forged = deepcopy(_payload(ready=True))
    forged["tasks"][0]["ready"] = False
    forged["tasks"][0]["status"] = "not_ready"
    forged["tasks"][0]["reason_codes"] = ["SCHEDULED_TASK_MISSING"]

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=json.dumps(forged),
            stderr="",
        )

    monkeypatch.setattr(module.shutil, "which", lambda _name: "powershell.exe")
    result = ForwardSchedulerProbe(
        audit_script=script,
        runner=runner,
    ).snapshot()

    assert result["ready"] is False
    assert result["reason_code"] == "SCHEDULED_TASK_OBSERVATION_UNAVAILABLE"


def test_validator_distinguishes_configured_from_first_success() -> None:
    awaiting = deepcopy(_payload(ready=True))
    for task in awaiting["tasks"]:
        task["operationally_verified"] = False
        task["operational_status"] = "awaiting_first_success"
        task["operational_reason_codes"] = [
            "AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION"
        ]
    awaiting["first_success_after_registration"] = False
    awaiting["operationally_verified"] = False
    awaiting["operational_status"] = "awaiting_first_success"
    awaiting["operational_reason_codes"] = [
        "AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION"
    ]

    validated = module.validate_forward_scheduler_snapshot(awaiting)

    assert validated["ready"] is True
    assert validated["configuration_ready"] is True
    assert validated["operationally_verified"] is False
    assert validated["operational_status"] == "awaiting_first_success"
