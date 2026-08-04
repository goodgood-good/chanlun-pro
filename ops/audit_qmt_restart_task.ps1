# Read-only audit of the interactive QMT/web pre-market restart dependency.
#
# This script never registers, starts, stops, enables, disables or otherwise
# mutates a task or process.  It separates four facts that must not be
# conflated: exact task configuration, post-registration success evidence,
# the latest task result, and loopback web readiness at observation time.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TaskName = 'Chanlun-QMT-DailyRestart'
$ScriptPath = [IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot 'restart_qmt_daily.ps1')
)
$RegistrationPath = Join-Path $ProjectRoot '.cache\chanlun_v3_scheduler\qmt_restart_registration.json'
$SuccessPath = Join-Path $ProjectRoot '.cache\chanlun_v3_scheduler\qmt_restart_success.json'
$Schema = 'chanlun-qmt-restart-scheduler-readiness/v1'
$ContractId = 'chanlun-qmt-restart-scheduler/windows-task-contract/v1'
$ExpectedArguments = (
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -WebReadinessTimeoutSeconds 1800 -CatchUpWindowMinutes 90' -f $ScriptPath
)
$ExpectedRunAt = [TimeSpan]::Parse('08:30:00')
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

function Add-Reason {
    param(
        [Collections.Generic.List[string]]$Reasons,
        [Parameter(Mandatory = $true)][string]$Reason
    )
    if (-not $Reasons.Contains($Reason)) { $Reasons.Add($Reason) }
}

function Get-StrictJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
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

function Get-LastResultReason {
    param([Parameter(Mandatory = $true)][long]$Value)
    if ($Value -eq 0) { return 'SUCCESS' }
    if ([uint32]$Value -eq $HasNotRunResult) { return 'NEVER_RUN' }
    if ([uint32]$Value -eq $RefusedResult) {
        return 'OPERATOR_OR_ADMINISTRATOR_REFUSED_REQUEST'
    }
    return 'NONZERO_TASK_RESULT'
}

$configurationReasons = [Collections.Generic.List[string]]::new()
$operationalReasons = [Collections.Generic.List[string]]::new()
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$state = 'MISSING'
$logonType = $null
$runLevel = $null
if ($null -eq $task) {
    Add-Reason $configurationReasons 'SCHEDULED_TASK_MISSING'
} else {
    $state = [string]$task.State
    $logonType = [string]$task.Principal.LogonType
    $runLevel = [string]$task.Principal.RunLevel
    if ($state -eq 'Disabled') {
        Add-Reason $configurationReasons 'SCHEDULED_TASK_DISABLED'
    }
    # miniQMT is a GUI process whose saved login only works in the current
    # user's non-elevated interactive desktop.  S4U is deliberately forbidden
    # for this one upstream task even though the two forward tasks use S4U.
    if (
        $logonType -ne 'Interactive' -or
        $runLevel -ne 'Limited' -or
        [string]::IsNullOrWhiteSpace([string]$task.Principal.UserId)
    ) {
        Add-Reason $configurationReasons 'SCHEDULED_TASK_PRINCIPAL_MISMATCH'
    }
    $actions = @($task.Actions)
    if (
        $actions.Count -ne 1 -or
        [string]$actions[0].Execute -ne 'powershell.exe' -or
        [string]$actions[0].Arguments -ne $ExpectedArguments -or
        -not [string]::IsNullOrWhiteSpace([string]$actions[0].WorkingDirectory)
    ) {
        Add-Reason $configurationReasons 'SCHEDULED_TASK_ACTION_MISMATCH'
    }
    if (
        [string]$task.Settings.MultipleInstances -ne 'IgnoreNew' -or
        [int]$task.Settings.RestartCount -ne 3 -or
        [string]$task.Settings.RestartInterval -ne 'PT5M' -or
        [string]$task.Settings.ExecutionTimeLimit -ne 'PT45M' -or
        $task.Settings.StartWhenAvailable -ne $true
    ) {
        Add-Reason $configurationReasons 'SCHEDULED_TASK_RECOVERY_MISMATCH'
    }
    $triggers = @($task.Triggers)
    $triggerValid = $triggers.Count -eq 1
    if ($triggerValid) {
        try {
            $startBoundary = [DateTimeOffset]::Parse(
                [string]$triggers[0].StartBoundary,
                [Globalization.CultureInfo]::InvariantCulture
            )
            $triggerValid = (
                $triggers[0].Enabled -eq $true -and
                [int]$triggers[0].DaysOfWeek -eq 62 -and
                [int]$triggers[0].WeeksInterval -eq 1 -and
                $startBoundary.TimeOfDay -eq $ExpectedRunAt
            )
        } catch {
            $triggerValid = $false
        }
    }
    if (-not $triggerValid) {
        Add-Reason $configurationReasons 'SCHEDULED_TASK_TRIGGER_MISMATCH'
    }
}

$registration = $null
$registeredAt = $null
$registrationSha256 = $null
if (-not (Test-Path -LiteralPath $RegistrationPath -PathType Leaf)) {
    Add-Reason $operationalReasons 'REGISTRATION_RECEIPT_MISSING'
} else {
    $registration = Get-StrictJson $RegistrationPath
    try {
        $registeredAt = [DateTimeOffset]::Parse(
            [string]$registration.registered_at,
            [Globalization.CultureInfo]::InvariantCulture
        )
        $registrationValid = (
            [string]$registration.schema -eq 'chanlun-qmt-restart-task-registration/v1' -and
            [string]$registration.task_name -eq $TaskName -and
            [IO.Path]::GetFullPath([string]$registration.script_path) -eq $ScriptPath -and
            [string]$registration.action_arguments -eq $ExpectedArguments -and
            [int]$registration.catch_up_window_minutes -eq 90 -and
            [int]$registration.web_readiness_timeout_seconds -eq 1800 -and
            [int]$registration.execution_time_limit_minutes -eq 45 -and
            [int]$registration.restart_count -eq 3 -and
            [int]$registration.restart_interval_minutes -eq 5 -and
            [string]$registration.logon_type -eq 'Interactive' -and
            [string]$registration.run_level -eq 'Limited' -and
            (Test-SafetyFields $registration)
        )
        if (-not $registrationValid) { throw 'receipt fields changed' }
        $registrationSha256 = 'sha256:{0}' -f (
            (Get-FileHash -LiteralPath $RegistrationPath -Algorithm SHA256).Hash.ToLowerInvariant()
        )
    } catch {
        $registration = $null
        $registeredAt = $null
        $registrationSha256 = $null
        Add-Reason $operationalReasons 'REGISTRATION_RECEIPT_INVALID'
    }
}

$success = $null
$successAt = $null
if (-not (Test-Path -LiteralPath $SuccessPath -PathType Leaf)) {
    Add-Reason $operationalReasons 'SUCCESS_RECEIPT_MISSING'
} elseif ($null -eq $registeredAt) {
    Add-Reason $operationalReasons 'SUCCESS_RECEIPT_UNBOUND'
} else {
    $success = Get-StrictJson $SuccessPath
    try {
        $successAt = [DateTimeOffset]::Parse(
            [string]$success.completed_at,
            [Globalization.CultureInfo]::InvariantCulture
        )
        $successValid = (
            [string]$success.schema -eq 'chanlun-qmt-restart-task-success/v1' -and
            [string]$success.task_name -eq $TaskName -and
            [string]$success.registration_receipt_sha256 -eq $registrationSha256 -and
            $successAt -ge $registeredAt -and
            $success.qmt_restart_completed -eq $true -and
            $success.web_readiness_verified -eq $true -and
            (Test-SafetyFields $success)
        )
        if (-not $successValid) { throw 'success fields changed' }
    } catch {
        $success = $null
        $successAt = $null
        Add-Reason $operationalReasons 'SUCCESS_RECEIPT_INVALID'
    }
}

$lastRunTime = $null
$lastTaskResult = $null
$lastTaskResultHex = $null
$lastRunReason = 'NEVER_RUN'
$nextRunTime = $null
if ($null -ne $task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $info) {
        if ([uint32]$info.LastTaskResult -eq $HasNotRunResult) {
            $lastTaskResult = [long]$info.LastTaskResult
            $lastTaskResultHex = '0x{0:X8}' -f ([uint32]$info.LastTaskResult)
            $lastRunReason = 'NEVER_RUN'
            Add-Reason $operationalReasons 'AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION'
        } elseif ($info.LastRunTime -ne [DateTime]::MinValue) {
            $lastRunTime = $info.LastRunTime.ToString('o')
            $lastRunOffset = [DateTimeOffset]$info.LastRunTime
            $lastTaskResult = [long]$info.LastTaskResult
            $lastTaskResultHex = '0x{0:X8}' -f ([uint32]$info.LastTaskResult)
            $lastRunReason = Get-LastResultReason $lastTaskResult
            if ($null -ne $registeredAt -and $lastRunOffset -lt $registeredAt) {
                Add-Reason $operationalReasons 'AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION'
            } elseif ($lastTaskResult -ne 0) {
                Add-Reason $operationalReasons 'LATEST_TASK_RUN_FAILED'
            }
        } else {
            Add-Reason $operationalReasons 'TASK_NEVER_RUN'
        }
        if ($info.NextRunTime -ne [DateTime]::MinValue) {
            $nextRunTime = $info.NextRunTime.ToString('o')
        }
    }
}

$upstreamReadyNow = $false
$upstreamReason = 'WEB_READINESS_UNAVAILABLE_NOW'
try {
    $health = Invoke-RestMethod `
        -Uri 'http://127.0.0.1:9900/readyz?market=a' `
        -Method Get `
        -TimeoutSec 5 `
        -ErrorAction Stop
    if ($health.status -eq 'ready') {
        $upstreamReadyNow = $true
        $upstreamReason = 'READY'
    } else {
        $upstreamReason = 'WEB_NOT_READY_NOW'
    }
} catch {
    $upstreamReason = 'WEB_READINESS_UNAVAILABLE_NOW'
}

$configurationReady = $configurationReasons.Count -eq 0
$operationallyVerified = (
    $configurationReady -and
    $operationalReasons.Count -eq 0 -and
    $null -ne $successAt -and
    $lastTaskResult -eq 0
)
$configurationReason = if ($configurationReady) {
    'READY'
} else {
    [string]$configurationReasons[0]
}
$operationalStatus = if ($operationallyVerified) {
    'verified'
} elseif ($operationalReasons.Contains('LATEST_TASK_RUN_FAILED')) {
    'not_verified'
} elseif (
    $operationalReasons.Contains('REGISTRATION_RECEIPT_MISSING') -or
    $operationalReasons.Contains('SUCCESS_RECEIPT_MISSING') -or
    $operationalReasons.Contains('TASK_NEVER_RUN') -or
    $operationalReasons.Contains('AWAITING_FIRST_SUCCESS_AFTER_REGISTRATION')
) {
    'awaiting_first_success'
} else {
    'not_verified'
}

$payload = [ordered]@{
    schema = $Schema
    contract_id = $ContractId
    observed_at = (Get-Date).ToString(
        'yyyy-MM-ddTHH:mm:ss.ffffffK',
        [Globalization.CultureInfo]::InvariantCulture
    )
    ready = $configurationReady
    status = if ($configurationReady) { 'ready' } else { 'not_ready' }
    reason_code = $configurationReason
    reason_codes = @($configurationReasons)
    configuration_ready = $configurationReady
    operationally_verified = $operationallyVerified
    operational_status = $operationalStatus
    operational_reason_codes = @($operationalReasons)
    upstream_ready_now = $upstreamReadyNow
    upstream_reason_code = $upstreamReason
    task = [ordered]@{
        name = $TaskName
        state = $state
        logon_type = $logonType
        run_level = $runLevel
        last_run_time = $lastRunTime
        last_task_result = $lastTaskResult
        last_task_result_hex = $lastTaskResultHex
        last_run_reason_code = $lastRunReason
        next_run_time = $nextRunTime
    }
    registration_receipt = if ($null -eq $registration) { $null } else { $RegistrationPath }
    registration_receipt_sha256 = $registrationSha256
    success_receipt = if ($null -eq $success) { $null } else { $SuccessPath }
    success_at = if ($null -eq $successAt) {
        $null
    } else {
        $successAt.ToString(
            'yyyy-MM-ddTHH:mm:ss.ffffffK',
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    real_account_accessed = $false
    real_order_transport_enabled = $false
    automated_order_authorized = $false
    live_status = 'LIVE_DISABLED'
}
$payload | ConvertTo-Json -Depth 8 -Compress
if (-not $configurationReady) { exit 3 }
