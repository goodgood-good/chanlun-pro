# Read-only audit for the two frozen V3 forward-paper scheduled tasks.
#
# This script never registers, starts, stops, enables, disables, or otherwise
# mutates a task.  It only observes the installed definitions and returns one
# machine-readable fail-closed verdict for Web readiness and operator review.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = [IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot 'run_v3_forward_paper_daily.ps1')
)
$RegistrationPath = Join-Path $ProjectRoot '.cache\chanlun_v3_scheduler\forward_task_registration.json'
$QmtAuditPath = Join-Path $PSScriptRoot 'audit_qmt_restart_task.ps1'
$Schema = 'chanlun-v3-forward-scheduler-readiness/v1'
$ContractId = 'chanlun-v3-forward-scheduler/windows-task-contract/v1'
$RefusedResult = [uint32]::Parse(
    '800710E0',
    [Globalization.NumberStyles]::HexNumber,
    [Globalization.CultureInfo]::InvariantCulture
)
$HasNotRunResult = [uint32]::Parse(
    '00041303',
    [Globalization.NumberStyles]::HexNumber,
    [Globalization.CultureInfo]::InvariantCulture
)
$AppRuntimeOwnerResult = [uint32]76

$contracts = @(
    [ordered]@{
        Name = 'Chanlun-V3-Forward-Capture'
        Phase = 'Capture'
        At = [TimeSpan]::Parse('09:10:00')
        ExecutionTimeLimit = 'PT3H'
    },
    [ordered]@{
        Name = 'Chanlun-V3-Forward-Evaluate'
        Phase = 'Evaluate'
        At = [TimeSpan]::Parse('15:20:00')
        ExecutionTimeLimit = 'PT12H'
    }
)

function Add-Reason {
    param(
        [Collections.Generic.List[string]]$Reasons,
        [Parameter(Mandatory = $true)]
        [string]$Reason
    )
    if (-not $Reasons.Contains($Reason)) { $Reasons.Add($Reason) }
}

function Get-LastResultReason {
    param([Parameter(Mandatory = $true)][long]$Value)
    if ($Value -eq 0) { return 'SUCCESS' }
    if ([uint32]$Value -eq $HasNotRunResult) { return 'NEVER_RUN' }
    if ([uint32]$Value -eq $RefusedResult) {
        return 'OPERATOR_OR_ADMINISTRATOR_REFUSED_REQUEST'
    }
    if ([uint32]$Value -eq $AppRuntimeOwnerResult) {
        return 'APP_RUNTIME_OWNS_FORWARD'
    }
    return 'NONZERO_TASK_RESULT'
}

function Test-SafetyFields {
    param([Parameter(Mandatory = $true)][object]$Document)
    return (
        $Document.real_account_accessed -eq $false -and
        $Document.real_order_transport_enabled -eq $false -and
        $Document.automated_order_authorized -eq $false -and
        [string]$Document.live_status -eq 'LIVE_DISABLED'
    )
}

$registrationReasons = [Collections.Generic.List[string]]::new()
$registration = $null
$registeredAt = $null
$pinnedPython = $null
if (-not (Test-Path -LiteralPath $RegistrationPath -PathType Leaf)) {
    Add-Reason $registrationReasons 'REGISTRATION_RECEIPT_MISSING'
} else {
    try {
        $registration = Get-Content -LiteralPath $RegistrationPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        $registeredAt = [DateTimeOffset]::Parse(
            [string]$registration.registered_at,
            [Globalization.CultureInfo]::InvariantCulture
        )
        $pinnedPython = [IO.Path]::GetFullPath(
            [string]$registration.python_executable
        )
        $receiptTasks = @($registration.tasks)
        if (
            [string]$registration.schema -ne 'chanlun-v3-forward-task-registration/v1' -or
            [IO.Path]::GetFullPath([string]$registration.runner_path) -ne $Runner -or
            -not (Test-Path -LiteralPath $pinnedPython -PathType Leaf) -or
            [string]$registration.logon_type -ne 'S4U' -or
            [string]$registration.run_level -ne 'Limited' -or
            -not (Test-SafetyFields $registration) -or
            $receiptTasks.Count -ne 2
        ) {
            throw 'registration receipt fields changed'
        }
        foreach ($contract in $contracts) {
            $receiptTask = @(
                $receiptTasks | Where-Object {
                    [string]$_.name -eq $contract.Name
                }
            )
            $expectedReceiptArguments = (
                '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Phase {1} -PythonExe "{2}"' -f (
                    $Runner,
                    $contract.Phase,
                    $pinnedPython
                )
            )
            if (
                $receiptTask.Count -ne 1 -or
                [string]$receiptTask[0].phase -ne $contract.Phase.ToUpperInvariant() -or
                [string]$receiptTask[0].action_arguments -ne $expectedReceiptArguments
            ) {
                throw 'registration receipt task fields changed'
            }
        }
    } catch {
        $registration = $null
        $registeredAt = $null
        $pinnedPython = $null
        Add-Reason $registrationReasons 'REGISTRATION_RECEIPT_INVALID'
    }
}

$taskResults = @()
foreach ($contract in $contracts) {
    $reasons = [Collections.Generic.List[string]]::new()
    $operationalReasons = [Collections.Generic.List[string]]::new()
    foreach ($registrationReason in $registrationReasons) {
        Add-Reason $reasons $registrationReason
        Add-Reason $operationalReasons $registrationReason
    }
    $expectedArguments = (
        '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Phase {1} -PythonExe "{2}"' -f (
            $Runner,
            $contract.Phase,
            $pinnedPython
        )
    )
    $task = Get-ScheduledTask -TaskName $contract.Name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Add-Reason -Reasons $reasons -Reason 'SCHEDULED_TASK_MISSING'
        Add-Reason -Reasons $operationalReasons -Reason 'SCHEDULED_TASK_MISSING'
        $taskResults += [ordered]@{
            name = $contract.Name
            phase = $contract.Phase.ToUpperInvariant()
            ready = $false
            configuration_ready = $false
            operationally_verified = $false
            operational_status = 'not_verified'
            status = 'not_ready'
            reason_codes = @($reasons)
            operational_reason_codes = @($operationalReasons)
            state = 'MISSING'
            logon_type = $null
            run_level = $null
            last_run_time = $null
            last_task_result = $null
            last_task_result_hex = $null
            last_run_reason_code = 'NEVER_OBSERVED'
            next_run_time = $null
        }
        continue
    }

    $state = [string]$task.State
    if ($state -eq 'Disabled') {
        Add-Reason -Reasons $reasons -Reason 'SCHEDULED_TASK_DISABLED'
    }
    if (
        [string]$task.Principal.LogonType -ne 'S4U' -or
        [string]$task.Principal.RunLevel -ne 'Limited' -or
        [string]::IsNullOrWhiteSpace([string]$task.Principal.UserId)
    ) {
        Add-Reason -Reasons $reasons -Reason 'SCHEDULED_TASK_PRINCIPAL_MISMATCH'
    }

    $actions = @($task.Actions)
    if (
        $actions.Count -ne 1 -or
        [string]$actions[0].Execute -ne 'powershell.exe' -or
        [string]$actions[0].Arguments -ne $expectedArguments -or
        -not [string]::IsNullOrWhiteSpace([string]$actions[0].WorkingDirectory)
    ) {
        Add-Reason -Reasons $reasons -Reason 'SCHEDULED_TASK_ACTION_MISMATCH'
    }

    if (
        [string]$task.Settings.MultipleInstances -ne 'IgnoreNew' -or
        [int]$task.Settings.RestartCount -ne 3 -or
        [string]$task.Settings.RestartInterval -ne 'PT5M' -or
        [string]$task.Settings.ExecutionTimeLimit -ne $contract.ExecutionTimeLimit -or
        $task.Settings.StartWhenAvailable -ne $true
    ) {
        Add-Reason -Reasons $reasons -Reason 'SCHEDULED_TASK_RECOVERY_MISMATCH'
    }

    $triggers = @($task.Triggers)
    $triggerValid = $triggers.Count -eq 1
    if ($triggerValid) {
        $trigger = $triggers[0]
        try {
            $startBoundary = [DateTimeOffset]::Parse(
                [string]$trigger.StartBoundary,
                [Globalization.CultureInfo]::InvariantCulture
            )
            $triggerValid = (
                $trigger.Enabled -eq $true -and
                [int]$trigger.DaysOfWeek -eq 62 -and
                [int]$trigger.WeeksInterval -eq 1 -and
                $startBoundary.TimeOfDay -eq $contract.At
            )
        } catch {
            $triggerValid = $false
        }
    }
    if (-not $triggerValid) {
        Add-Reason -Reasons $reasons -Reason 'SCHEDULED_TASK_TRIGGER_MISMATCH'
    }

    foreach ($configurationReason in $reasons) {
        Add-Reason $operationalReasons $configurationReason
    }

    $info = Get-ScheduledTaskInfo -TaskName $contract.Name -ErrorAction SilentlyContinue
    $lastRunTime = $null
    $lastTaskResult = $null
    $lastTaskResultHex = $null
    $lastRunReason = 'NEVER_RUN'
    $nextRunTime = $null
    if ($null -ne $info) {
        if ([uint32]$info.LastTaskResult -eq $HasNotRunResult) {
            $lastTaskResult = [long]$info.LastTaskResult
            $lastTaskResultHex = '0x{0:X8}' -f ([uint32]$info.LastTaskResult)
            $lastRunReason = 'NEVER_RUN'
        } elseif ($info.LastRunTime -ne [DateTime]::MinValue) {
            $lastRunTime = $info.LastRunTime.ToString('o')
            $lastTaskResult = [long]$info.LastTaskResult
            $lastTaskResultHex = '0x{0:X8}' -f ([uint32]$info.LastTaskResult)
            $lastRunReason = Get-LastResultReason -Value $lastTaskResult
        }
        if ($info.NextRunTime -ne [DateTime]::MinValue) {
            $nextRunTime = $info.NextRunTime.ToString('o')
        }
    }

    if ($null -eq $registeredAt) {
        # The exact receipt reason was already copied above.
    } elseif ($null -eq $lastRunTime) {
        Add-Reason $operationalReasons 'AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION'
    } else {
        $lastRunOffset = [DateTimeOffset]$info.LastRunTime
        if ($lastRunOffset -lt $registeredAt) {
            Add-Reason $operationalReasons 'AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION'
        } elseif ($lastTaskResult -ne 0) {
            Add-Reason $operationalReasons 'LATEST_TASK_RUN_FAILED'
        }
    }

    $taskReady = $reasons.Count -eq 0
    $taskOperationallyVerified = (
        $taskReady -and $operationalReasons.Count -eq 0
    )
    $taskResults += [ordered]@{
        name = $contract.Name
        phase = $contract.Phase.ToUpperInvariant()
        ready = $taskReady
        configuration_ready = $taskReady
        operationally_verified = $taskOperationallyVerified
        operational_status = if ($taskOperationallyVerified) {
            'verified'
        } elseif ($operationalReasons.Contains('AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION')) {
            'awaiting_first_success'
        } else {
            'not_verified'
        }
        status = if ($taskReady) { 'ready' } else { 'not_ready' }
        reason_codes = @($reasons)
        operational_reason_codes = @($operationalReasons)
        state = $state
        logon_type = [string]$task.Principal.LogonType
        run_level = [string]$task.Principal.RunLevel
        last_run_time = $lastRunTime
        last_task_result = $lastTaskResult
        last_task_result_hex = $lastTaskResultHex
        last_run_reason_code = $lastRunReason
        next_run_time = $nextRunTime
    }
}

$qmtObservation = $null
try {
    if (-not (Test-Path -LiteralPath $QmtAuditPath -PathType Leaf)) {
        throw 'QMT scheduler audit script is missing'
    }
    $qmtOutput = @(& powershell.exe `
        -NoProfile `
        -NonInteractive `
        -ExecutionPolicy Bypass `
        -File $QmtAuditPath 2>$null)
    $qmtExit = $LASTEXITCODE
    if ($qmtExit -notin @(0, 3) -or $qmtOutput.Count -eq 0) {
        throw ('QMT scheduler audit exited with {0}' -f $qmtExit)
    }
    $qmtObservation = ($qmtOutput -join "`n") | ConvertFrom-Json
    if (
        [string]$qmtObservation.schema -ne 'chanlun-qmt-restart-scheduler-readiness/v1' -or
        -not (Test-SafetyFields $qmtObservation) -or
        $qmtObservation.configuration_ready -isnot [bool] -or
        $qmtObservation.operationally_verified -isnot [bool] -or
        $qmtObservation.upstream_ready_now -isnot [bool]
    ) {
        throw 'QMT scheduler observation is invalid'
    }
} catch {
    $qmtObservation = [ordered]@{
        schema = 'chanlun-qmt-restart-scheduler-readiness/v1'
        ready = $false
        status = 'unresolved'
        reason_code = 'QMT_SCHEDULER_OBSERVATION_UNAVAILABLE'
        reason_codes = @('QMT_SCHEDULER_OBSERVATION_UNAVAILABLE')
        configuration_ready = $false
        operationally_verified = $false
        operational_status = 'not_verified'
        operational_reason_codes = @('QMT_SCHEDULER_OBSERVATION_UNAVAILABLE')
        upstream_ready_now = $false
        upstream_reason_code = 'QMT_SCHEDULER_OBSERVATION_UNAVAILABLE'
        error = ('{0}: {1}' -f $_.Exception.GetType().Name, $_.Exception.Message)
        real_account_accessed = $false
        real_order_transport_enabled = $false
        automated_order_authorized = $false
        live_status = 'LIVE_DISABLED'
    }
}

$allReasons = [Collections.Generic.List[string]]::new()
$allOperationalReasons = [Collections.Generic.List[string]]::new()
foreach ($taskResult in $taskResults) {
    foreach ($reason in @($taskResult.reason_codes)) {
        Add-Reason -Reasons $allReasons -Reason $reason
    }
    foreach ($reason in @($taskResult.operational_reason_codes)) {
        Add-Reason -Reasons $allOperationalReasons -Reason $reason
    }
}
$configurationReady = @(
    $taskResults | Where-Object { $_.configuration_ready -ne $true }
).Count -eq 0
$forwardTasksVerified = @(
    $taskResults | Where-Object { $_.operationally_verified -ne $true }
).Count -eq 0
if ($qmtObservation.configuration_ready -ne $true) {
    Add-Reason $allOperationalReasons 'UPSTREAM_QMT_CONFIGURATION_NOT_READY'
}
if ($qmtObservation.operationally_verified -ne $true) {
    if ([string]$qmtObservation.operational_status -eq 'awaiting_first_success') {
        Add-Reason $allOperationalReasons 'UPSTREAM_QMT_AWAITING_FIRST_SUCCESS'
    } else {
        Add-Reason $allOperationalReasons 'UPSTREAM_QMT_NOT_OPERATIONALLY_VERIFIED'
    }
}
$operationallyVerified = (
    $configurationReady -and
    $forwardTasksVerified -and
    $qmtObservation.configuration_ready -eq $true -and
    $qmtObservation.operationally_verified -eq $true
)
$reasonCode = if ($configurationReady) {
    'READY'
} else {
    [string]$allReasons[0]
}
$operationalStatus = if ($operationallyVerified) {
    'verified'
} elseif (
    $configurationReady -and
    $qmtObservation.configuration_ready -eq $true -and
    -not $allOperationalReasons.Contains('LATEST_TASK_RUN_FAILED') -and
    (
        $allOperationalReasons.Contains('AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION') -or
        [string]$qmtObservation.operational_status -eq 'awaiting_first_success'
    )
) {
    'awaiting_first_success'
} else {
    'not_verified'
}
$payload = [ordered]@{
    schema = $Schema
    contract_id = $ContractId
    # Python's ISO parser accepts microseconds (six digits), while .NET's
    # round-trip format emits seven fractional digits.  Publish one canonical
    # cross-runtime timestamp instead of weakening the consumer validator.
    observed_at = (Get-Date).ToString(
        'yyyy-MM-ddTHH:mm:ss.ffffffK',
        [Globalization.CultureInfo]::InvariantCulture
    )
    ready = $configurationReady
    status = if ($configurationReady) { 'ready' } else { 'not_ready' }
    reason_code = $reasonCode
    reason_codes = @($allReasons)
    configuration_ready = $configurationReady
    operationally_verified = $operationallyVerified
    operational_status = $operationalStatus
    operational_reason_codes = @($allOperationalReasons)
    first_success_after_registration = $forwardTasksVerified
    registered_at = if ($null -eq $registeredAt) {
        $null
    } else {
        $registeredAt.ToString(
            'yyyy-MM-ddTHH:mm:ss.ffffffK',
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    pinned_python_executable = $pinnedPython
    upstream_qmt = $qmtObservation
    tasks = @($taskResults)
    task_count = $taskResults.Count
    real_account_accessed = $false
    real_order_transport_enabled = $false
    automated_order_authorized = $false
    live_status = 'LIVE_DISABLED'
}
$payload | ConvertTo-Json -Depth 8 -Compress
if (-not $configurationReady) { exit 3 }
