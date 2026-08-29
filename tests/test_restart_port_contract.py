import os
from pathlib import Path
import subprocess

import pytest


def test_restart_script_validates_and_reuses_configured_web_port():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    port_preflight = source.index("$webPort = 9900")
    stop_phase = source.index("# --- 1. 先停止网页项目")

    assert port_preflight < stop_phase
    assert "$env:CHANLUN_WEB_PORT = [string]$webPort" in source
    assert '$healthUri = "http://${probeHost}:$webPort/readyz?market=a"' in source
    assert "Invoke-RestMethod -Uri $healthUri" in source
    assert "-HealthUri $healthUri" in source


@pytest.mark.skipif(os.name != "nt", reason="restart script targets Windows")
def test_restart_dotenv_loader_preserves_equals_and_quotes(tmp_path):
    script = Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    env_file = tmp_path / ".env"
    env_file.write_text(
        'CHANLUN_WEB_HOST="127.0.0.1"\n'
        'CHANLUN_WEB_PORT=19999\n'
        'CHANLUN_TEST_VALUE="左=右"\n'
        'CHANLUN_TEST_OVERRIDE=from-project\n',
        encoding="utf-8",
    )
    script_arg = str(script).replace("'", "''")
    env_arg = str(env_file).replace("'", "''")
    command = (
        f"$tokens=$null; $errors=$null; "
        f"$ast=[Management.Automation.Language.Parser]::ParseFile('{script_arg}',"
        "[ref]$tokens,[ref]$errors); "
        "$definition=$ast.Find({param($node) "
        "$node -is [Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Import-ProjectDotEnv'}, $true); "
        ". ([scriptblock]::Create($definition.Extent.Text)); "
        "$expected=([string][char]0x5de6) + '=' + ([string][char]0x53f3); "
        "Remove-Item Env:CHANLUN_WEB_HOST,Env:CHANLUN_WEB_PORT,"
        "Env:CHANLUN_TEST_VALUE -ErrorAction SilentlyContinue; "
        "$env:CHANLUN_TEST_OVERRIDE='stale-parent-value'; "
        f"Import-ProjectDotEnv -Path '{env_arg}' "
        "-OverrideNames @('CHANLUN_TEST_OVERRIDE'); "
        "if ($env:CHANLUN_WEB_HOST -ne '127.0.0.1' -or "
        "$env:CHANLUN_WEB_PORT -ne '19999' -or "
        "$env:CHANLUN_TEST_VALUE -ne $expected -or "
        "$env:CHANLUN_TEST_OVERRIDE -ne 'from-project') { exit 1 }"
    )

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_restart_owns_only_the_configured_port_process_and_confirms_shutdown():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    assert "[regex]::Escape($AppScript)" in source
    assert "$relativeAppPattern" not in source
    assert "Get-NetTCPConnection" in source
    assert "$targetWebProcs" in source
    assert "Wait-Process -Id" in source
    assert "$i -lt 15" in source
    assert "$remainingPortOwners.Count -gt 0" in source
    assert "web process or listening port remained after stop" in source
    assert "Restore-WebService" in source


def test_restart_only_adopts_restricted_web_process_after_multi_factor_attestation():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    attestation = source.index("function Get-AttestedWebProcs")
    port_scan = source.index(
        "$portOwners = @(Get-ListeningProcessIds -Port $webPort)",
        attestation,
    )
    foreign_rejection = source.index("$foreignOwners = @(", port_scan)

    assert attestation < port_scan < foreign_rejection
    assert "$PortOwnerIds -notcontains $reportedPid" in source
    assert "$reportedRevision.StartsWith(" in source
    assert "$expectedCommitPrefix" in source
    assert "@('scheduler', 'qmt_runtime', 'trading_screening')" in source
    assert "[string]$process.Name -ne 'python.exe'" in source
    assert "accepted endpoint-attested web PID=" in source


def test_restart_acquires_single_flight_lock_before_stopping_any_process():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    preflight_only = source.index("if ($PreflightOnly)")
    acquire = source.index("$deploymentMutex = Enter-DeploymentMutex")
    stop_phase = source.index("# --- 1. 先停止网页项目")

    assert preflight_only < acquire < stop_phase
    assert "WaitOne(0)" in source
    assert "AbandonedMutexException" in source
    assert "another restart invocation owns deployment lock" in source
    assert "Exit-DeploymentMutex -Mutex $deploymentMutex" in source
    assert "ReleaseMutex()" in source
    assert ".Dispose()" in source


def test_elevated_restart_hands_start_to_limited_task_after_unlocking():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    elevated_branch = source.index("if (Test-CurrentProcessElevated)")
    unlock = source.index("# HANDOFF-LOCK-RELEASE", elevated_branch)
    limited_start = source.index("# HANDOFF-LIMITED-START", unlock)
    direct_start = source.index("# --- 2.", limited_start)
    branch = source[elevated_branch:direct_start]

    assert elevated_branch < unlock < limited_start < direct_start
    assert "-RunLevel Limited" in source
    assert "Register-LimitedWebLaunchTask" in branch
    assert "Exit-DeploymentMutex -Mutex $deploymentMutex" in branch
    assert "Start-ScheduledTask -TaskName $handoffTaskName" in branch
    assert "Remove-LimitedWebLaunchTask -TaskName $handoffTaskName" in branch
    assert "$expectedHandoffRevisionPrefix" in branch
    assert "$candidateOwners -contains $candidatePid" in branch
    assert "-ExpectedProcessId $handoffPid" in branch
    verify = branch.index("-ExpectedProcessId $handoffPid")
    persist_scope = branch.index("Write-WatchdogDeploymentScope", verify)
    watchdog_gate = branch.index("if (-not $SkipWatchdog)", persist_scope)
    stop_watchdog = branch.index("Stop-Process `", watchdog_gate)
    install_watchdog = branch.index("-File $watchdogInstaller", stop_watchdog)
    done = branch.index("===== web restart DONE =====", install_watchdog)

    assert verify < persist_scope < watchdog_gate < stop_watchdog
    assert stop_watchdog < install_watchdog < done
    assert "Limited scheduled task with persisted deployment scope" in branch


@pytest.mark.skipif(os.name != "nt", reason="restart script targets Windows")
def test_restart_single_flight_lock_rejects_a_concurrent_process(tmp_path):
    """Exercise contention and abandoned-owner recovery without touching services."""

    script = Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    helper = tmp_path / "deployment_lock_probe.ps1"
    helper.write_text(
        r"""
param(
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][int]$Port,
    [switch]$Hold
)
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $ScriptPath,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) { exit 40 }
foreach ($functionName in @(
    'Get-DeploymentMutexName',
    'Enter-DeploymentMutex',
    'Exit-DeploymentMutex'
)) {
    $definition = $ast.Find(
        {
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $functionName
        },
        $true
    )
    if ($null -eq $definition) { exit 41 }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$name = Get-DeploymentMutexName -Root $Root -Port $Port
$mutex = Enter-DeploymentMutex -Name $name
if ($null -eq $mutex) {
    Write-Output 'BLOCKED'
    exit 23
}
Write-Output ('ACQUIRED:' + $name)
[Console]::Out.Flush()
if ($Hold) { [Console]::In.ReadLine() | Out-Null }
Exit-DeploymentMutex -Mutex $mutex
exit 0
""".strip(),
        encoding="utf-8",
    )
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        "-ScriptPath",
        str(script),
        "-Root",
        str(tmp_path),
        "-Port",
        "19991",
    ]
    holder = subprocess.Popen(
        [*command, "-Hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        acquired = holder.stdout.readline().strip()
        assert acquired.startswith("ACQUIRED:Local\\ChanlunProDeploy_")

        blocked = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert blocked.returncode == 23
        assert blocked.stdout.strip() == "BLOCKED"

        assert holder.stdin is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder_stdout, holder_stderr = holder.communicate(timeout=30)
        assert holder.returncode == 0, holder_stdout + holder_stderr

        reacquired = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert reacquired.returncode == 0, reacquired.stdout + reacquired.stderr
        assert reacquired.stdout.startswith("ACQUIRED:Local\\ChanlunProDeploy_")

        abandoned = subprocess.Popen(
            [*command, "-Hold"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        abandoned_acquired = abandoned.stdout.readline().strip()
        assert abandoned_acquired.startswith("ACQUIRED:Local\\ChanlunProDeploy_")
        abandoned.kill()
        abandoned.wait(timeout=10)

        recovered = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert recovered.stdout.startswith("ACQUIRED:Local\\ChanlunProDeploy_")
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


def test_restart_loads_dotenv_and_resolves_the_project_python_before_preflight():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    dotenv = source.index("Import-ProjectDotEnv")
    python = source.index("Resolve-ProjectPython")
    preflight = source.index("$preflightCode =")

    assert dotenv < preflight
    assert python < preflight
    assert "CHANLUN_PYTHON" in source
    assert ".venv\\Scripts\\python.exe" in source
    assert "poetry" in source


def test_restart_builds_complete_longbridge_environment_before_preflight():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    dotenv_call = source.index("Import-ProjectDotEnv -Path")
    user_fallback_call = source.index("Import-UserEnvironmentFallback -Names")
    preflight = source.index("$preflightCode =")

    assert dotenv_call < user_fallback_call < preflight
    assert "[string[]]$OverrideNames" in source
    assert "$OverrideNames -notcontains $key" in source
    assert "[Environment]::GetEnvironmentVariable($name, 'User')" in source
    for name in (
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
    ):
        assert source.count(f"'{name}'") >= 2


def test_project_login_hash_overrides_stale_parent_process_value():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    managed_names = source.index("$deploymentManagedNames = @(")
    dotenv_call = source.index("Import-ProjectDotEnv -Path")
    validation = source.index(
        "if (-not (Test-LoginPasswordHash -Value $env:CHANLUN_LOGIN_PWD))"
    )

    assert managed_names < dotenv_call < validation
    assert "'CHANLUN_LOGIN_PWD'" in source[managed_names:dotenv_call]


def test_restart_rejects_invalid_login_environment_before_stopping_service():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    validation = source.index(
        "if (-not (Test-LoginPasswordHash -Value $env:CHANLUN_LOGIN_PWD))"
    )
    stop_phase = source.index("# --- 1. 先停止网页项目")

    assert validation < stop_phase
    assert "function Test-LoginPasswordHash" in source
    assert ".StartsWith('pbkdf2:')" in source
    assert ".StartsWith('scrypt:')" in source
    assert "现有服务未停止" in source


def test_poetry_python_resolution_tolerates_informational_stderr_only():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    resolver = source.index("function Resolve-ProjectPython")
    next_function = source.index("function Get-WebProcs")
    body = source[resolver:next_function]

    assert "$previousErrorActionPreference = $ErrorActionPreference" in body
    assert "$ErrorActionPreference = 'Continue'" in body
    assert "$ErrorActionPreference = $previousErrorActionPreference" in body
    assert "if ($line -isnot [string]) { continue }" in body
    assert "if ($exitCode -ne 0)" in body
    assert "C:\\Users\\lc\\miniconda3" not in source
