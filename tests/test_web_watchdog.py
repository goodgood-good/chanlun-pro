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
        "EnableFullSymbolCatalog",
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
    assert "operational_degraded" in watchdog
    assert "application PID changed" in watchdog
    assert "[int]$LivenessTimeoutSeconds = 15" in watchdog
    assert "-TimeoutSec $LivenessTimeoutSeconds" in watchdog
    assert "[int]$ReadinessTimeoutSeconds = 15" in watchdog
    assert "-TimeoutSeconds $ReadinessTimeoutSeconds" in watchdog
    assert "$premarketTrigger = New-ScheduledTaskTrigger" in installer
    assert "-Weekly" in installer
    assert "08:20" in installer
    assert "-WindowStyle Hidden" in installer
    assert "-WorkingDirectory $ProjectRoot" in installer
    assert "deployment_scope.json" in restart
    assert "deployment_scope.json" in watchdog
    assert "Write-WatchdogDeploymentScope" in restart
    assert "$PSBoundParameters.ContainsKey($_)" in watchdog
    assert "chanlun-web-watchdog-deployment-scope-v1" in restart
    assert "chanlun-web-watchdog-deployment-scope-v1" in watchdog


def _healthy_readiness_payload():
    return {"status":"ready", "runtime_ready":True,"pid":1234,"revision":"test-revision", "reasons":[],
            "components": {name: {"required":True,"ready":True} for name in ("scheduler","runtime","metadata","symbols","ticks")}}


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
    # Candidate discovery lag is observable but must not restart a healthy
    # holdings/watchlist notification lane.






















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


@pytest.mark.skipif(os.name != "nt", reason="watchdog targets Windows")
def test_scheduled_watchdog_recovery_uses_persisted_deployment_scope(
    tmp_path: Path,
) -> None:
    ops = tmp_path / "ops"
    ops.mkdir()
    received_parameters = tmp_path / "received-parameters.txt"
    (ops / "restart_web.ps1").write_text(
        """param(
    [switch]$SkipWatchdog,
    [int]$WebReadinessTimeoutSeconds,
    [switch]$EnableFullSymbolCatalog
)
$PSBoundParameters.Keys |
    Sort-Object |
    Set-Content -LiteralPath '{received_parameters}' -Encoding UTF8
exit 0
""".format(
            received_parameters=str(received_parameters).replace("'", "''")
        ),
        encoding="utf-8",
    )

    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        unavailable_port = probe_socket.getsockname()[1]

    state_root = tmp_path / ".cache" / "chanlun_web_watchdog"
    state_root.mkdir(parents=True)
    (state_root / "deployment_scope.json").write_text(
        json.dumps(
            {
                "schema": "chanlun-web-watchdog-deployment-scope-v1",
                "project_root": str(tmp_path),
                "web_port": unavailable_port,
                "updated_at": "2026-08-29T00:00:00+08:00",
                "enable_full_symbol_catalog": True,
            }
        ),
        encoding="utf-8",
    )

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

    assert result.returncode == 1, result.stdout + result.stderr
    received = set(
        received_parameters.read_text(encoding="utf-8-sig").splitlines()
    )
    assert {
        "EnableFullSymbolCatalog",
        "SkipWatchdog",
        "WebReadinessTimeoutSeconds",
    } <= received
    heartbeat = json.loads(
        (state_root / "heartbeat.json").read_text(encoding="utf-8-sig")
    )
    assert heartbeat["scope_source"] == "persisted"
    assert all(heartbeat["deployment_scope"].values())
