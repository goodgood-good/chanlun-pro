"""Validation for the app-owned forward-paper scheduler contract."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path


SCHEMA = "chanlun-forward-scheduler-readiness"
APP_RUNTIME_CONTRACT_ID = (
    "chanlun-forward-scheduler/app-runtime-contract"
)
_QMT_SCHEMA = "chanlun-qmt-runtime-readiness"
_TASKS = {
    "chanlun-app-forward-capture": "CAPTURE",
    "chanlun-app-forward-evaluate": "EVALUATE",
}


def validate_forward_scheduler_snapshot(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("forward scheduler observation must be an object")
    if (
        payload.get("schema") != SCHEMA
        or payload.get("contract_id") != APP_RUNTIME_CONTRACT_ID
    ):
        raise ValueError("forward scheduler observation identity is invalid")
    if payload.get("execution_owner") != "APP_RUNTIME":
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
        or upstream.get("schema") != _QMT_SCHEMA
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


__all__ = [
    "APP_RUNTIME_CONTRACT_ID",
    "SCHEMA",
    "validate_forward_scheduler_snapshot",
]
