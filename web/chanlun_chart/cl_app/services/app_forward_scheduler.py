"""由应用进程托管统一策略人工复核前向模拟调度。

长期运行的 ``app.py`` 是业务调度的唯一所有者。

每个阶段仍通过全新的 Python 进程运行 ``tools/run_forward_paper.py``，以保留
工具的实现来源证明，并保证页面、历史回放和前向样本共用同一冻结决策核心。
本控制器不会打开账户，也不会启用订单通道。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.forward_paper import (
    FORWARD_PAPER_CONTRACT_SCHEMA,
    FORWARD_PAPER_LEDGER_SCHEMA,
    load_forward_contract,
    load_forward_paper_ledger,
    validate_forward_paper_ledger,
)
from chanlun.decision_support.trading_system.file_lock import (
    InterprocessLockTimeout,
    interprocess_file_lock,
)
from chanlun.decision_support.trading_system.trading_session import (
    resolve_trading_session_requirement,
)
from .app_runtime_owner import pid_alive
from .job_names import JOB_DISPLAY_NAMES


CN = ZoneInfo("Asia/Shanghai")
APP_FORWARD_CONTRACT_ID = (
    "chanlun-forward-scheduler/app-runtime-contract"
)
APP_FORWARD_STATE_SCHEMA = "chanlun-app-forward-runtime-state"
APP_FORWARD_OWNER_SCHEMA = "chanlun-forward-execution-owner"
FORWARD_READINESS_SCHEMA = "chanlun-forward-scheduler-readiness"
QMT_RUNTIME_SCHEMA = "chanlun-qmt-runtime-readiness"
FORWARD_LEDGER_ROTATION_SCHEMA = "chanlun-forward-paper-ledger-contract-rotation"

CAPTURE_JOB_ID = "forward_capture"
EVALUATE_JOB_ID = "forward_evaluate"
RECONCILE_JOB_ID = "forward_reconcile"
STARTUP_JOB_ID = "forward_startup_reconcile"

_TASKS = {
    "CAPTURE": "chanlun-app-forward-capture",
    "EVALUATE": "chanlun-app-forward-evaluate",
}
_TERMINAL_PHASE_STATES = {"SUCCEEDED", "NO_SAMPLE"}
_SAFETY = {
    "real_account_accessed": False,
    "real_order_transport_enabled": False,
    "automated_order_authorized": False,
    "live_status": "LIVE_DISABLED",
}


def _is_sha256(value: object) -> bool:
    text = value if isinstance(value, str) else ""
    if not text.startswith("sha256:") or len(text) != 71:
        return False
    try:
        int(text[7:], 16)
    except ValueError:
        return False
    return True


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class _StoredForwardContract:
    """只为校验历史哈希链提供其原始、不可变的契约文档。"""

    def __init__(self, document: Mapping[str, object]) -> None:
        self._document = dict(document)
        self.contract_id = str(document["contract_id"])
        self.strategy_parameter_set_id = str(
            document["strategy_parameter_set_id"]
        )

    @property
    def operational_status(self) -> str:
        return "REVIEW_REQUIRED"

    def document(self) -> dict[str, object]:
        return dict(self._document)


def _stored_forward_contract(
    document: Mapping[str, object],
) -> _StoredForwardContract:
    """认证历史契约自身，不把它重新解释成当前策略。"""

    stable = {key: value for key, value in document.items() if key != "contract_id"}
    contract_id = document.get("contract_id")
    parameter_set_id = document.get("strategy_parameter_set_id")
    if (
        document.get("schema") != FORWARD_PAPER_CONTRACT_SCHEMA
        or not _is_sha256(contract_id)
        or contract_id != sha256_json(stable)
        or not _is_sha256(parameter_set_id)
        or document.get("real_account_access") is not False
        or document.get("real_order_transport") is not False
        or document.get("tick_data_used") is not False
        or document.get("signal_bar_fill_allowed") is not False
        or document.get("highest_status") != "REVIEW_REQUIRED"
        or document.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("superseded forward paper contract is invalid")
    return _StoredForwardContract(document)


def _validate_superseded_forward_ledger(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """复用正式账本校验器认证旧链；这里只替换契约读取视角。"""

    raw_contract = payload.get("contract")
    if not isinstance(raw_contract, Mapping):
        raise ValueError("superseded forward paper contract is unavailable")
    return validate_forward_paper_ledger(
        payload,
        contract=_stored_forward_contract(raw_contract),
    )


def _empty_forward_ledger(contract: object) -> dict[str, object]:
    stable: dict[str, object] = {
        "schema": FORWARD_PAPER_LEDGER_SCHEMA,
        "contract": contract.document(),
        "events": [],
        "paper_status": contract.operational_status,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def prepare_forward_paper_ledger_contract(
    *,
    forward_root: Path,
    parameter_snapshot: Path,
) -> dict[str, object]:
    """让活动账本只属于当前契约，并完整保留已认证的旧哈希链。

    契约不同时不能把旧事件接到新链上。函数先使用正式校验器验证旧链，再把原始
    字节写入内容寻址归档，最后原子发布当前契约的空账本。损坏账本绝不轮换。
    """

    root = Path(forward_root).resolve()
    ledger_path = root / "forward_paper_ledger.json"
    contract = load_forward_contract(Path(parameter_snapshot).resolve())
    result: dict[str, object] = {
        "ready": True,
        "status": "CURRENT",
        "reason_code": "CURRENT_FORWARD_CONTRACT",
        "current_contract_id": contract.contract_id,
        "rotation": None,
        **_SAFETY,
    }
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with interprocess_file_lock(lock_path):
        if not ledger_path.is_file():
            return result
        try:
            raw = ledger_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("forward paper ledger cannot be read") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("forward paper ledger document is invalid")
        if payload.get("contract") == contract.document():
            load_forward_paper_ledger(ledger_path, contract=contract)
            return result

        validated = _validate_superseded_forward_ledger(payload)
        archived_content_sha256 = str(validated["content_sha256"])
        archived_contract = validated["contract"]
        assert isinstance(archived_contract, Mapping)
        archive_root = root / "ledger_archives"
        archive_path = archive_root / f"{archived_content_sha256[7:]}.json"
        if archive_path.is_file():
            if archive_path.read_bytes() != raw:
                raise RuntimeError("immutable forward ledger archive changed")
        else:
            _atomic_bytes(archive_path, raw)
        archived_payload = json.loads(archive_path.read_text(encoding="utf-8"))
        if not isinstance(archived_payload, Mapping):
            raise RuntimeError("immutable forward ledger archive is invalid")
        archived_validated = _validate_superseded_forward_ledger(archived_payload)
        if archived_validated["content_sha256"] != archived_content_sha256:
            raise RuntimeError("immutable forward ledger archive identity changed")

        receipt_stable: dict[str, object] = {
            "schema": FORWARD_LEDGER_ROTATION_SCHEMA,
            "archived_ledger": archive_path.relative_to(root).as_posix(),
            "archived_ledger_file_sha256": _sha256_bytes(raw),
            "archived_ledger_content_sha256": archived_content_sha256,
            "archived_contract_id": archived_contract["contract_id"],
            "archived_event_count": len(validated["events"]),
            "current_contract_id": contract.contract_id,
            "old_events_carried_forward": False,
            **_SAFETY,
        }
        receipt = {
            **receipt_stable,
            "content_sha256": sha256_json(receipt_stable),
        }
        receipt_path = archive_root / (
            f"rotation-{str(receipt['content_sha256'])[7:]}.json"
        )
        if receipt_path.is_file():
            existing_receipt = _read_mapping(receipt_path)
            if existing_receipt != receipt:
                raise RuntimeError("immutable forward ledger rotation receipt changed")
        else:
            _atomic_json(receipt_path, receipt)

        current = _empty_forward_ledger(contract)
        validate_forward_paper_ledger(current, contract=contract)
        _atomic_json(ledger_path, current)
        load_forward_paper_ledger(ledger_path, contract=contract)
        return {
            **result,
            "status": "ROTATED",
            "reason_code": "SUPERSEDED_FORWARD_CONTRACT_ARCHIVED",
            "rotation": {
                **receipt,
                "receipt": receipt_path.relative_to(root).as_posix(),
            },
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


def discover_qmt_local_data_dir(
    configured: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Resolve the explicitly configured read-only QMT ``datadir``."""

    raw = configured or os.environ.get("CHANLUN_QMT_LOCAL_DATA_DIR")
    if raw is not None and str(raw).strip():
        candidate = Path(raw).expanduser().resolve()
        gics = candidate / "Sector" / "Temple" / "GICS"
        if not candidate.is_dir() or not gics.is_dir():
            raise FileNotFoundError(
                f"configured QMT local data directory is invalid: {candidate}"
            )
        return candidate

    return None


def evaluation_readiness_from_health(
    payload: Mapping[str, object],
    *,
    session: date,
    observed_at: datetime,
) -> dict[str, object]:
    """Apply the former PowerShell close/coverage/archive wait contract."""

    raw_components = payload.get("components")
    components = raw_components if isinstance(raw_components, Mapping) else {}
    raw_screening = components.get("trading_screening")
    raw_archive = components.get("forward_archive")
    raw_delivery = components.get("forward_delivery")
    screening = raw_screening if isinstance(raw_screening, Mapping) else {}
    archive = raw_archive if isinstance(raw_archive, Mapping) else {}
    delivery = raw_delivery if isinstance(raw_delivery, Mapping) else {}
    if delivery.get("evaluation_ready") is True:
        return {
            "ready": True,
            "already_complete": True,
            "terminal": True,
            "reason_code": "EVALUATION_ALREADY_DELIVERED",
        }
    delivery_reason = str(
        delivery.get("reason_code")
        or "FORWARD_DELIVERY_READINESS_UNAVAILABLE"
    )
    if delivery_reason in {
        "CAPTURE_IMPLEMENTATION_PROVENANCE_UNATTESTED",
        "IMPLEMENTATION_CHANGED_SINCE_CAPTURE",
        "CURRENT_IMPLEMENTATION_PROVENANCE_UNAVAILABLE",
        "CAPTURE_MISSING_AFTER_DUE",
    }:
        return {
            "ready": False,
            "terminal": True,
            "reason_code": delivery_reason,
        }
    try:
        market_cutoff = datetime.fromisoformat(
            str(screening.get("market_data_as_of"))
        )
        if market_cutoff.tzinfo is None or market_cutoff.utcoffset() is None:
            raise ValueError("market cutoff must be timezone-aware")
        market_cutoff = market_cutoff.astimezone(CN)
        decision_close = datetime.combine(session, time(15), tzinfo=CN)
        cutoff_ready = market_cutoff >= decision_close
    except (TypeError, ValueError):
        cutoff_ready = False
    try:
        pending_symbol_count = int(screening.get("pending_symbol_count", -1))
    except (TypeError, ValueError):
        pending_symbol_count = -1
    coverage_ready = bool(
        screening.get("coverage_cycle_complete") is True
        and pending_symbol_count == 0
    )
    ready = bool(
        payload.get("status") == "ready"
        and screening.get("ready") is True
        and coverage_ready
        and archive.get("ready") is True
        and cutoff_ready
    )
    if ready:
        reason = "READY"
    elif not cutoff_ready:
        reason = "MARKET_CLOSE_DATA_PENDING"
    elif not coverage_ready:
        reason = "SCREENING_COVERAGE_PENDING"
    elif archive.get("ready") is not True:
        reason = str(archive.get("reason_code") or "FORWARD_ARCHIVE_PENDING")
    else:
        reason = "APPLICATION_READINESS_PENDING"
    return {
        "ready": ready,
        "already_complete": False,
        "terminal": False,
        "reason_code": reason,
        "observed_at": _canonical_at(observed_at),
    }


class AppForwardSchedulerController:
    """Own Capture/Evaluate scheduling inside the running web application."""

    def __init__(
        self,
        *,
        scheduler: Any,
        repository_root: Path,
        forward_root: Path | None = None,
        qmt_local_data_dir: Path | None,
        trading_session_provider: Callable[..., Mapping[str, object] | None],
        capture_readiness_provider: (
            Callable[..., Mapping[str, object]] | None
        ) = None,
        evaluation_readiness_provider: (
            Callable[..., Mapping[str, object]] | None
        ) = None,
        clock: Callable[[], datetime] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        python_executable: str | os.PathLike[str] | None = None,
        parameter_snapshot: Path | None = None,
        state_path: Path | None = None,
        owner_path: Path | None = None,
        capture_timeout_seconds: int = 1800,
        evaluate_timeout_seconds: int = 7200,
    ) -> None:
        self._scheduler = scheduler
        self._root = Path(repository_root).resolve()
        self._forward_root = (
            Path(forward_root).resolve()
            if forward_root is not None
            else (
                self._root
                / ".cache"
                / "chanlun_human_review_forward"
            ).resolve()
        )
        self._qmt = (
            None
            if qmt_local_data_dir is None
            else Path(qmt_local_data_dir).resolve()
        )
        self._calendar = trading_session_provider
        self._capture_readiness = capture_readiness_provider
        self._evaluation_readiness = evaluation_readiness_provider
        self._clock = clock or (lambda: datetime.now(CN))
        self._runner = runner
        self._python = Path(python_executable or sys.executable).resolve()
        self._parameter_snapshot = (
            Path(parameter_snapshot).resolve()
            if parameter_snapshot is not None
            else (
                self._root
                / "config"
                / "decision_support"
                / "human_review_parameters.json"
            ).resolve()
        )
        self._forward_contract_id: str | None = None
        self._ledger_contract_preparation: dict[str, object] | None = None
        self._ledger_contract_error: str | None = None
        runtime_root = self._root / ".cache" / "chanlun_scheduler"
        self._state_path = state_path or runtime_root / "app_forward_runtime.json"
        self._owner_path = owner_path or runtime_root / "forward_execution_owner.json"
        self._execution_lock_path = runtime_root / "app_forward_execution.lock"
        self._capture_timeout = int(capture_timeout_seconds)
        self._evaluate_timeout = int(evaluate_timeout_seconds)
        self._state_lock = threading.RLock()
        self._attempt_lock = threading.Lock()
        self._registered = False
        self._registered_at: datetime | None = None

    @property
    def owner_path(self) -> Path:
        return self._owner_path

    @property
    def forward_root(self) -> Path:
        return self._forward_root

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("forward scheduler clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("forward scheduler clock must be timezone-aware")
        return value.astimezone(CN)

    def _resource_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self._registered:
            reasons.append("APP_FORWARD_JOBS_NOT_REGISTERED")
        if not self._python.is_file():
            reasons.append("PINNED_PYTHON_UNAVAILABLE")
        if not (self._root / "tools" / "run_forward_paper.py").is_file():
            reasons.append("FORWARD_TOOL_UNAVAILABLE")
        if self._qmt is None or not (
            self._qmt / "Sector" / "Temple" / "GICS"
        ).is_dir():
            reasons.append("QMT_LOCAL_DATA_DIRECTORY_UNAVAILABLE")
        if self._registered and not bool(getattr(self._scheduler, "running", False)):
            reasons.append("APP_SCHEDULER_NOT_RUNNING")
        if self._ledger_contract_error is not None:
            reasons.append("FORWARD_LEDGER_CONTRACT_UNRESOLVED")
        return reasons

    def _owner_payload(self, observed_at: datetime) -> dict[str, object]:
        return {
            "schema": APP_FORWARD_OWNER_SCHEMA,
            "owner": "APP_RUNTIME",
            "pid": os.getpid(),
            "project_root": str(self._root),
            "forward_root": str(self._forward_root),
            "application_entrypoint": str(
                self._root / "web" / "chanlun_chart" / "app.py"
            ),
            "registered_at": (
                _canonical_at(self._registered_at)
                if self._registered_at is not None
                else _canonical_at(observed_at)
            ),
            "heartbeat_at": _canonical_at(observed_at),
            **_SAFETY,
        }

    @staticmethod
    def _pid_alive(pid: object) -> bool:
        return pid_alive(pid)

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
                and self._pid_alive(existing.get("pid"))
            ):
                raise RuntimeError(
                    "another live app process owns strict strategy forward scheduling"
                )
            _atomic_json(self._owner_path, self._owner_payload(observed_at))

    def _heartbeat(self, observed_at: datetime) -> None:
        if self._registered:
            _atomic_json(self._owner_path, self._owner_payload(observed_at))

    def register_jobs(self) -> None:
        """注册两个固定时点任务，以及一个有界的五分钟恢复任务。"""

        with self._state_lock:
            if self._registered:
                return
            observed_at = self._now()
            try:
                preparation = prepare_forward_paper_ledger_contract(
                    forward_root=self._forward_root,
                    parameter_snapshot=self._parameter_snapshot,
                )
                self._ledger_contract_preparation = preparation
                self._forward_contract_id = str(preparation["current_contract_id"])
                self._ledger_contract_error = None
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._ledger_contract_preparation = None
                self._forward_contract_id = None
                self._ledger_contract_error = (
                    f"{type(exc).__name__}: {str(exc)[:240]}"
                )
            self._registered_at = observed_at
            self._claim_owner(observed_at)
            common = {
                "replace_existing": True,
                "coalesce": True,
                "max_instances": 1,
            }
            self._scheduler.add_job(
                self.capture_due,
                trigger="cron",
                id=CAPTURE_JOB_ID,
                name=JOB_DISPLAY_NAMES[CAPTURE_JOB_ID],
                day_of_week="mon-fri",
                hour=9,
                minute=10,
                misfire_grace_time=15 * 60,
                **common,
            )
            self._scheduler.add_job(
                self.evaluate_due,
                trigger="cron",
                id=EVALUATE_JOB_ID,
                name=JOB_DISPLAY_NAMES[EVALUATE_JOB_ID],
                day_of_week="mon-fri",
                hour=15,
                minute=20,
                # 全市场盘后筛选在 23:00 的次日预选边界前仍然有效。覆盖仍在
                # 推进时，不应沿用旧的 19:20 截止时间让恢复任务提前失效。
                misfire_grace_time=8 * 60 * 60,
                **common,
            )
            self._scheduler.add_job(
                self.reconcile,
                trigger="interval",
                id=RECONCILE_JOB_ID,
                name=JOB_DISPLAY_NAMES[RECONCILE_JOB_ID],
                minutes=5,
                next_run_time=observed_at + timedelta(minutes=5),
                misfire_grace_time=5 * 60,
                **common,
            )
            self._scheduler.add_job(
                self.reconcile,
                trigger="date",
                id=STARTUP_JOB_ID,
                name=JOB_DISPLAY_NAMES[STARTUP_JOB_ID],
                run_date=observed_at + timedelta(seconds=3),
                misfire_grace_time=5 * 60,
                **common,
            )
            self._registered = True
            # 注册标记切换后重新写入，使健康检查与状态迁移门看到一致的所有权声明。
            self._heartbeat(observed_at)

    def stop(self) -> None:
        """Release only this process's execution-owner receipt."""

        with self._state_lock:
        # ``scheduler.shutdown(wait=False)`` 用于限制 Web 停机等待时间。
        # 若子阶段仍在运行，则保留所有者标记直到进程退出，避免第二个调度器
        # 与仍活跃的子任务重叠执行。
            if self._attempt_lock.locked():
                self._registered = False
                return
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

    def _load_state(self) -> dict[str, object]:
        value = _read_mapping(self._state_path)
        if (
            value is None
            or value.get("schema") != APP_FORWARD_STATE_SCHEMA
            or value.get("execution_owner") != "APP_RUNTIME"
            or value.get("forward_contract_id") != self._forward_contract_id
            or any(value.get(key) != expected for key, expected in _SAFETY.items())
            or not isinstance(value.get("phases"), Mapping)
        ):
            return {
                "schema": APP_FORWARD_STATE_SCHEMA,
                "execution_owner": "APP_RUNTIME",
                "updated_at": None,
                "forward_contract_id": self._forward_contract_id,
                "phases": {},
                **_SAFETY,
            }
        return value

    def _write_state(self, value: dict[str, object], observed_at: datetime) -> None:
        value["updated_at"] = _canonical_at(observed_at)
        value["forward_contract_id"] = self._forward_contract_id
        _atomic_json(self._state_path, value)

    def _phase_record(
        self, state: Mapping[str, object], phase: str, session: date
    ) -> dict[str, object] | None:
        phases = state.get("phases")
        raw = phases.get(phase) if isinstance(phases, Mapping) else None
        if not isinstance(raw, Mapping) or raw.get("session") != session.isoformat():
            return None
        return dict(raw)

    def _calendar_requirement(
        self, session: date, observed_at: datetime
    ) -> dict[str, object]:
        try:
            evidence = self._calendar(
                session=session,
                observed_at=observed_at,
            )
        except Exception:
            evidence = None
        return resolve_trading_session_requirement(
            evidence,
            session=session,
            observed_at=observed_at,
        )

    def _adopt_existing_capture(
        self, *, session: date, observed_at: datetime
    ) -> bool:
        if self._capture_readiness is None:
            return False
        try:
            readiness = dict(
                self._capture_readiness(
                    session=session,
                    observed_at=observed_at,
                )
            )
        except Exception:
            return False
        if readiness.get("ready") is not True:
            return False
        self._set_terminal_without_run(
            "CAPTURE",
            observed_at=observed_at,
            status="SUCCEEDED",
            reason_code="CAPTURE_ALREADY_DELIVERED",
        )
        return True

    def _set_terminal_without_run(
        self,
        phase: str,
        *,
        observed_at: datetime,
        status: str,
        reason_code: str,
    ) -> None:
        with self._state_lock:
            state = self._load_state()
            session = observed_at.date()
            existing = self._phase_record(state, phase, session)
            if existing is not None and existing.get("status") in _TERMINAL_PHASE_STATES:
                return
            phases = dict(state.get("phases", {}))
            phases[phase] = {
                "phase": phase,
                "session": session.isoformat(),
                "status": status,
                "reason_code": reason_code,
                "attempt_count": int((existing or {}).get("attempt_count", 0)),
                "last_attempt_at": (existing or {}).get("last_attempt_at"),
                "completed_at": _canonical_at(observed_at),
                "result_code": (existing or {}).get("result_code"),
            }
            state["phases"] = phases
            self._write_state(state, observed_at)

    @staticmethod
    def _window(phase: str, session: date) -> tuple[datetime, datetime]:
        if phase == "CAPTURE":
            return (
                datetime.combine(session, time(9, 10), CN),
                datetime.combine(session, time(9, 25), CN),
            )
        if phase == "EVALUATE":
            return (
                datetime.combine(session, time(15, 20), CN),
                datetime.combine(session, time(23, 0), CN),
            )
        raise ValueError(f"unsupported forward phase: {phase}")

    def _command(self, phase: str, session: date) -> list[str]:
        command = [
            str(self._python),
            str(self._root / "tools" / "run_forward_paper.py"),
            "--root",
            str(self._forward_root),
            "--qmt-local-data-dir",
            str(self._qmt),
            "--session",
            session.isoformat(),
        ]
        if phase == "CAPTURE":
            return [*command, "capture", "--source", "auto"]
        if phase == "EVALUATE":
            return [*command, "evaluate"]
        raise ValueError(f"unsupported forward phase: {phase}")

    def _delivery_postcondition(
        self,
        phase: str,
        *,
        session: date,
        observed_at: datetime,
    ) -> tuple[bool, str | None]:
        """Verify that a zero-exit child made its durable event observable.

        A subprocess exit code only proves that the CLI reached its normal
        return path.  The scheduler must additionally prove that the exact
        ledger consumed by the application contains the phase event.  This
        catches path/configuration drift and prevents a successful child from
        being reported as a delivered Capture/Evaluate phase.
        """

        provider = (
            self._capture_readiness
            if phase == "CAPTURE"
            else self._evaluation_readiness
        )
        if provider is None:
            # 与主机无关的单元测试以及明确禁用的监控没有共享就绪状态提供方；
            # 生产环境始终具备该提供方。
            return True, None
        try:
            readiness = dict(
                provider(session=session, observed_at=observed_at)
            )
        except Exception as exc:
            return False, (
                "DELIVERY_POSTCONDITION_UNAVAILABLE:"
                f"{type(exc).__name__}"
            )
        if phase == "CAPTURE":
            delivered = readiness.get("ready") is True
        else:
            delivered = readiness.get("already_complete") is True
        return delivered, str(
            readiness.get("reason_code")
            or (
                "DELIVERY_EVENT_OBSERVED"
                if delivered
                else f"{phase}_DELIVERY_EVENT_MISSING"
            )
        )

    @staticmethod
    def _bounded_process_evidence(value: str | None) -> tuple[str, str]:
        text = value or ""
        digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        return digest, text[-2000:]

    def _attempt(self, phase: str, *, observed_at: datetime | None = None) -> bool:
        now = self._now() if observed_at is None else observed_at.astimezone(CN)
        if not self._attempt_lock.acquire(blocking=False):
            return False
        try:
            try:
                execution_lock = interprocess_file_lock(
                    self._execution_lock_path,
                    timeout_seconds=1.0,
                )
                execution_lock.__enter__()
            except InterprocessLockTimeout:
                return False
            try:
                self._heartbeat(now)
                session = now.date()
                requirement = self._calendar_requirement(session, now)
                if requirement.get("required") is False:
                    self._set_terminal_without_run(
                        phase,
                        observed_at=now,
                        status="NO_SAMPLE",
                        reason_code="NON_TRADING_SESSION_NOT_DUE",
                    )
                    return True
                if requirement.get("required") is not True:
                    self._set_terminal_without_run(
                        phase,
                        observed_at=now,
                        status="BLOCKED",
                        reason_code="TRADING_SESSION_REQUIREMENT_UNRESOLVED",
                    )
                    return False
                start, end = self._window(phase, session)
                if now < start or now > end:
                    return False
                resource_reasons = self._resource_reasons()
                if resource_reasons:
                    self._set_terminal_without_run(
                        phase,
                        observed_at=now,
                        status="BLOCKED",
                        reason_code=resource_reasons[0],
                    )
                    return False
                with self._state_lock:
                    current_state = self._load_state()
                    current_record = self._phase_record(
                        current_state, phase, session
                    )
                    if (
                        current_record is not None
                        and current_record.get("status")
                        in _TERMINAL_PHASE_STATES
                    ):
                        return True
                if phase == "CAPTURE" and self._adopt_existing_capture(
                    session=session,
                    observed_at=now,
                ):
                    return True
                if phase == "EVALUATE" and self._evaluation_readiness is not None:
                    try:
                        readiness = dict(
                            self._evaluation_readiness(
                                session=session,
                                observed_at=now,
                            )
                        )
                    except Exception as exc:
                        readiness = {
                            "ready": False,
                            "terminal": False,
                            "reason_code": (
                                "EVALUATION_READINESS_UNAVAILABLE:"
                                f"{type(exc).__name__}"
                            ),
                        }
                    if readiness.get("already_complete") is True:
                        self._set_terminal_without_run(
                            phase,
                            observed_at=now,
                            status="SUCCEEDED",
                            reason_code="EVALUATION_ALREADY_DELIVERED",
                        )
                        return True
                    if readiness.get("ready") is not True:
                        next_retry = now + timedelta(minutes=5)
                        terminal = bool(readiness.get("terminal")) or (
                            next_retry > end
                        )
                        self._set_terminal_without_run(
                            phase,
                            observed_at=now,
                            status="BLOCKED" if terminal else "WAITING",
                            reason_code=str(
                                readiness.get("reason_code")
                                or "EVALUATION_READINESS_PENDING"
                            ),
                        )
                        return False
                with self._state_lock:
                    state = self._load_state()
                    existing = self._phase_record(state, phase, session)
            # 跨进程锁已经排除了仍存活的执行。持久化的 RUNNING 记录只能是
            # 崩溃残留，必须重试，不能让当天任务永久卡死。
                    if existing is not None and existing.get("status") in (
                        _TERMINAL_PHASE_STATES
                    ):
                        return True
                    attempts = int((existing or {}).get("attempt_count", 0)) + 1
                    phases = dict(state.get("phases", {}))
                    phases[phase] = {
                        "phase": phase,
                        "session": session.isoformat(),
                        "status": "RUNNING",
                        "reason_code": "FORWARD_PHASE_RUNNING",
                        "attempt_count": attempts,
                        "last_attempt_at": _canonical_at(now),
                        "completed_at": None,
                        "result_code": None,
                    }
                    state["phases"] = phases
                    self._write_state(state, now)

                timeout = (
                    self._capture_timeout
                    if phase == "CAPTURE"
                    else self._evaluate_timeout
                )
                try:
                    completed = self._runner(
                        self._command(phase, session),
                        cwd=str(self._root),
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                    )
                    result_code = int(completed.returncode)
                    stdout_hash, stdout_tail = self._bounded_process_evidence(
                        completed.stdout
                    )
                    stderr_hash, stderr_tail = self._bounded_process_evidence(
                        completed.stderr
                    )
                    execution_error = None
                except (OSError, subprocess.SubprocessError) as exc:
                    result_code = -1
                    stdout_hash, stdout_tail = self._bounded_process_evidence("")
                    stderr_hash, stderr_tail = self._bounded_process_evidence(str(exc))
                    execution_error = f"{type(exc).__name__}: {str(exc)[:300]}"

                completed_at = self._now()
                postcondition_ready = result_code == 0
                postcondition_reason: str | None = None
                if result_code == 0:
                    (
                        postcondition_ready,
                        postcondition_reason,
                    ) = self._delivery_postcondition(
                        phase,
                        session=session,
                        observed_at=completed_at,
                    )
                success = result_code == 0 and postcondition_ready
                next_retry = completed_at + timedelta(minutes=5)
                retryable = not success and next_retry <= end
                status = "SUCCEEDED" if success else (
                    "RETRY_PENDING" if retryable else "BLOCKED"
                )
                if success:
                    reason = "FORWARD_PHASE_SUCCEEDED"
                elif result_code == 0:
                    reason = (
                        "FORWARD_PHASE_POSTCONDITION_RETRY_PENDING"
                        if retryable
                        else "FORWARD_PHASE_POSTCONDITION_FAILED"
                    )
                else:
                    reason = (
                        "FORWARD_PHASE_RETRY_PENDING"
                        if retryable
                        else "FORWARD_PHASE_FAILED"
                    )
                with self._state_lock:
                    state = self._load_state()
                    phases = dict(state.get("phases", {}))
                    phases[phase] = {
                        "phase": phase,
                        "session": session.isoformat(),
                        "status": status,
                        "reason_code": reason,
                        "attempt_count": attempts,
                        "last_attempt_at": _canonical_at(now),
                        "completed_at": _canonical_at(completed_at),
                        "result_code": result_code,
                        "stdout_sha256": stdout_hash,
                        "stdout_tail": stdout_tail,
                        "stderr_sha256": stderr_hash,
                        "stderr_tail": stderr_tail,
                        "execution_error": execution_error,
                        "delivery_postcondition_checked": (
                            result_code == 0
                            and (
                                self._capture_readiness is not None
                                if phase == "CAPTURE"
                                else self._evaluation_readiness is not None
                            )
                        ),
                        "delivery_postcondition_ready": (
                            postcondition_ready if result_code == 0 else False
                        ),
                        "delivery_postcondition_reason_code": (
                            postcondition_reason
                        ),
                    }
                    state["phases"] = phases
                    self._write_state(state, completed_at)
                return success
            finally:
                execution_lock.__exit__(None, None, None)
        finally:
            self._attempt_lock.release()

    def capture_due(self) -> bool:
        return self._attempt("CAPTURE")

    def evaluate_due(self) -> bool:
        return self._attempt("EVALUATE")

    def reconcile(self) -> None:
        """Retry only inside frozen windows and make missed windows explicit."""

        now = self._now()
        self._heartbeat(now)
        requirement = self._calendar_requirement(now.date(), now)
        if requirement.get("required") is False:
            for phase in _TASKS:
                self._set_terminal_without_run(
                    phase,
                    observed_at=now,
                    status="NO_SAMPLE",
                    reason_code="NON_TRADING_SESSION_NOT_DUE",
                )
            return
        if requirement.get("required") is not True:
            return
        for phase in _TASKS:
            start, end = self._window(phase, now.date())
            if (
                phase == "CAPTURE"
                and now >= start
                and self._adopt_existing_capture(
                    session=now.date(),
                    observed_at=now,
                )
            ):
                continue
            if start <= now <= end:
                self._attempt(phase, observed_at=now)
            elif now > end:
                self._set_terminal_without_run(
                    phase,
                    observed_at=now,
                    status="MISSED",
                    reason_code=f"{phase}_WINDOW_MISSED",
                )

    def snapshot(self, *, force_refresh: bool = False) -> dict[str, object]:
        """Return the same readiness shape consumed by the review decision core."""

        if not isinstance(force_refresh, bool):
            raise TypeError("force_refresh must be boolean")
        now = self._now()
        resource_reasons = self._resource_reasons()
        configuration_ready = not resource_reasons
        state = self._load_state()
        registered_at = self._registered_at
        tasks: list[dict[str, object]] = []
        for phase, name in _TASKS.items():
            record = self._phase_record(state, phase, now.date()) or {}
            completed_at = record.get("completed_at")
            completed_after_registration = False
            if isinstance(completed_at, str) and registered_at is not None:
                try:
                    parsed = datetime.fromisoformat(completed_at)
                    completed_after_registration = (
                        parsed.tzinfo is not None
                        and parsed >= registered_at
                    )
                except ValueError:
                    pass
            operational = bool(
                record.get("status") == "SUCCEEDED"
                and completed_after_registration
            )
            task_reasons = list(resource_reasons)
            operational_reasons = [] if operational else [
                "AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION"
            ]
            tasks.append(
                {
                    "name": name,
                    "phase": phase,
                    "ready": configuration_ready,
                    "configuration_ready": configuration_ready,
                    "status": "ready" if configuration_ready else "not_ready",
                    "reason_codes": task_reasons,
                    "operationally_verified": operational,
                    "operational_status": (
                        "verified" if operational else "awaiting_first_success"
                    ),
                    "operational_reason_codes": operational_reasons,
                    "state": "Ready" if configuration_ready else "NotReady",
                    "last_run_time": record.get("last_attempt_at"),
                    "last_task_result": record.get("result_code"),
                    "last_run_reason_code": record.get("reason_code", "NEVER_RUN"),
                    "next_run_time": None,
                    "attempt_count": int(record.get("attempt_count", 0)),
                    "phase_status": record.get("status", "PENDING"),
                    "session": now.date().isoformat(),
                }
            )

        first_success = all(bool(task["operationally_verified"]) for task in tasks)
        qmt_ready = not any(
            reason == "QMT_LOCAL_DATA_DIRECTORY_UNAVAILABLE"
            for reason in resource_reasons
        )
        upstream = {
            "schema": QMT_RUNTIME_SCHEMA,
            "ready": qmt_ready,
            "status": "ready" if qmt_ready else "not_ready",
            "reason_code": "READY" if qmt_ready else "QMT_LOCAL_DATA_UNAVAILABLE",
            "reason_codes": [] if qmt_ready else ["QMT_LOCAL_DATA_UNAVAILABLE"],
            "configuration_ready": qmt_ready,
            "operationally_verified": qmt_ready,
            "operational_status": "verified" if qmt_ready else "not_verified",
            "operational_reason_codes": [] if qmt_ready else [
                "QMT_LOCAL_DATA_UNAVAILABLE"
            ],
            "upstream_ready_now": qmt_ready,
            "upstream_reason_code": "READY" if qmt_ready else "QMT_LOCAL_DATA_UNAVAILABLE",
            "data_directory": str(self._qmt) if self._qmt is not None else None,
            **_SAFETY,
        }
        operational = bool(configuration_ready and first_success and qmt_ready)
        operational_reasons = [] if operational else (
            ["AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION"]
            if configuration_ready and qmt_ready
            else list(dict.fromkeys([*resource_reasons, "UPSTREAM_QMT_RUNTIME_UNAVAILABLE"] if not qmt_ready else resource_reasons))
        )
        return {
            "schema": FORWARD_READINESS_SCHEMA,
            "contract_id": APP_FORWARD_CONTRACT_ID,
            "execution_owner": "APP_RUNTIME",
            "observed_at": _canonical_at(now),
            "ready": configuration_ready,
            "status": "ready" if configuration_ready else "not_ready",
            "reason_code": "READY" if configuration_ready else resource_reasons[0],
            "reason_codes": resource_reasons,
            "configuration_ready": configuration_ready,
            "operationally_verified": operational,
            "operational_status": "verified" if operational else (
                "awaiting_first_success"
                if configuration_ready and qmt_ready
                else "not_verified"
            ),
            "operational_reason_codes": operational_reasons,
            "first_success_after_registration": first_success,
            "registered_at": (
                _canonical_at(registered_at) if registered_at is not None else None
            ),
            "forward_contract_id": self._forward_contract_id,
            "forward_ledger_contract": (
                {
                    **dict(self._ledger_contract_preparation or {}),
                    "error": None,
                }
                if self._ledger_contract_error is None
                else {
                    "ready": False,
                    "status": "not_ready",
                    "reason_code": "FORWARD_LEDGER_CONTRACT_UNRESOLVED",
                    "current_contract_id": self._forward_contract_id,
                    "rotation": None,
                    "error": self._ledger_contract_error,
                    **_SAFETY,
                }
            ),
            "pinned_python_executable": str(self._python),
            "upstream_qmt": upstream,
            "tasks": tasks,
            "task_count": len(tasks),
            **_SAFETY,
        }


__all__ = (
    "APP_FORWARD_CONTRACT_ID",
    "APP_FORWARD_OWNER_SCHEMA",
    "APP_FORWARD_STATE_SCHEMA",
    "FORWARD_LEDGER_ROTATION_SCHEMA",
    "AppForwardSchedulerController",
    "CAPTURE_JOB_ID",
    "EVALUATE_JOB_ID",
    "RECONCILE_JOB_ID",
    "STARTUP_JOB_ID",
    "discover_qmt_local_data_dir",
    "evaluation_readiness_from_health",
    "prepare_forward_paper_ledger_contract",
)
