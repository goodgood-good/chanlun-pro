from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "ops" / "watch_web.ps1"
INSTALLER = ROOT / "ops" / "install_web_watchdog.ps1"
RESTART = ROOT / "ops" / "restart_web.ps1"


def test_restart_launches_single_instance_watchdog_without_recursion() -> None:
    restart = RESTART.read_text(encoding="utf-8")
    watchdog = WATCHDOG.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "[switch]$SkipWatchdog" in restart
    assert "if (-not $SkipWatchdog)" in restart
    assert "ops\\watch_web.ps1" in restart
    assert "-SkipWatchdog" in watchdog
    for switch in (
        "EnableLargeScreeningScope",
        "EnableLargeHoldingMonitorScope",
        "EnableFullSymbolCatalog",
        "EnableFullCoverage",
        "ForceFullCoverageUntilComplete",
    ):
        assert f"[switch]${switch}" in watchdog
        assert f"$watchdogArguments += '-{switch}'" in restart
        assert f"$arguments += '-{switch}'" in watchdog
    assert "watchdog.lock" in watchdog
    assert "[IO.FileShare]::None" in watchdog
    assert "$restartProcess = Start-Process" in watchdog
    assert "$restartProcess.WaitForExit()" in watchdog
    assert "$output = & powershell.exe" not in watchdog
    assert "Register-ScheduledTask" in installer
    assert "-RestartCount 10" in installer
    assert "[string]$ProjectRoot," in watchdog
    assert "[string]$ProjectRoot," in installer
    assert "Split-Path -Parent $PSScriptRoot" in watchdog
    assert "Split-Path -Parent $PSScriptRoot" in installer
    assert "/readyz?market=" in watchdog
    assert "priority_monitor_ready" in watchdog
    assert "priority_monitor_starting" in watchdog
    assert "operational_degraded" in watchdog
    assert "application PID changed" in watchdog
    assert "outbox_worker_alive" in watchdog
    assert "[int]$LivenessTimeoutSeconds = 15" in watchdog
    assert "-TimeoutSec $LivenessTimeoutSeconds" in watchdog
    assert "[int]$ReadinessTimeoutSeconds = 15" in watchdog
    assert "-TimeoutSeconds $ReadinessTimeoutSeconds" in watchdog
    assert "$premarketTrigger = New-ScheduledTaskTrigger" in installer
    assert "-Weekly" in installer
    assert "08:20" in installer


def _healthy_readiness_payload() -> dict[str, object]:
    return {
        "status": "ready",
        "runtime_ready": True,
        "pid": 1234,
        "revision": "test-revision",
        "components": {
            "scheduler": {"ready": True},
            "runtime": {"ready": True},
            "qmt_runtime": {"ready": True, "operationally_verified": True},
            "ticks": {"ready": True},
            "trading_screening": {
                "runtime_ready": True,
                "runtime_status": "ready",
                "worker_alive": True,
                "heartbeat_age_seconds": 1,
                "heartbeat_max_age_seconds": 180,
                "priority_monitor_session_open": True,
                "priority_monitor_ready": True,
                "priority_monitor_status": "verified",
                "priority_monitor_age_seconds": 10,
                "realtime_alert_ready": True,
                "realtime_alert_status": "ready",
                "candidate_monitor_status": "cadence_overdue",
                "notification_dispatcher_configured": True,
                "notification_delivery": {
                    "configured": True,
                    "status": "verified",
                    "outbox_worker_alive": True,
                },
                "native_gateway": {
                    "ready": True,
                    "market_data_probe": {"ready": True},
                },
            },
        },
    }


def _run_watchdog_once(
    tmp_path: Path,
    readiness: dict[str, object],
    *,
    readiness_http_status: int = 200,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            response = (
                readiness
                if self.path.startswith("/readyz")
                else {"status": "alive", "pid": 1234, "revision": "test-revision"}
            )
            payload = json.dumps(response).encode("utf-8")
            self.send_response(
                readiness_http_status
                if self.path.startswith("/readyz")
                else 200
            )
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ops = tmp_path / "ops"
        ops.mkdir()
        (ops / "restart_web.ps1").write_text("exit 0\n", encoding="utf-8")
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WATCHDOG),
                "-ProjectRoot",
                str(tmp_path),
                "-WebPort",
                str(server.server_port),
                "-Once",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()

    heartbeat = json.loads(
        (tmp_path / ".cache" / "chanlun_web_watchdog" / "heartbeat.json").read_text(
            encoding="utf-8-sig"
        )
    )
    return result, heartbeat


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_once_records_a_healthy_liveness_probe(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            response = (
                _healthy_readiness_payload()
                if self.path.startswith("/readyz")
                else {"status": "alive", "pid": 1234, "revision": "test-revision"}
            )
            payload = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ops = tmp_path / "ops"
        ops.mkdir()
        (ops / "restart_web.ps1").write_text("exit 0\n", encoding="utf-8")
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WATCHDOG),
                "-ProjectRoot",
                str(tmp_path),
                "-WebPort",
                str(server.server_port),
                "-Once",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    heartbeat = json.loads(
        (tmp_path / ".cache" / "chanlun_web_watchdog" / "heartbeat.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert heartbeat["schema"] == "chanlun-web-watchdog-heartbeat"
    assert heartbeat["status"] == "healthy"
    assert heartbeat["consecutive_failures"] == 0
    assert heartbeat["health_uri"].endswith("/readyz?market=a")
    assert heartbeat["realtime_session_open"] is True
    # Candidate discovery lag is observable but must not restart a healthy
    # holdings/watchlist notification lane.
    assert heartbeat["candidate_monitor_status"] == "cadence_overdue"


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_exposes_candidate_cadence_without_restart_recommendation(
    tmp_path: Path,
) -> None:
    readiness = _healthy_readiness_payload()
    screening = readiness["components"]["trading_screening"]
    screening["realtime_alert_ready"] = False
    screening["realtime_alert_status"] = "candidate_monitor_degraded"
    screening["candidate_monitor_status"] = "cadence_overdue"

    result, heartbeat = _run_watchdog_once(tmp_path, readiness)

    assert result.returncode == 1, result.stdout + result.stderr
    assert heartbeat["status"] == "operational_degraded"
    assert heartbeat["recovery_recommended"] is False
    assert heartbeat["priority_monitor_status"] == "verified"
    assert heartbeat["candidate_monitor_status"] == "cadence_overdue"
    assert heartbeat["realtime_alert_status"] == "candidate_monitor_degraded"
    assert "candidate_monitor_cadence_overdue" in heartbeat["detail"]


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_parses_503_candidate_degradation_without_restart(
    tmp_path: Path,
) -> None:
    readiness = _healthy_readiness_payload()
    readiness["status"] = "not_ready"
    screening = readiness["components"]["trading_screening"]
    screening["realtime_alert_ready"] = False
    screening["realtime_alert_status"] = "candidate_monitor_degraded"
    screening["candidate_monitor_status"] = "warming"

    result, heartbeat = _run_watchdog_once(
        tmp_path,
        readiness,
        readiness_http_status=503,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert heartbeat["status"] == "operational_degraded"
    assert heartbeat["recovery_recommended"] is False
    assert "candidate_monitor_warming" in heartbeat["detail"]
    assert "ready endpoint failed" not in heartbeat["detail"]


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_does_not_restart_live_post_close_snapshot_rebuild(
    tmp_path: Path,
) -> None:
    readiness = _healthy_readiness_payload()
    readiness["status"] = "not_ready"
    readiness["runtime_ready"] = False
    screening = readiness["components"]["trading_screening"]
    screening.update(
        {
            "runtime_ready": False,
            "runtime_status": "not_ready",
            "worker_alive": True,
            "heartbeat_age_seconds": 1,
            "heartbeat_max_age_seconds": 180,
            "priority_monitor_session_open": False,
            "priority_monitor_ready": True,
            "priority_monitor_status": "not_due",
            "realtime_alert_ready": True,
            "realtime_alert_status": "not_due",
            "candidate_monitor_status": "not_due",
        }
    )

    result, heartbeat = _run_watchdog_once(
        tmp_path,
        readiness,
        readiness_http_status=503,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert heartbeat["status"] == "operational_degraded"
    assert heartbeat["recovery_recommended"] is False
    assert "trading_screening_not_ready" in heartbeat["detail"]
    assert "ready endpoint failed" not in heartbeat["detail"]


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_gives_current_process_priority_attestation_startup_budget(
    tmp_path: Path,
) -> None:
    readiness = _healthy_readiness_payload()
    screening = readiness["components"]["trading_screening"]
    screening["priority_monitor_ready"] = False
    screening["priority_monitor_status"] = "awaiting_runtime_verification"
    screening["realtime_alert_ready"] = False
    screening["realtime_alert_status"] = "priority_monitor_degraded"

    result, heartbeat = _run_watchdog_once(tmp_path, readiness)

    assert result.returncode == 1, result.stdout + result.stderr
    assert heartbeat["status"] == "startup_readiness_failed"
    assert heartbeat["recovery_recommended"] is True
    assert heartbeat["priority_monitor_status"] == "awaiting_runtime_verification"
    assert "priority_monitor_starting" in heartbeat["detail"]
    assert "realtime_alert_not_ready" not in heartbeat["detail"]


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_does_not_restart_for_completed_priority_data_degradation(
    tmp_path: Path,
) -> None:
    readiness = _healthy_readiness_payload()
    screening = readiness["components"]["trading_screening"]
    screening["priority_monitor_ready"] = False
    screening["priority_monitor_status"] = "degraded"
    screening["priority_monitor_age_seconds"] = 10
    screening["priority_monitor_last_failure_reason_counts"] = {
        "STRUCTURE_BUNDLE_STALE": 1
    }
    screening["realtime_alert_ready"] = False
    screening["realtime_alert_status"] = "priority_monitor_degraded"

    result, heartbeat = _run_watchdog_once(tmp_path, readiness)

    assert result.returncode == 1, result.stdout + result.stderr
    assert heartbeat["status"] == "operational_degraded"
    assert heartbeat["recovery_recommended"] is False
    assert "priority_monitor_degraded" in heartbeat["detail"]
    assert "realtime_alert_not_ready" not in heartbeat["detail"]


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_once_rejects_a_stale_priority_monitor(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/readyz"):
                response = _healthy_readiness_payload()
                screening = response["components"]["trading_screening"]
                screening["priority_monitor_ready"] = False
                screening["priority_monitor_status"] = "stale"
                screening["priority_monitor_age_seconds"] = 240
            else:
                response = {
                    "status": "alive",
                    "pid": 1234,
                    "revision": "test-revision",
                }
            payload = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ops = tmp_path / "ops"
        ops.mkdir()
        (ops / "restart_web.ps1").write_text("exit 0\n", encoding="utf-8")
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WATCHDOG),
                "-ProjectRoot",
                str(tmp_path),
                "-WebPort",
                str(server.server_port),
                "-Once",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 1, result.stdout + result.stderr
    heartbeat = json.loads(
        (tmp_path / ".cache" / "chanlun_web_watchdog" / "heartbeat.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert heartbeat["status"] == "readiness_failed"
    assert heartbeat["recovery_recommended"] is True
    assert "priority_monitor_not_ready" in heartbeat["detail"]


@pytest.mark.parametrize(
    (
        "configured",
        "delivery_status",
        "outbox_worker_alive",
        "failure_threshold",
        "expected_status",
        "expected_recovery",
        "expected_reason",
    ),
    [
        (
            True,
            "degraded",
            True,
            1,
            "configuration_failed",
            False,
            "notification_delivery_degraded",
        ),
        (
            False,
            "unavailable",
            True,
            1,
            "configuration_failed",
            False,
            "notification_dispatcher_not_configured",
        ),
        (
            True,
            "unavailable",
            False,
            6,
            "readiness_failed",
            True,
            "outbox_worker_not_alive",
        ),
    ],
)
@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_classifies_notification_failures_without_restart_loops(
    tmp_path: Path,
    configured: bool,
    delivery_status: str,
    outbox_worker_alive: bool,
    failure_threshold: int,
    expected_status: str,
    expected_recovery: bool,
    expected_reason: str,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/readyz"):
                response = _healthy_readiness_payload()
                screening = response["components"]["trading_screening"]
                screening["realtime_alert_ready"] = False
                screening["notification_dispatcher_configured"] = configured
                screening["notification_delivery"].update(
                    {
                        "configured": configured,
                        "status": delivery_status,
                        "outbox_worker_alive": outbox_worker_alive,
                    }
                )
            else:
                response = {
                    "status": "alive",
                    "pid": 1234,
                    "revision": "test-revision",
                }
            payload = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ops = tmp_path / "ops"
        ops.mkdir()
        (ops / "restart_web.ps1").write_text("exit 0\n", encoding="utf-8")
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WATCHDOG),
                "-ProjectRoot",
                str(tmp_path),
                "-WebPort",
                str(server.server_port),
                "-ReadinessFailureThreshold",
                str(failure_threshold),
                "-Once",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 1, result.stdout + result.stderr
    heartbeat = json.loads(
        (tmp_path / ".cache" / "chanlun_web_watchdog" / "heartbeat.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert heartbeat["status"] == expected_status
    assert heartbeat["recovery_recommended"] is expected_recovery
    assert expected_reason in heartbeat["detail"]


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_watchdog_recovery_returns_while_spawned_web_process_keeps_running(
    tmp_path: Path,
) -> None:
    ops = tmp_path / "ops"
    ops.mkdir()
    child_pid_path = tmp_path / "spawned-child.pid"
    (ops / "restart_web.ps1").write_text(
        """param(
    [switch]$SkipWatchdog,
    [int]$WebReadinessTimeoutSeconds
)
$child = Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep -Seconds 60') `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath '{child_pid_path}' -Value $child.Id -Encoding ASCII
exit 0
""".format(child_pid_path=str(child_pid_path).replace("'", "''")),
        encoding="utf-8",
    )

    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        unavailable_port = probe_socket.getsockname()[1]

    started_at = time.monotonic()
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WATCHDOG),
                "-ProjectRoot",
                str(tmp_path),
                "-WebPort",
                str(unavailable_port),
                "-FailureThreshold",
                "1",
                "-LivenessTimeoutSeconds",
                "5",
                "-RestartCooldownSeconds",
                "10",
                "-Once",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        if child_pid_path.exists():
            child_pid = child_pid_path.read_text(encoding="ascii").strip()
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Stop-Process -Id {child_pid} -Force -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

    # Keep the assertion well below the spawned child's lifetime while leaving
    # enough headroom for a heavily loaded Windows host to create PowerShell.
    assert time.monotonic() - started_at < 30
    assert result.returncode == 1, result.stdout + result.stderr
    heartbeat = json.loads(
        (tmp_path / ".cache" / "chanlun_web_watchdog" / "heartbeat.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert heartbeat["status"] == "recovered"
    watchdog_log = next((tmp_path / "ops" / "logs").glob("web_watchdog_*.log"))
    assert "recovery completed successfully" in watchdog_log.read_text(
        encoding="utf-8-sig"
    )
