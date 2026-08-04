from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "ops" / "register_qmt_restart_task.ps1"
RESTART = ROOT / "ops" / "restart_qmt_daily.ps1"
AUDIT = ROOT / "ops" / "audit_qmt_restart_task.ps1"
MANAGE = ROOT / "ops" / "manage_qmt_runtime.ps1"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_qmt_task_limit_covers_cold_readiness_and_has_bounded_retry() -> None:
    source = _source(REGISTER)

    assert "[int]$WebReadinessTimeoutSeconds = 1800" in source
    assert "[int]$ExecutionTimeLimitMinutes = 45" in source
    assert "[Math]::Ceiling($WebReadinessTimeoutSeconds / 60.0) + 10" in source
    assert "-WebReadinessTimeoutSeconds {1}" in source
    assert "-RestartCount $RestartCount" in source
    assert "-RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes)" in source
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes)" in source
    assert "[int]$RestartCount = 3" in source
    assert "[int]$RestartIntervalMinutes = 5" in source


def test_qmt_registration_retains_interactive_limited_exact_contract() -> None:
    source = _source(REGISTER)

    assert "#Requires -RunAsAdministrator" not in source
    assert "-LogonType Interactive -RunLevel Limited" in source
    assert "$confirmedActions[0].Arguments -ne $expectedArguments" in source
    assert "$confirmedTask.Settings.RestartCount" in source
    assert "$confirmedTask.Settings.RestartInterval" in source
    assert "$confirmedTask.Settings.ExecutionTimeLimit" in source
    assert "$confirmedTriggers[0].DaysOfWeek -eq 62" in source
    assert "the interactive non-elevated QMT principal was not retained" in source
    assert "qmt_restart_registration.json" in source
    assert "real_order_transport_enabled = $false" in source
    assert "live_status = 'LIVE_DISABLED'" in source
    assert "yyyy-MM-ddTHH:mm:ss.ffffffK" in source


def test_qmt_registration_explains_admin_owned_task_migration() -> None:
    source = _source(REGISTER)

    assert "Get-Acl -LiteralPath $taskFile" in source
    assert "access.*denied|拒绝访问" in source
    assert "Unregister-ScheduledTask -TaskName" in source
    assert "then return to a normal (non-admin) PowerShell" in source
    assert "The recreated task remains Interactive/Limited" in source


def test_success_receipt_is_bound_to_registration_and_real_readiness() -> None:
    source = _source(RESTART)

    assert "function Write-QmtSchedulerSuccessReceipt" in source
    assert "registration_receipt_sha256 = $registrationHash" in source
    assert "qmt_restart_completed = $true" in source
    assert "web_readiness_verified = $true" in source
    assert "if (-not $Force -and -not $WebOnly)" in source
    assert source.index("if ($verifyExit -ne 0)") < source.rindex(
        "Write-QmtSchedulerSuccessReceipt"
    )
    assert "real_account_accessed = $false" in source
    assert "real_order_transport_enabled = $false" in source
    assert "automated_order_authorized = $false" in source


def test_legacy_restart_refuses_to_race_the_app_qmt_owner() -> None:
    source = _source(RESTART)

    assert "function Get-LiveAppQmtOwner" in source
    assert "chanlun-qmt-execution-owner/v1" in source
    assert "chanlun-qmt-runtime/app-runtime-contract/v1" in source
    assert "legacy daily task refused to run" in source
    assert "exit 76" in source
    assert "-not $Force -and -not $WebOnly -and -not $PreflightOnly" in source


def test_app_qmt_helper_targets_only_the_configured_installation() -> None:
    source = _source(MANAGE)

    assert "[ValidateSet('Status', 'Ensure', 'Restart')]" in source
    assert "Get-TargetProcesses" in source
    assert "(Split-Path -LiteralPath $_.Path) -ieq $Directory" in source
    assert "QMT executable must be XtItClient.exe" in source
    assert "QMT_RUNTIME_OPERATION_IN_PROGRESS" in source
    assert "real_account_accessed = $false" in source
    assert "real_order_transport_enabled = $false" in source
    assert "live_status = 'LIVE_DISABLED'" in source
    assert "app.py" not in source


def test_app_qmt_helper_has_bounded_nonfatal_log_retention() -> None:
    source = _source(MANAGE)

    assert "[int]$LogRetentionDays = 30" in source
    assert "[long]$LogMaxTotalBytes = 104857600" in source
    assert "function Invoke-QmtLogRetention" in source
    assert "^qmt_runtime_\\d{4}-\\d{2}-\\d{2}\\.log$" in source
    assert "$log.FullName -ieq $currentFullPath" in source
    assert "Diagnostic cleanup must never make QMT unavailable" in source
    assert "log_retention_days = $LogRetentionDays" in source
    assert "log_max_total_bytes = $LogMaxTotalBytes" in source


@pytest.mark.skipif(os.name != "nt", reason="QMT helper targets Windows")
def test_qmt_log_retention_removes_only_oldest_dated_logs(tmp_path: Path) -> None:
    today = date.today()
    names = {
        "expired": f"qmt_runtime_{today - timedelta(days=60):%Y-%m-%d}.log",
        "oldest": f"qmt_runtime_{today - timedelta(days=3):%Y-%m-%d}.log",
        "middle": f"qmt_runtime_{today - timedelta(days=2):%Y-%m-%d}.log",
        "newest": f"qmt_runtime_{today - timedelta(days=1):%Y-%m-%d}.log",
        "current": f"qmt_runtime_{today:%Y-%m-%d}.log",
    }
    for name in names.values():
        (tmp_path / name).write_bytes(b"x" * 10)
    unrelated = tmp_path / "web_restart_stderr_keep.log"
    unrelated.write_bytes(b"y" * 20)

    escaped_script = str(MANAGE).replace("'", "''")
    escaped_dir = str(tmp_path).replace("'", "''")
    escaped_current = str(tmp_path / names["current"]).replace("'", "''")
    command = rf"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    '{escaped_script}', [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) {{ exit 50 }}
$definition = $ast.Find(
    {{
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Invoke-QmtLogRetention'
    }},
    $true
)
if ($null -eq $definition) {{ exit 51 }}
. ([scriptblock]::Create($definition.Extent.Text))
$result = Invoke-QmtLogRetention `
    -Directory '{escaped_dir}' `
    -CurrentLog '{escaped_current}' `
    -RetentionDays 30 `
    -MaxTotalBytes 25
$result | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["removed_count"] == 3
    assert result["removed_bytes"] == 30
    assert result["remaining_bytes"] == 20
    assert result["current_log_preserved"] is True
    assert not (tmp_path / names["expired"]).exists()
    assert not (tmp_path / names["oldest"]).exists()
    assert not (tmp_path / names["middle"]).exists()
    assert (tmp_path / names["newest"]).exists()
    assert (tmp_path / names["current"]).exists()
    assert unrelated.exists()


@pytest.mark.skipif(os.name != "nt", reason="QMT helper targets Windows")
def test_app_qmt_snapshot_reads_main_start_time_without_dictionary_projection() -> None:
    escaped = str(MANAGE).replace("'", "''")
    command = rf"""
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    '{escaped}', [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) {{ exit 40 }}
$definition = $ast.Find(
    {{
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Get-QmtSnapshot'
    }},
    $true
)
if ($null -eq $definition) {{ exit 41 }}
. ([scriptblock]::Create($definition.Extent.Text))
function Get-TargetProcesses {{
    @(
        [pscustomobject]@{{
            ProcessName = 'XtMiniQmt'
            Id = 101
            StartTime = [datetime]'2026-08-02T18:21:23'
            Path = 'D:\qmt\XtMiniQmt.exe'
        }},
        [pscustomobject]@{{
            ProcessName = 'miniquote'
            Id = 102
            StartTime = [datetime]'2026-08-02T18:21:24'
            Path = 'D:\qmt\miniquote.exe'
        }}
    )
}}
$Action = 'Status'
$snapshot = Get-QmtSnapshot `
    -Executable 'D:\qmt\XtItClient.exe' `
    -Directory 'D:\qmt'
if (
    $snapshot.ready -ne $true -or
    $snapshot.main_process_count -ne 1 -or
    [string]::IsNullOrWhiteSpace([string]$snapshot.main_started_at)
) {{ exit 42 }}
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_qmt_audit_is_read_only_and_separates_configuration_from_operation() -> None:
    source = _source(AUDIT)

    for mutation in (
        "Register-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Enable-ScheduledTask",
        "Disable-ScheduledTask",
        "Unregister-ScheduledTask",
    ):
        assert mutation not in source
    assert "configuration_ready = $configurationReady" in source
    assert "operationally_verified = $operationallyVerified" in source
    assert "upstream_ready_now = $upstreamReadyNow" in source
    assert "REGISTRATION_RECEIPT_MISSING" in source
    assert "SUCCESS_RECEIPT_MISSING" in source
    assert "AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION" in source
    assert "00041303" in source
    assert "$lastRunReason = 'NEVER_RUN'" in source
    assert "LATEST_TASK_RUN_FAILED" in source
    assert "$logonType -ne 'Interactive'" in source
    assert "$runLevel -ne 'Limited'" in source
    assert "ExecutionTimeLimit -ne 'PT45M'" in source
    assert "Invoke-RestMethod" in source
    assert "live_status = 'LIVE_DISABLED'" in source


@pytest.mark.skipif(os.name != "nt", reason="scheduled-task scripts target Windows")
@pytest.mark.parametrize("path", (REGISTER, RESTART, AUDIT, MANAGE))
def test_qmt_task_scripts_parse_as_powershell(path: Path) -> None:
    escaped = str(path).replace("'", "''")
    command = (
        "$tokens=$null; $errors=$null; "
        f"[Management.Automation.Language.Parser]::ParseFile('{escaped}',"
        "[ref]$tokens,[ref]$errors) | Out-Null; "
        "if ($errors.Count -ne 0) { $errors | ForEach-Object { "
        "Write-Error $_.Message }; exit 1 }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
