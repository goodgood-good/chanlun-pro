import json
import os
import pathlib
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "verify_deploy.ps1"
LEGACY_SCRIPT = ROOT / "ops" / "verify_deploy_v17.ps1"
WINDOWS_RUN = ROOT / "windows_run.bat"
REGISTER_SCRIPT = ROOT / "ops" / "register_qmt_restart_task.ps1"


class _HealthHandler(BaseHTTPRequestHandler):
    revision = "expected-revision"
    status = "ready"
    last_path = None
    pid = 999999
    source_revision = "expected-revision"

    def do_GET(self):
        type(self).last_path = self.path
        body = json.dumps(
            {
                "status": self.status,
                "revision": self.revision,
                "source_revision": self.source_revision,
                "pid": self.pid,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@pytest.mark.skipif(os.name != "nt", reason="deployment script targets Windows")
def test_deploy_verifier_exit_code_tracks_revision_match():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uri = f"http://127.0.0.1:{server.server_port}/readyz?market=a"

    def run(expected):
        return subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-ProjectRoot",
                str(ROOT),
                "-HealthUri",
                uri,
                "-ExpectedRevision",
                expected,
                "-SkipProcessCheck",
                "-SkipFreshnessCheck",
                "-SkipSourceCheck",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        matched = run("expected-revision")
        mismatch = run("different-revision")
    finally:
        server.shutdown()
        server.server_close()

    assert matched.returncode == 0, matched.stdout + matched.stderr
    assert "DEPLOY-OK" in matched.stdout
    assert mismatch.returncode != 0
    assert "DEPLOY-CHECK-FAILED" in mismatch.stdout


@pytest.mark.skipif(os.name != "nt", reason="deployment script targets Windows")
def test_deploy_verifier_rejects_not_ready_status():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uri = f"http://127.0.0.1:{server.server_port}/readyz?market=a"
    _HealthHandler.status = "not_ready"

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-ProjectRoot",
                str(ROOT),
                "-HealthUri",
                uri,
                "-ExpectedRevision",
                "expected-revision",
                "-SkipProcessCheck",
                "-SkipFreshnessCheck",
                "-SkipSourceCheck",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        _HealthHandler.status = "ready"
        server.shutdown()
        server.server_close()

    assert result.returncode != 0
    assert "DEPLOY-CHECK-FAILED" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="deployment script targets Windows")
def test_deploy_verifier_rejects_health_from_an_unrelated_pid():
    helper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "chanlun_chart"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.5)
    uri = f"http://127.0.0.1:{server.server_port}/readyz?market=a"

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-ProjectRoot",
                str(ROOT),
                "-HealthUri",
                uri,
                "-ExpectedRevision",
                "expected-revision",
                "-SkipFreshnessCheck",
                "-SkipSourceCheck",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        helper.terminate()
        helper.wait(timeout=10)

    assert result.returncode != 0
    assert "web process" in result.stdout

def test_daily_restart_completes_preflight_before_stopping_web():
    source = (ROOT / "ops" / "restart_qmt_daily.ps1").read_text(encoding="utf-8")
    stop_at = source.index("# --- 1. Stop the web project FIRST")
    required_preflight = {
        "project directories": "foreach ($requiredDir in @($ProjectRoot, $AppDir, $SrcPath))",
        "application files": "foreach ($requiredFile in @($AppScript, $verifyScript))",
        "dotenv": "Import-ProjectDotEnv -Path",
        "python interpreter": "$PythonExe = Resolve-ProjectPython",
        "source revision": "$sourceRevision = Get-ApplicationSourceRevision",
        "bounded compile": "$preflightProcess.WaitForExit($PreflightTimeoutSec * 1000)",
    }

    for label, marker in required_preflight.items():
        assert marker in source, f"missing {label} preflight"
        assert source.index(marker) < stop_at, f"{label} is checked after stopping web"

def test_operations_default_to_readiness_probe():
    verifier = SCRIPT.read_text(encoding="utf-8")
    legacy = LEGACY_SCRIPT.read_text(encoding="utf-8")
    restart = (ROOT / "ops" / "restart_qmt_daily.ps1").read_text(encoding="utf-8")
    windows_run = WINDOWS_RUN.read_text(encoding="utf-8")

    assert "http://127.0.0.1:9900/readyz?market=a" in verifier
    assert "ExpectedSourceRevision is required" in verifier
    assert "Get-NetTCPConnection" in verifier
    assert "http://127.0.0.1:9900/readyz?market=a" in legacy
    assert '$healthUri = "http://${probeHost}:$webPort/readyz?market=a"' in restart
    assert "AddSeconds(120)" in restart
    assert "$env:CHANLUN_WEB_HOST = '127.0.0.1'" in restart
    assert "$env:CHANLUN_BUILD_REVISION = $deploymentRevision" in restart
    assert "$health.revision -eq $deploymentRevision" in restart
    assert "[string]$health.pid -eq [string]$startedProcess.Id" in restart
    assert "-ExpectedRevision $deploymentRevision" in restart
    assert "app.py loads project .env" in windows_run
    assert "if not defined CHANLUN_WEB_HOST set" not in windows_run


@pytest.mark.skipif(os.name != "nt", reason="deployment script targets Windows")
def test_deploy_verifier_rejects_unattested_explicit_revision():
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-ProjectRoot",
            str(ROOT),
            "-HealthUri",
            "http://127.0.0.1:1/readyz?market=a",
            "-ExpectedRevision",
            "claimed-revision",
            "-SkipProcessCheck",
            "-SkipFreshnessCheck",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "ExpectedSourceRevision is required" in result.stdout
    assert "DEPLOY-CHECK-FAILED" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="deployment script targets Windows")
def test_process_start_date_converter_accepts_datetime_and_dmtf():
    helper = ROOT / "ops" / "deploy_common.ps1"
    command = (
        f". '{helper}'; "
        "$now = Get-Date; "
        "$dmtf = [Management.ManagementDateTimeConverter]::ToDmtfDateTime($now); "
        "$a = ConvertTo-ProcessStartDate $now; "
        "$b = ConvertTo-ProcessStartDate $dmtf; "
        "if ($a -isnot [datetime] -or $b -isnot [datetime]) { exit 1 }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_verifier_is_only_a_compatibility_wrapper():
    source = LEGACY_SCRIPT.read_text(encoding="utf-8")
    assert "verify_deploy.ps1" in source
    assert "_SIGNAL_CACHE_VERSION" not in source


def test_restart_attests_dirty_source_and_verifier_rechecks_it():
    restart = (ROOT / "ops" / "restart_qmt_daily.ps1").read_text(encoding="utf-8")
    verifier = SCRIPT.read_text(encoding="utf-8")

    for source in (restart, verifier):
        assert "Get-ApplicationSourceRevision" in source
        assert "ls-files --cached --others --exclude-standard" in source
        assert "hash-object --no-filters --stdin-paths" in source
    assert "$env:CHANLUN_BUILD_REVISION = $deploymentRevision" in restart
    assert "-ExpectedSourceRevision $sourceRevision" in restart
    assert "current source revision" in verifier


def test_restart_has_a_bounded_scheduled_catch_up_window():
    source = (ROOT / "ops" / "restart_qmt_daily.ps1").read_text(encoding="utf-8")

    assert "[switch]$Force" in source
    assert "$CatchUpWindowMinutes" in source
    assert ".AddMinutes($CatchUpWindowMinutes)" in source
    assert "outside the scheduled catch-up window" in source


def test_task_registration_reports_failure_and_confirms_registration():
    source = REGISTER_SCRIPT.read_text(encoding="utf-8")

    assert "$ErrorActionPreference = 'Stop'" in source
    assert "Register-ScheduledTask @regArgs -ErrorAction Stop" in source
    assert "-CatchUpWindowMinutes {1}" in source
    assert "Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop" in source
    assert "catch" in source
    assert "exit 1" in source
    assert "exit 0" in source


def test_windows_launcher_uses_the_same_python_resolution_without_masking_dotenv():
    source = WINDOWS_RUN.read_text(encoding="utf-8")

    assert "CHANLUN_PYTHON" in source
    assert ".venv\\Scripts\\python.exe" in source
    assert "poetry run python" in source
    assert "if not defined CHANLUN_WEB_HOST set" not in source
