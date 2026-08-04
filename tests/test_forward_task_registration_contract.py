from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "ops" / "register_v3_forward_paper_tasks.ps1"
RUNNER = ROOT / "ops" / "run_v3_forward_paper_daily.ps1"
AUDIT = ROOT / "ops" / "audit_v3_forward_paper_tasks.ps1"
MIGRATE = ROOT / "ops" / "migrate_v3_forward_tasks_to_app.ps1"
APP_AUDIT = ROOT / "ops" / "audit_v3_app_forward_scheduler.ps1"


def _source() -> str:
    return REGISTER.read_text(encoding="utf-8")


def _runner_source() -> str:
    return RUNNER.read_text(encoding="utf-8")


def _audit_source() -> str:
    return AUDIT.read_text(encoding="utf-8")


def _migration_source() -> str:
    return MIGRATE.read_text(encoding="utf-8")


def _app_audit_source() -> str:
    return APP_AUDIT.read_text(encoding="utf-8")


def test_forward_tasks_have_bounded_interruption_recovery() -> None:
    source = _source()

    assert source.startswith("#Requires -RunAsAdministrator")
    assert "-RestartCount $RestartCount" in source
    assert "-RestartInterval (New-TimeSpan -Minutes $RestartMinutes)" in source
    assert source.count("-RestartCount 3") == 2
    assert source.count("-RestartMinutes 5") == 2


def test_forward_task_registration_verifies_recovery_contract() -> None:
    source = _source()

    assert "$confirmed.Settings.RestartCount" in source
    assert "$confirmed.Settings.RestartInterval" in source
    assert "[Xml.XmlConvert]::ToTimeSpan" in source
    assert "scheduled task restart count was not retained" in source
    assert "scheduled task restart interval was not retained" in source
    assert "scheduled task execution limit was not retained" in source


def test_forward_task_registration_verifies_exact_action_and_single_instance() -> None:
    source = _source()

    assert "$expectedArguments" in source
    assert "$confirmed.Actions.Count -ne 1" in source
    assert "$confirmed.Actions[0].Execute" in source
    assert "$confirmed.Actions[0].Arguments" in source
    assert "$confirmed.Actions[0].WorkingDirectory" in source
    assert "$confirmed.Settings.MultipleInstances" in source
    assert "$confirmed.Settings.StartWhenAvailable" in source
    assert "scheduled task action was not retained" in source
    assert "scheduled task single-instance contract was not retained" in source
    assert "scheduled task delayed-start contract was not retained" in source
    assert "$confirmed.Principal.LogonType" in source
    assert "$confirmed.Principal.RunLevel" in source
    assert "$confirmed.Principal.UserId" in source
    assert "scheduled task disconnected-session principal was not retained" in source


def test_forward_task_registration_verifies_exact_weekly_trigger() -> None:
    source = _source()

    assert "$confirmedTriggers = @($confirmed.Triggers)" in source
    assert "$confirmedTriggers.Count -eq 1" in source
    assert "$confirmedTriggers[0].Enabled -eq $true" in source
    assert "[int]$confirmedTriggers[0].DaysOfWeek -eq 62" in source
    assert "[int]$confirmedTriggers[0].WeeksInterval -eq 1" in source
    assert "$startBoundary.TimeOfDay -eq [TimeSpan]::Parse($At)" in source
    assert "scheduled task weekly trigger was not retained" in source


def test_forward_tasks_remain_read_only_and_live_disabled() -> None:
    source = _source()

    assert "-LogonType S4U -RunLevel Limited" in source
    assert "without storing a" in source
    assert "Keep the separate QMT restart task interactive" in source
    assert "LIVE_DISABLED" in source
    assert "never access an account or order transport" in source


def test_forward_tasks_pin_an_absolute_python_and_write_registration_receipt() -> None:
    source = _source()
    runner = _runner_source()

    assert "function Resolve-ForwardPython" in source
    assert "$PinnedPython = Resolve-ForwardPython" in source
    assert '-PythonExe "{2}"' in source
    assert "forward_task_registration.json" in source
    assert "python_executable = $PinnedPython" in source
    assert "registered_at = $registeredAt" in source
    assert "real_order_transport_enabled = $false" in source
    assert "[string]$PythonExe = ''" in runner
    assert "[IO.Path]::GetFullPath($PythonExe)" in runner
    assert "Forward-paper Python executable not found" in runner
    assert "python={5}" in runner


def test_forward_runner_logs_a_unique_process_identity_on_every_line() -> None:
    source = _runner_source()

    assert "$InvocationId = [Guid]::NewGuid().ToString('N')" in source
    assert "[run={2} pid={3}]" in source
    assert "$InvocationId, $PID, $Message" in source
    assert "invocation started session={0}" in source
    assert "coverage_wait_minutes={1}" in source
    assert "data_gate_attempts={3}" in source


def test_forward_runner_mutex_contention_is_retryable_not_success() -> None:
    source = _runner_source()
    start = source.index("if (-not $acquired)")
    end = source.index("$qmtData = Resolve-QmtDataDirectory", start)
    gate = source[start:end]

    assert "NO_SAMPLE_PHASE_CONCURRENCY_BLOCKED" in gate
    assert "temporary failure permits scheduled retry" in gate
    assert "exit 75" in gate
    assert "exit 0" not in gate


def test_forward_runner_refuses_legacy_execution_while_app_owns_it() -> None:
    source = _runner_source()

    assert "function Test-AppForwardRuntimeOwner" in source
    assert "chanlun-v3-forward-execution-owner/v1" in source
    assert "APP_RUNTIME_OWNS_FORWARD" in source
    assert "exit 76" in source
    assert "real_order_transport_enabled -eq $false" in source


def test_app_migration_is_gated_and_removes_all_legacy_runtime_tasks() -> None:
    source = _migration_source()

    assert source.startswith("#Requires -RunAsAdministrator")
    assert "function Get-JsonHttpDocument" in source
    assert "catch [Net.WebException]" in source
    assert "Invoke-RestMethod -Uri" not in source
    assert "chanlun-v3-forward-scheduler/app-runtime-contract/v1" in source
    assert "forward.execution_owner -ne 'APP_RUNTIME'" in source
    assert "forward.ready -ne $true" in source
    assert "chanlun-qmt-runtime/app-runtime-contract/v1" in source
    assert "qmtRuntime.execution_owner -ne 'APP_RUNTIME'" in source
    assert "qmtRuntime.ready -ne $true" in source
    assert "forward and QMT runtime ownership do not belong" in source
    assert "no task was changed" in source
    assert source.count("Unregister-ScheduledTask") == 1
    assert "foreach ($taskName in $TaskNames)" in source
    assert "Chanlun-QMT-DailyRestart" in source
    assert "qmt_bootstrap_task_preserved = $false" in source
    assert "qmt_legacy_task_absent = $true" in source
    assert "forward_app_migration.json" in source
    assert "real_order_transport_enabled = $false" in source
    assert "live_status = 'LIVE_DISABLED'" in source


def test_app_forward_audit_is_read_only_and_checks_single_ownership() -> None:
    source = _app_audit_source()

    for mutation in (
        "Register-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Enable-ScheduledTask",
        "Disable-ScheduledTask",
        "Unregister-ScheduledTask",
    ):
        assert mutation not in source
    assert "chanlun-v3-forward-scheduler/app-runtime-contract/v1" in source
    assert "function Get-JsonHttpDocument" in source
    assert "catch [Net.WebException]" in source
    assert "Invoke-RestMethod -Uri" not in source
    assert "APP_FORWARD_OWNER_HEARTBEAT_STALE" in source
    assert "LEGACY_FORWARD_TASKS_STILL_PRESENT" in source
    assert "APP_QMT_OWNER_HEARTBEAT_STALE" in source
    assert "LEGACY_QMT_TASK_STILL_PRESENT" in source
    assert "chanlun-qmt-runtime/app-runtime-contract/v1" in source
    assert "Chanlun-QMT-DailyRestart" in source
    assert "real_order_transport_enabled = $false" in source
    assert "live_status = 'LIVE_DISABLED'" in source


def test_forward_evaluator_waits_for_the_shared_archive_readiness_gate() -> None:
    source = _runner_source()

    assert "$forwardArchive = $ready.components.forward_archive" in source
    assert "$forwardArchiveReady = $forwardArchive.ready -eq $true" in source
    assert "$forwardArchiveReason = [string]$forwardArchive.reason_code" in source
    assert "$forwardArchiveReady -and" in source
    assert "'forward_archive_pending'" in source
    assert "$forwardDelivery = $ready.components.forward_delivery" in source
    assert "NON_TRADING_SESSION_NOT_DUE" in source
    assert "CAPTURE_MISSING_AFTER_DUE" in source
    assert "[DateTimeOffset]::Now -ge $ExpectedClose" in source
    assert "NO_SAMPLE_NON_TRADING_SESSION" in source
    assert "NO_SAMPLE_DELIVERY_BLOCKED" in source
    assert "$implementationContinuityBlocked" in source
    assert "CAPTURE_IMPLEMENTATION_PROVENANCE_UNATTESTED" in source
    assert "IMPLEMENTATION_CHANGED_SINCE_CAPTURE" in source
    assert "CURRENT_IMPLEMENTATION_PROVENANCE_UNAVAILABLE" in source
    assert "implementation_continuity_blocked" in source
    assert source.index("$forwardDelivery = $ready.components.forward_delivery") < (
        source.index("$marketDataAsOf = [string]$screening.market_data_as_of")
    )
    assert "forward_archive_reason={5}" in source
    assert "forward_delivery_reason={6}" in source
    assert "forward_session={1}" in source


def test_forward_task_audit_is_read_only_and_checks_the_full_contract() -> None:
    source = _audit_source()

    for mutation in (
        "Register-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Enable-ScheduledTask",
        "Disable-ScheduledTask",
        "Unregister-ScheduledTask",
    ):
        assert mutation not in source
    assert "Get-ScheduledTask -TaskName" in source
    assert "Get-ScheduledTaskInfo -TaskName" in source
    assert "SCHEDULED_TASK_PRINCIPAL_MISMATCH" in source
    assert "SCHEDULED_TASK_ACTION_MISMATCH" in source
    assert "SCHEDULED_TASK_RECOVERY_MISMATCH" in source
    assert "SCHEDULED_TASK_TRIGGER_MISMATCH" in source
    assert "[int]$trigger.DaysOfWeek -eq 62" in source
    assert "[string]$task.Principal.LogonType -ne 'S4U'" in source
    assert "real_order_transport_enabled = $false" in source
    assert "live_status = 'LIVE_DISABLED'" in source
    assert "configuration_ready = $configurationReady" in source
    assert "operationally_verified = $operationallyVerified" in source
    assert "first_success_after_registration = $forwardTasksVerified" in source
    assert "AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION" in source
    assert "LATEST_TASK_RUN_FAILED" in source
    assert "00041303" in source
    assert "APP_RUNTIME_OWNS_FORWARD" in source
    assert "$lastRunReason = 'NEVER_RUN'" in source
    assert "pinned_python_executable = $pinnedPython" in source
    assert "upstream_qmt = $qmtObservation" in source
    assert "audit_qmt_restart_task.ps1" in source
    assert "yyyy-MM-ddTHH:mm:ss.ffffffK" in source
