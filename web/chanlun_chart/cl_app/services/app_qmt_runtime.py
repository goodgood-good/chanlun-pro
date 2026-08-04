"""Application-owned lifecycle management for the local QMT terminal.

The manually launched ``app.py`` process is the sole runtime owner.  This
controller starts QMT when it is absent, performs the bounded 08:30 weekday
restart, and repairs an unexpected QMT exit.  It delegates exact-process
handling to ``ops/manage_qmt_runtime.ps1`` and never touches an account or an
order transport.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, time, timedelta
import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Any
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.file_lock import (
    interprocess_file_lock,
)
from .app_runtime_owner import pid_alive


CN = ZoneInfo("Asia/Shanghai")
QMT_RUNTIME_SCHEMA = "chanlun-qmt-runtime-readiness/v1"
QMT_OBSERVATION_SCHEMA = "chanlun-qmt-app-runtime-observation/v1"
QMT_STATE_SCHEMA = "chanlun-qmt-app-runtime-state/v1"
QMT_OWNER_SCHEMA = "chanlun-qmt-execution-owner/v1"
APP_QMT_CONTRACT_ID = "chanlun-qmt-runtime/app-runtime-contract/v1"

QMT_DAILY_JOB_ID = "qmt_app_daily_restart"
QMT_MONITOR_JOB_ID = "qmt_app_runtime_monitor"

_SAFETY = {
    "real_account_accessed": False,
    "real_order_transport_enabled": False,
    "automated_order_authorized": False,
    "live_status": "LIVE_DISABLED",
}


def _canonical_at(value: datetime) -> str:
    return value.astimezone(CN).isoformat(timespec="microseconds")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        temporary.write_text(encoded + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_mapping(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(CN)


class AppQmtRuntimeController:
    """Own QMT bootstrap, daily restart and bounded recovery inside app.py."""

    def __init__(
        self,
        *,
        scheduler: Any,
        repository_root: Path,
        clock: Callable[[], datetime] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        powershell_executable: str = "powershell.exe",
        helper_script: Path | None = None,
        state_path: Path | None = None,
        owner_path: Path | None = None,
        before_change: Callable[[str], None] | None = None,
        after_change: Callable[[str], None] | None = None,
        startup_timeout_seconds: int = 120,
        warmup_seconds: int = 90,
        recovery_cooldown_seconds: int = 300,
        observation_max_age_seconds: int = 180,
    ) -> None:
        self._scheduler = scheduler
        self._root = Path(repository_root).resolve()
        self._clock = clock or (lambda: datetime.now(CN))
        self._runner = runner
        self._powershell = str(powershell_executable)
        self._helper = (
            Path(helper_script).resolve()
            if helper_script is not None
            else self._root / "ops" / "manage_qmt_runtime.ps1"
        )
        runtime_root = self._root / ".cache" / "chanlun_v3_scheduler"
        self._state_path = state_path or runtime_root / "app_qmt_runtime.json"
        self._owner_path = owner_path or runtime_root / "qmt_execution_owner.json"
        self._before_change = before_change
        self._after_change = after_change
        self._startup_timeout = int(startup_timeout_seconds)
        self._warmup = int(warmup_seconds)
        self._recovery_cooldown = int(recovery_cooldown_seconds)
        self._observation_max_age = int(observation_max_age_seconds)
        if self._startup_timeout < 10:
            raise ValueError("startup_timeout_seconds must be at least 10")
        if self._warmup < 0:
            raise ValueError("warmup_seconds must not be negative")
        if self._recovery_cooldown < 60:
            raise ValueError("recovery_cooldown_seconds must be at least 60")
        if self._observation_max_age < 60:
            raise ValueError("observation_max_age_seconds must be at least 60")
        self._operation_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._registered = False
        self._registered_at: datetime | None = None

    @property
    def owner_path(self) -> Path:
        return self._owner_path

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("QMT runtime clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("QMT runtime clock must be timezone-aware")
        return value.astimezone(CN)

    def _configuration_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self._helper.is_file():
            reasons.append("QMT_RUNTIME_HELPER_MISSING")
        if not self._powershell.strip():
            reasons.append("POWERSHELL_EXECUTABLE_UNRESOLVED")
        return reasons

    def _load_state(self) -> dict[str, object]:
        value = _read_mapping(self._state_path)
        if (
            value is None
            or value.get("schema") != QMT_STATE_SCHEMA
            or any(value.get(key) != expected for key, expected in _SAFETY.items())
        ):
            return {
                "schema": QMT_STATE_SCHEMA,
                "execution_owner": "APP_RUNTIME",
                "updated_at": None,
                "last_action": None,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_recovery_at": None,
                "daily_session": None,
                "daily_status": "PENDING",
                "daily_reason_code": "NEVER_RUN",
                "observation": None,
                "error": None,
                **_SAFETY,
            }
        return value

    def _write_state(self, value: dict[str, object], observed_at: datetime) -> None:
        value["updated_at"] = _canonical_at(observed_at)
        _atomic_json(self._state_path, value)

    def _claim_owner(self, observed_at: datetime) -> None:
        lock_path = self._owner_path.with_suffix(
            self._owner_path.suffix + ".claim.lock"
        )
        with interprocess_file_lock(lock_path, timeout_seconds=5.0):
            existing = _read_mapping(self._owner_path)
            if (
                existing is not None
                and existing.get("owner") == "APP_RUNTIME"
                and existing.get("project_root") == str(self._root)
                and existing.get("pid") != os.getpid()
                and pid_alive(existing.get("pid"))
            ):
                raise RuntimeError(
                    "another live app process owns the QMT runtime"
                )
            _atomic_json(
                self._owner_path,
                {
                    "schema": QMT_OWNER_SCHEMA,
                    "contract_id": APP_QMT_CONTRACT_ID,
                    "owner": "APP_RUNTIME",
                    "pid": os.getpid(),
                    "project_root": str(self._root),
                    "helper_script": str(self._helper),
                    "registered_at": _canonical_at(
                        self._registered_at or observed_at
                    ),
                    "heartbeat_at": _canonical_at(observed_at),
                    **_SAFETY,
                },
            )

    @staticmethod
    def _extract_observation(stdout: str) -> dict[str, object] | None:
        for line in reversed((stdout or "").splitlines()):
            try:
                value = json.loads(line.lstrip("\ufeff"))
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, Mapping)
                and value.get("schema") == QMT_OBSERVATION_SCHEMA
            ):
                return dict(value)
        return None

    def _invoke(self, action: str) -> tuple[dict[str, object], str | None]:
        command = [
            self._powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._helper),
            "-Action",
            action,
            "-StartupTimeoutSeconds",
            str(self._startup_timeout),
            "-WarmupSeconds",
            str(self._warmup),
        ]
        try:
            completed = self._runner(
                command,
                cwd=str(self._root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._startup_timeout + self._warmup + 45,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {}, f"{type(exc).__name__}: {str(exc)[:300]}"
        observation = self._extract_observation(completed.stdout)
        if observation is None:
            stderr = (completed.stderr or "").strip()[-300:]
            return {}, (
                "QMT helper returned no valid observation"
                + (f": {stderr}" if stderr else "")
            )
        error = observation.get("error")
        if completed.returncode != 0 and not error:
            error = f"QMT helper exited with code {completed.returncode}"
        return observation, None if error is None else str(error)[:300]

    def _record(
        self,
        *,
        action: str,
        observation: Mapping[str, object],
        error: str | None,
        observed_at: datetime,
        recovery: bool = False,
    ) -> None:
        with self._state_lock:
            state = self._load_state()
            state["last_action"] = action.upper()
            state["last_attempt_at"] = _canonical_at(observed_at)
            state["observation"] = dict(observation) if observation else None
            state["error"] = error
            if observation.get("ready") is True and error is None:
                state["last_success_at"] = _canonical_at(observed_at)
            if recovery:
                state["last_recovery_at"] = _canonical_at(observed_at)
            self._write_state(state, observed_at)
        self._claim_owner(observed_at)

    def _operate(
        self,
        action: str,
        *,
        notify_change: bool,
        recovery: bool = False,
    ) -> dict[str, object]:
        if not self._operation_lock.acquire(blocking=False):
            return {
                "schema": QMT_OBSERVATION_SCHEMA,
                "ready": False,
                "status": "not_ready",
                "reason_code": "QMT_RUNTIME_OPERATION_IN_PROGRESS",
                **_SAFETY,
            }
        try:
            if notify_change and action in {"Ensure", "Restart"}:
                if self._before_change is not None:
                    self._before_change(action.upper())
            observation, error = self._invoke(action)
            observed_at = self._now()
            self._record(
                action=action,
                observation=observation,
                error=error,
                observed_at=observed_at,
                recovery=recovery,
            )
            changed = bool(observation.get("changed"))
            if (
                notify_change
                and observation.get("ready") is True
                and error is None
                and (changed or action == "Restart")
                and self._after_change is not None
            ):
                self._after_change(action.upper())
            return observation
        finally:
            self._operation_lock.release()

    @staticmethod
    def _fresh_for_session(
        observation: Mapping[str, object], observed_at: datetime
    ) -> bool:
        started_at = _parse_datetime(observation.get("main_started_at"))
        threshold = datetime.combine(observed_at.date(), time(8), tzinfo=CN)
        return bool(started_at is not None and started_at >= threshold)

    def _mark_daily(
        self, *, observed_at: datetime, status: str, reason_code: str
    ) -> None:
        with self._state_lock:
            state = self._load_state()
            state["daily_session"] = observed_at.date().isoformat()
            state["daily_status"] = status
            state["daily_reason_code"] = reason_code
            self._write_state(state, observed_at)

    def startup(self) -> None:
        """Synchronously establish QMT before native screening starts."""

        reasons = self._configuration_reasons()
        if reasons:
            raise RuntimeError(reasons[0])
        observed_at = self._now()
        if self._registered_at is None:
            self._registered_at = observed_at
        self._claim_owner(observed_at)
        status = self._operate("Status", notify_change=False)
        in_catchup = bool(
            observed_at.weekday() < 5
            and time(8, 30) <= observed_at.time() <= time(10)
        )
        if in_catchup and not self._fresh_for_session(status, observed_at):
            status = self._operate("Restart", notify_change=False)
            self._mark_daily(
                observed_at=observed_at,
                status="SUCCEEDED" if status.get("ready") is True else "FAILED",
                reason_code=(
                    "QMT_DAILY_RESTART_SUCCEEDED"
                    if status.get("ready") is True
                    else "QMT_DAILY_RESTART_FAILED"
                ),
            )
        elif status.get("ready") is not True:
            recovery_action = (
                "Restart"
                if int(status.get("process_count", 0) or 0) > 0
                else "Ensure"
            )
            status = self._operate(
                recovery_action,
                notify_change=False,
                recovery=True,
            )
        elif in_catchup:
            self._mark_daily(
                observed_at=observed_at,
                status="ADOPTED",
                reason_code="QMT_FRESH_START_ADOPTED",
            )
        if status.get("ready") is not True:
            reason = str(status.get("reason_code") or "QMT_RUNTIME_NOT_READY")
            raise RuntimeError(reason)

    def register_jobs(self) -> None:
        with self._state_lock:
            if self._registered:
                return
            observed_at = self._now()
            if self._registered_at is None:
                self._registered_at = observed_at
            common = {
                "replace_existing": True,
                "coalesce": True,
                "max_instances": 1,
            }
            self._scheduler.add_job(
                self.daily_restart,
                trigger="cron",
                id=QMT_DAILY_JOB_ID,
                name="QMT weekday restart (app-owned)",
                day_of_week="mon-fri",
                hour=8,
                minute=30,
                misfire_grace_time=90 * 60,
                **common,
            )
            self._scheduler.add_job(
                self.monitor,
                trigger="interval",
                id=QMT_MONITOR_JOB_ID,
                name="QMT runtime recovery monitor",
                minutes=1,
                next_run_time=observed_at + timedelta(minutes=1),
                misfire_grace_time=60,
                **common,
            )
            self._registered = True
            self._claim_owner(observed_at)

    def daily_restart(self) -> bool:
        observed_at = self._now()
        status = self._operate("Status", notify_change=False)
        if self._fresh_for_session(status, observed_at):
            self._mark_daily(
                observed_at=observed_at,
                status="ADOPTED",
                reason_code="QMT_FRESH_START_ADOPTED",
            )
            return True
        result = self._operate("Restart", notify_change=True)
        success = result.get("ready") is True
        self._mark_daily(
            observed_at=self._now(),
            status="SUCCEEDED" if success else "FAILED",
            reason_code=(
                "QMT_DAILY_RESTART_SUCCEEDED"
                if success
                else "QMT_DAILY_RESTART_FAILED"
            ),
        )
        return success

    def monitor(self) -> bool:
        status = self._operate("Status", notify_change=False)
        if status.get("ready") is True:
            return True
        with self._state_lock:
            state = self._load_state()
        last_recovery = _parse_datetime(state.get("last_recovery_at"))
        now = self._now()
        if (
            last_recovery is not None
            and (now - last_recovery).total_seconds() < self._recovery_cooldown
        ):
            return False
        recovery_action = (
            "Restart"
            if int(status.get("process_count", 0) or 0) > 0
            else "Ensure"
        )
        result = self._operate(
            recovery_action,
            notify_change=True,
            recovery=True,
        )
        return result.get("ready") is True

    def snapshot(self) -> dict[str, object]:
        now = self._now()
        reasons = self._configuration_reasons()
        with self._state_lock:
            state = self._load_state()
        raw_observation = state.get("observation")
        observation = (
            dict(raw_observation)
            if isinstance(raw_observation, Mapping)
            else {}
        )
        configured = not reasons
        process_ready = observation.get("ready") is True
        registered = self._registered
        last_attempt = _parse_datetime(state.get("last_attempt_at"))
        observation_fresh = bool(
            last_attempt is not None
            and (now - last_attempt).total_seconds() <= self._observation_max_age
        )
        if not configured:
            reason = reasons[0]
        elif not registered:
            reason = "QMT_RUNTIME_JOBS_NOT_REGISTERED"
        elif not observation_fresh:
            reason = "QMT_RUNTIME_OBSERVATION_STALE"
        elif not process_ready:
            reason = str(
                observation.get("reason_code") or "QMT_RUNTIME_NOT_OBSERVED"
            )
        else:
            reason = "READY"
        ready = bool(
            configured and registered and observation_fresh and process_ready
        )
        registered_at = self._registered_at
        last_success = _parse_datetime(state.get("last_success_at"))
        operational = bool(
            ready
            and registered_at is not None
            and last_success is not None
            and last_success >= registered_at
        )
        return {
            "schema": QMT_RUNTIME_SCHEMA,
            "contract_id": APP_QMT_CONTRACT_ID,
            "execution_owner": "APP_RUNTIME",
            "observed_at": _canonical_at(now),
            "ready": ready,
            "status": "ready" if ready else "not_ready",
            "reason_code": reason,
            "reason_codes": [] if ready else [reason],
            "required": True,
            "configuration_ready": configured,
            "operationally_verified": operational,
            "operational_status": (
                "verified" if operational else "awaiting_first_success"
                if configured
                else "not_verified"
            ),
            "operational_reason_codes": [] if operational else [
                "AWAITING_APP_QMT_SUCCESS"
                if configured
                else reason
            ],
            "registered_at": (
                None if registered_at is None else _canonical_at(registered_at)
            ),
            "last_action": state.get("last_action"),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_success_at": state.get("last_success_at"),
            "daily_session": state.get("daily_session"),
            "daily_status": state.get("daily_status"),
            "daily_reason_code": state.get("daily_reason_code"),
            "qmt_executable": observation.get("qmt_executable"),
            "qmt_directory": observation.get("qmt_directory"),
            "log_retention_days": observation.get("log_retention_days"),
            "log_max_total_bytes": observation.get("log_max_total_bytes"),
            "main_process_count": int(
                observation.get("main_process_count", 0) or 0
            ),
            "main_started_at": observation.get("main_started_at"),
            "processes": observation.get("processes", []),
            "error": state.get("error"),
            "jobs": [
                {
                    "id": QMT_DAILY_JOB_ID,
                    "phase": "DAILY_RESTART",
                    "schedule": "MON-FRI 08:30 Asia/Shanghai",
                },
                {
                    "id": QMT_MONITOR_JOB_ID,
                    "phase": "RECOVERY_MONITOR",
                    "schedule": "EVERY 1 MINUTE",
                },
            ],
            **_SAFETY,
        }

    def stop(self) -> None:
        """Release app ownership without terminating the interactive QMT UI."""

        with self._state_lock:
            existing = _read_mapping(self._owner_path)
            if (
                existing is not None
                and existing.get("owner") == "APP_RUNTIME"
                and existing.get("pid") == os.getpid()
                and existing.get("project_root") == str(self._root)
            ):
                try:
                    self._owner_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._registered = False


__all__ = (
    "APP_QMT_CONTRACT_ID",
    "AppQmtRuntimeController",
    "QMT_DAILY_JOB_ID",
    "QMT_MONITOR_JOB_ID",
    "QMT_OBSERVATION_SCHEMA",
    "QMT_RUNTIME_SCHEMA",
)
