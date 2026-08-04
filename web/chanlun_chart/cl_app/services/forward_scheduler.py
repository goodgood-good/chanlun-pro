"""Read-only cached observation of the Windows forward-paper task contract."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Callable


SCHEMA = "chanlun-v3-forward-scheduler-readiness/v1"
CONTRACT_ID = "chanlun-v3-forward-scheduler/windows-task-contract/v1"
APP_RUNTIME_CONTRACT_ID = (
    "chanlun-v3-forward-scheduler/app-runtime-contract/v1"
)
_CONTRACT_IDS = {CONTRACT_ID, APP_RUNTIME_CONTRACT_ID}
_QMT_SCHEMAS = {
    "chanlun-qmt-restart-scheduler-readiness/v1",
    "chanlun-qmt-runtime-readiness/v1",
}
_TASKS = {
    "Chanlun-V3-Forward-Capture": "CAPTURE",
    "Chanlun-V3-Forward-Evaluate": "EVALUATE",
}


def _unavailable(reason: str) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "contract_id": CONTRACT_ID,
        "observed_at": None,
        "ready": False,
        "status": "unresolved",
        "reason_code": "SCHEDULED_TASK_OBSERVATION_UNAVAILABLE",
        "reason_codes": ["SCHEDULED_TASK_OBSERVATION_UNAVAILABLE"],
        "configuration_ready": False,
        "operationally_verified": False,
        "operational_status": "not_verified",
        "operational_reason_codes": [
            "SCHEDULED_TASK_OBSERVATION_UNAVAILABLE"
        ],
        "first_success_after_registration": False,
        "registered_at": None,
        "pinned_python_executable": None,
        "upstream_qmt": {
            "schema": "chanlun-qmt-restart-scheduler-readiness/v1",
            "ready": False,
            "status": "unresolved",
            "reason_code": "QMT_SCHEDULER_OBSERVATION_UNAVAILABLE",
            "reason_codes": ["QMT_SCHEDULER_OBSERVATION_UNAVAILABLE"],
            "configuration_ready": False,
            "operationally_verified": False,
            "operational_status": "not_verified",
            "operational_reason_codes": [
                "QMT_SCHEDULER_OBSERVATION_UNAVAILABLE"
            ],
            "upstream_ready_now": False,
            "upstream_reason_code": "QMT_SCHEDULER_OBSERVATION_UNAVAILABLE",
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        },
        "tasks": [],
        "task_count": 0,
        "error": reason[:200],
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }


def validate_forward_scheduler_snapshot(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("forward scheduler observation must be an object")
    if (
        payload.get("schema") != SCHEMA
        or payload.get("contract_id") not in _CONTRACT_IDS
    ):
        raise ValueError("forward scheduler observation identity is invalid")
    if (
        payload.get("contract_id") == APP_RUNTIME_CONTRACT_ID
        and payload.get("execution_owner") != "APP_RUNTIME"
    ):
        raise ValueError("app forward scheduler execution owner is invalid")
    observed_at = payload.get("observed_at")
    if not isinstance(observed_at, str):
        raise ValueError("forward scheduler observed_at is unavailable")
    observed = datetime.fromisoformat(observed_at)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("forward scheduler observed_at must be timezone-aware")
    for key, expected in (
        ("real_account_accessed", False),
        ("real_order_transport_enabled", False),
        ("automated_order_authorized", False),
        ("live_status", "LIVE_DISABLED"),
    ):
        if payload.get(key) != expected:
            raise ValueError(f"forward scheduler safety field {key} is invalid")
    ready = payload.get("ready")
    if not isinstance(ready, bool):
        raise ValueError("forward scheduler ready must be boolean")
    if payload.get("status") != ("ready" if ready else "not_ready"):
        raise ValueError("forward scheduler status is inconsistent")
    if payload.get("configuration_ready") is not ready:
        raise ValueError("forward scheduler configuration verdict is inconsistent")
    reason_code = payload.get("reason_code")
    reasons = payload.get("reason_codes")
    if not isinstance(reason_code, str) or not isinstance(reasons, list):
        raise ValueError("forward scheduler reasons are invalid")
    if any(not isinstance(value, str) or not value for value in reasons):
        raise ValueError("forward scheduler reason_codes are invalid")
    if ready:
        if reason_code != "READY" or reasons:
            raise ValueError("ready forward scheduler has failure reasons")
    elif reason_code == "READY" or reason_code not in reasons:
        raise ValueError("failed forward scheduler has no primary reason")
    operational = payload.get("operationally_verified")
    operational_status = payload.get("operational_status")
    operational_reasons = payload.get("operational_reason_codes")
    if not isinstance(operational, bool) or not isinstance(
        operational_reasons, list
    ):
        raise ValueError("forward scheduler operational verdict is invalid")
    if any(
        not isinstance(value, str) or not value
        for value in operational_reasons
    ):
        raise ValueError("forward scheduler operational reasons are invalid")
    if operational and operational_status != "verified":
        raise ValueError("forward scheduler operational status is inconsistent")
    if not operational and operational_status not in {
        "awaiting_first_success",
        "not_verified",
    }:
        raise ValueError("forward scheduler operational status is inconsistent")
    if (
        operational_status == "awaiting_first_success"
        and not {
            "AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION",
            "UPSTREAM_QMT_AWAITING_FIRST_SUCCESS",
        }.intersection(operational_reasons)
    ):
        raise ValueError("forward scheduler awaiting status is unproven")
    if operational == bool(operational_reasons):
        raise ValueError("forward scheduler operational reasons are inconsistent")
    first_success = payload.get("first_success_after_registration")
    if not isinstance(first_success, bool):
        raise ValueError("forward scheduler first-success verdict is invalid")
    registered_at = payload.get("registered_at")
    pinned_python = payload.get("pinned_python_executable")
    if ready:
        if not isinstance(registered_at, str) or not isinstance(
            pinned_python, str
        ):
            raise ValueError("ready forward scheduler registration is unavailable")
        registered = datetime.fromisoformat(registered_at)
        if registered.tzinfo is None or registered.utcoffset() is None:
            raise ValueError("forward scheduler registered_at must be timezone-aware")
        if not Path(pinned_python).is_absolute():
            raise ValueError("forward scheduler Python must be absolute")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or payload.get("task_count") != len(tasks):
        raise ValueError("forward scheduler task count is invalid")
    if {value.get("name") for value in tasks if isinstance(value, dict)} != set(_TASKS):
        raise ValueError("forward scheduler task set is invalid")
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("forward scheduler task is invalid")
        name = task.get("name")
        if task.get("phase") != _TASKS.get(name):
            raise ValueError("forward scheduler task phase is invalid")
        task_ready = task.get("ready")
        task_reasons = task.get("reason_codes")
        if not isinstance(task_ready, bool) or not isinstance(task_reasons, list):
            raise ValueError("forward scheduler task verdict is invalid")
        if task.get("status") != ("ready" if task_ready else "not_ready"):
            raise ValueError("forward scheduler task status is inconsistent")
        if task_ready != (not task_reasons):
            raise ValueError("forward scheduler task reasons are inconsistent")
        if task.get("configuration_ready") is not task_ready:
            raise ValueError("forward scheduler task configuration is inconsistent")
        task_operational = task.get("operationally_verified")
        task_operational_reasons = task.get("operational_reason_codes")
        if not isinstance(task_operational, bool) or not isinstance(
            task_operational_reasons, list
        ):
            raise ValueError("forward scheduler task operational verdict is invalid")
        if task_operational == bool(task_operational_reasons):
            raise ValueError("forward scheduler task operational reasons are inconsistent")
        expected_task_operational_status = (
            "verified"
            if task_operational
            else (
                "awaiting_first_success"
                if "AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION"
                in task_operational_reasons
                else "not_verified"
            )
        )
        if task.get("operational_status") != expected_task_operational_status:
            raise ValueError("forward scheduler task operational status is inconsistent")
    if ready != all(bool(value["ready"]) for value in tasks):
        raise ValueError("forward scheduler aggregate verdict is inconsistent")
    if first_success != all(
        bool(value["operationally_verified"]) for value in tasks
    ):
        raise ValueError("forward scheduler first-success aggregate is inconsistent")
    upstream = payload.get("upstream_qmt")
    if (
        not isinstance(upstream, dict)
        or upstream.get("schema") not in _QMT_SCHEMAS
    ):
        raise ValueError("forward scheduler QMT dependency is invalid")
    for key, expected in (
        ("real_account_accessed", False),
        ("real_order_transport_enabled", False),
        ("automated_order_authorized", False),
        ("live_status", "LIVE_DISABLED"),
    ):
        if upstream.get(key) != expected:
            raise ValueError(f"forward scheduler QMT safety field {key} is invalid")
    upstream_configuration = upstream.get("configuration_ready")
    upstream_operational = upstream.get("operationally_verified")
    upstream_now = upstream.get("upstream_ready_now")
    if not all(
        isinstance(value, bool)
        for value in (upstream_configuration, upstream_operational, upstream_now)
    ):
        raise ValueError("forward scheduler QMT verdict is invalid")
    expected_operational = bool(
        ready
        and first_success
        and upstream_configuration
        and upstream_operational
    )
    if operational != expected_operational:
        raise ValueError("forward scheduler aggregate operational verdict is inconsistent")
    return deepcopy(payload)


class ForwardSchedulerProbe:
    """Execute the read-only PowerShell audit with a short in-process TTL."""

    def __init__(
        self,
        *,
        audit_script: Path,
        ttl_seconds: float = 30.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        self._audit_script = Path(audit_script).resolve()
        self._ttl_seconds = float(ttl_seconds)
        self._runner = runner
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached_at: float | None = None
        self._cached: dict[str, object] | None = None

    def snapshot(self, *, force_refresh: bool = False) -> dict[str, object]:
        if not isinstance(force_refresh, bool):
            raise TypeError("force_refresh must be boolean")
        now = self._monotonic()
        with self._lock:
            if (
                not force_refresh
                and self._cached is not None
                and self._cached_at is not None
                and now - self._cached_at < self._ttl_seconds
            ):
                return deepcopy(self._cached)
            result = self._observe()
            self._cached = result
            self._cached_at = now
            return deepcopy(result)

    def _observe(self) -> dict[str, object]:
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if executable is None:
            return _unavailable("PowerShell executable is unavailable")
        if not self._audit_script.is_file():
            return _unavailable("forward scheduler audit script is unavailable")
        try:
            completed = self._runner(
                [
                    executable,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self._audit_script),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8-sig",
                timeout=15,
            )
            if completed.returncode not in {0, 3}:
                raise RuntimeError(
                    f"audit process exited with {completed.returncode}"
                )
            return validate_forward_scheduler_snapshot(
                json.loads(completed.stdout)
            )
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
            return _unavailable(f"{type(exc).__name__}: {str(exc)}")


__all__ = [
    "APP_RUNTIME_CONTRACT_ID",
    "CONTRACT_ID",
    "ForwardSchedulerProbe",
    "SCHEMA",
    "validate_forward_scheduler_snapshot",
]
