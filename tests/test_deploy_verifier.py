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
WINDOWS_RUN = ROOT / "windows_run.bat"


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
def test_deploy_verifier_resolves_default_project_root_after_file_binding():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
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
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEPLOY-OK" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="deployment script targets Windows")
def test_deploy_verifier_accepts_current_source_derived_run_revision_by_default():
    helper = ROOT / "ops" / "deploy_common.ps1"
    revision_result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                f". '{helper}'; "
                f"Get-ApplicationSourceRevision -Root '{ROOT}'"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    source_revision = revision_result.stdout.strip().splitlines()[-1]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    original_revision = _HealthHandler.revision
    _HealthHandler.revision = f"{source_revision}.run.test-instance"
    thread.start()
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
                "-SkipProcessCheck",
                "-SkipFreshnessCheck",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        _HealthHandler.revision = original_revision
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEPLOY-OK" in result.stdout


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

def test_web_restart_completes_preflight_before_stopping_web():
    source = (ROOT / "ops" / "restart_web.ps1").read_text(encoding="utf-8")
    stop_at = source.index("# --- 1. 先停止网页项目")
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
    restart = (ROOT / "ops" / "restart_web.ps1").read_text(encoding="utf-8")
    windows_run = WINDOWS_RUN.read_text(encoding="utf-8")

    assert "http://127.0.0.1:9900/readyz?market=a" in verifier
    assert "ExpectedSourceRevision is required" in verifier
    assert "Get-NetTCPConnection" in verifier
    assert '$healthUri = "http://${probeHost}:$webPort/readyz?market=a"' in restart
    assert "[int]$WebReadinessTimeoutSeconds = 1800" in restart
    assert "AddSeconds($WebReadinessTimeoutSeconds)" in restart
    assert "$env:CHANLUN_WEB_HOST = '127.0.0.1'" in restart
    assert "$env:CHANLUN_BUILD_REVISION = $deploymentRevision" in restart
    assert "$health.revision -eq $deploymentRevision" in restart
    assert "[string]$health.pid -eq [string]$startedProcess.Id" in restart
    assert "-ExpectedRevision $deploymentRevision" in restart
    assert "[switch]$OpenBrowser" in restart
    assert '$webUri = "http://${probeHost}:$webPort/"' in restart
    assert "if ($OpenBrowser) { Open-WebApplication -Uri $webUri }" in restart
    assert "ops\\restart_web.ps1" in windows_run
    assert "-OpenBrowser" in windows_run
    for scope_flag in (
        "CHANLUN_SYMBOL_CATALOG_FULL_REFRESH_AUTHORIZED",
        "CHANLUN_TRADING_SCREENING_ALLOW_LARGE_SCOPE",
        "CHANLUN_TRADING_SCREENING_FULL_COVERAGE_ENABLED",
        "CHANLUN_TRADING_SCREENING_FORCE_FULL_COVERAGE_UNTIL_COMPLETE",
    ):
        assert f'set "{scope_flag}=0"' in windows_run
    assert "web\\chanlun_chart\\app.py" not in windows_run
    assert "poetry run python" not in windows_run


def test_normal_restart_forces_bounded_screening_numeric_scope_after_dotenv():
    restart = (ROOT / "ops" / "restart_web.ps1").read_text(encoding="utf-8")
    windows_run = WINDOWS_RUN.read_text(encoding="utf-8")
    numeric_scope_names = (
        "CHANLUN_TRADING_SCREENING_VALIDATION_COHORT_SIZE",
        "CHANLUN_TRADING_SCREENING_CANDIDATE_5M_MAX_SYMBOLS",
        "CHANLUN_TRADING_SCREENING_CANDIDATE_30M_MAX_SYMBOLS",
        "CHANLUN_TRADING_SCREENING_SUPPORTIVE_DISCOVERY_MAX_SECTOR_RANK",
        "CHANLUN_TRADING_SCREENING_SYMBOLS_PER_REFRESH",
        "CHANLUN_TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH",
        "CHANLUN_TRADING_SCREENING_PRIORITY_MAX_SYMBOLS",
    )

    dotenv_call = restart.index("Import-ProjectDotEnv -Path")
    symbol_catalog_codes = restart.index(
        "'CHANLUN_SYMBOL_CATALOG_VALIDATION_CODES'", dotenv_call
    )
    symbol_catalog_authorization = restart.index(
        "'CHANLUN_SYMBOL_CATALOG_FULL_REFRESH_AUTHORIZED'",
        symbol_catalog_codes,
    )
    bounded_gate = restart.index(
        "if (-not $EnableLargeScreeningScope) {", dotenv_call
    )
    large_scope_flag = restart.index(
        "'CHANLUN_TRADING_SCREENING_ALLOW_LARGE_SCOPE'", bounded_gate
    )
    reset_body = restart[bounded_gate:large_scope_flag]

    assert (
        dotenv_call
        < symbol_catalog_codes
        < symbol_catalog_authorization
        < bounded_gate
        < large_scope_flag
    )
    assert "[switch]$EnableFullSymbolCatalog" in restart
    assert "$EnableFullSymbolCatalog.IsPresent" in restart
    assert "-FullSymbolCatalogEnabled" in restart
    assert (
        'set "CHANLUN_SYMBOL_CATALOG_FULL_REFRESH_AUTHORIZED=0"'
        in windows_run
    )
    assert 'set "CHANLUN_SYMBOL_CATALOG_VALIDATION_CODES=' in windows_run
    assert (
        "[Environment]::SetEnvironmentVariable($name, '12', 'Process')"
        in reset_body
    )
    for name in numeric_scope_names:
        assert f"'{name}'" in reset_body
        assert f'set "{name}=12"' in windows_run
    assert (
        "'CHANLUN_TRADING_SCREENING_MAX_ADMITTED_UNIVERSE_SYMBOLS'"
        in reset_body
    )
    assert "$LargeScopePriorityMaxSymbols = 48" in restart
    assert "} else {" in reset_body
    assert (
        "'CHANLUN_TRADING_SCREENING_PRIORITY_MAX_SYMBOLS'" in reset_body
        and "[string]$LargeScopePriorityMaxSymbols" in reset_body
    )
    assert (
        'set "CHANLUN_TRADING_SCREENING_MAX_ADMITTED_UNIVERSE_SYMBOLS=20"'
        in windows_run
    )

    holding_gate = restart.index(
        "if (-not $EnableLargeHoldingMonitorScope) {", dotenv_call
    )
    holding_authorization = restart.index(
        "'CHANLUN_HOLDING_GROUP_MONITOR_LARGE_SCOPE_AUTHORIZED'",
        holding_gate,
    )
    assert "[switch]$EnableLargeHoldingMonitorScope" in restart
    assert (
        "'CHANLUN_HOLDING_GROUP_MONITOR_MAX_SYMBOLS'"
        in restart[holding_gate:holding_authorization]
    )
    assert "-LargeHoldingMonitorScopeEnabled" in restart
    assert "$EnableLargeHoldingMonitorScope.IsPresent" in restart
    assert "-LargeScopeEnabled $EnableLargeScreeningScope.IsPresent" in restart
    assert "-FullCoverageEnabled $EnableFullCoverage.IsPresent" in restart
    assert (
        "-ForcedFullCoverageEnabled $ForceFullCoverageUntilComplete.IsPresent"
        in restart
    )
    assert 'set "CHANLUN_HOLDING_GROUP_MONITOR_MAX_SYMBOLS=12"' in windows_run
    assert (
        'set "CHANLUN_HOLDING_GROUP_MONITOR_LARGE_SCOPE_AUTHORIZED=0"'
        in windows_run
    )


def test_restart_replaces_watchdog_with_current_scope_after_deploy_verification():
    restart = (ROOT / "ops" / "restart_web.ps1").read_text(encoding="utf-8")

    watchdog_gate = restart.index("if (-not $SkipWatchdog) {")
    watchdog_body = restart[watchdog_gate:]
    stop_at = watchdog_body.index("Stop-Process `")
    launch_at = watchdog_body.index("$watchdogProcess = Start-Process `")

    assert stop_at < launch_at
    assert "$watchdogScriptToken = '-File {0}' -f $watchdogScript" in watchdog_body
    assert "$watchdogRootToken = '-ProjectRoot {0}' -f $ProjectRoot" in watchdog_body
    assert "$watchdogPortToken = '-WebPort {0}' -f $webPort" in watchdog_body
    assert "Get-CimInstance Win32_Process" in watchdog_body
    assert "$_.ProcessId -ne $PID" in watchdog_body
    assert "-ErrorAction Stop" in watchdog_body[stop_at:launch_at]
    assert "-WindowStyle Hidden" in watchdog_body[launch_at:]
    assert "with current deployment scope" in watchdog_body[launch_at:]


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


def test_restart_attests_dirty_source_and_verifier_rechecks_it():
    restart = (ROOT / "ops" / "restart_web.ps1").read_text(encoding="utf-8")
    verifier = SCRIPT.read_text(encoding="utf-8")
    helper = (ROOT / "ops" / "deploy_common.ps1").read_text(encoding="utf-8")

    for source in (restart, verifier):
        assert "Get-ApplicationSourceRevision" in source
        assert "deploy_common.ps1" in source
    assert "function Get-ApplicationSourceRevision" in helper
    assert "ls-files --cached --others --exclude-standard" in helper
    assert "Get-ApplicationFileSha256" in helper
    assert "[Security.Cryptography.SHA256]::Create()" in helper
    assert "[IO.File]::Open(" in helper
    assert "$env:CHANLUN_BUILD_REVISION = $deploymentRevision" in restart
    assert "-ExpectedSourceRevision $sourceRevision" in restart
    assert "current source revision" in verifier


def test_restart_source_manifest_uses_real_tab_delimiters():
    helper = (ROOT / "ops" / "deploy_common.ps1").read_text(
        encoding="utf-8"
    )

    assert '$manifest.Add(("{0}`t{1}" -f $path, $hash))' in helper
    assert "$manifest.Add(('{0}`t{1}' -f $path, $hash))" not in helper
    assert "[Array]::Sort($paths, [StringComparer]::Ordinal)" in helper


@pytest.mark.skipif(os.name != "nt", reason="deployment script targets Windows")
def test_restart_and_forward_runner_compute_the_same_source_revision():
    from chanlun.decision_support.trading_system.decision_source_provenance import (
        calculate_forward_application_source_revision,
    )

    helper = str(ROOT / "ops" / "deploy_common.ps1").replace("'", "''")
    root = str(ROOT).replace("'", "''")
    command = (
        f". '{helper}';"
        f"Get-ApplicationSourceRevision -Root '{root}'"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())
    assert output[-1] == calculate_forward_application_source_revision(ROOT)


def test_windows_launcher_delegates_python_resolution_to_managed_restart():
    source = WINDOWS_RUN.read_text(encoding="utf-8")

    assert "ops\\restart_web.ps1" in source
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass" in source
    assert "CHANLUN_PYTHON" not in source
    assert ".venv\\Scripts\\python.exe" not in source
    assert "poetry run python" not in source
    assert "if not defined CHANLUN_WEB_HOST set" not in source
