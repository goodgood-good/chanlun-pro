# ============================================================================
# register_qmt_restart_task.ps1
#
# Registers a Windows Scheduled Task that runs restart_qmt_daily.ps1 every
# trading day (Mon-Fri) at 08:30, inside the current user's interactive
# desktop session (miniQMT is a GUI app and needs an interactive session).
#
# RUN THIS ONCE in a NORMAL (non-admin) PowerShell window:
#     powershell -ExecutionPolicy Bypass -File .\register_qmt_restart_task.ps1
# The task runs non-elevated as the current user, so administrator rights are
# NOT needed -- and must not be used: miniQMT must be launched non-elevated for
# its saved auto-login to work on restart.
# ============================================================================

[CmdletBinding()]
param(
    [ValidateRange(1, 720)]
    [int]$CatchUpWindowMinutes = 90,
    [ValidateRange(30, 1800)]
    [int]$WebReadinessTimeoutSeconds = 1800,
    [ValidateRange(15, 180)]
    [int]$ExecutionTimeLimitMinutes = 45,
    [ValidateRange(1, 10)]
    [int]$RestartCount = 3,
    [ValidateRange(1, 60)]
    [int]$RestartIntervalMinutes = 5
)

$ErrorActionPreference = 'Stop'

$TaskName   = 'Chanlun-QMT-DailyRestart'
$ScriptPath = Join-Path $PSScriptRoot 'restart_qmt_daily.ps1'
$RunAt      = '08:30'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReceiptDir = Join-Path $ProjectRoot '.cache\chanlun_v3_scheduler'
$ReceiptPath = Join-Path $ReceiptDir 'qmt_restart_registration.json'

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    Write-Error "restart script not found: $ScriptPath"
    exit 1
}

# The child script may spend the full readiness timeout after QMT warm-up,
# preflight, process shutdown and web launch.  A Scheduled Task limit shorter
# than that window kills a healthy cold start before it can publish readiness.
$minimumExecutionMinutes = (
    [int][Math]::Ceiling($WebReadinessTimeoutSeconds / 60.0) + 10
)
if ($ExecutionTimeLimitMinutes -lt $minimumExecutionMinutes) {
    throw (
        'ExecutionTimeLimitMinutes must be at least {0} for a {1}s web readiness timeout' -f (
            $minimumExecutionMinutes,
            $WebReadinessTimeoutSeconds
        )
    )
}

$expectedArguments = (
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -WebReadinessTimeoutSeconds {1} -CatchUpWindowMinutes {2}' -f (
        $ScriptPath,
        $WebReadinessTimeoutSeconds,
        $CatchUpWindowMinutes
    )
)
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument $expectedArguments

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $RunAt

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount $RestartCount `
    -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes)

# RunLevel Limited (NOT Highest): the task -- and QMT, which it launches --
# must run non-elevated, exactly like the user starting QMT by hand. An
# elevated QMT does not pick up the saved auto-login and comes up not logged in.
$principal = New-ScheduledTaskPrincipal `
    -UserId ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType Interactive -RunLevel Limited

$regArgs = @{
    TaskName    = $TaskName
    Action      = $action
    Trigger     = $trigger
    Settings    = $settings
    Principal   = $principal
    Description = 'chanlun-pro: pre-market restart of miniQMT + web project to avoid the 0xC0000409 crash caused by QMT terminal degradation.'
    Force       = $true
}
try {
    $registeredTask = Register-ScheduledTask @regArgs -ErrorAction Stop
    if ($null -eq $registeredTask) {
        throw 'Register-ScheduledTask returned no task object'
    }
    $confirmedTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($null -eq $confirmedTask -or $confirmedTask.State -eq 'Disabled') {
        throw 'the scheduled task is missing or disabled after registration'
    }
    $confirmedActions = @($confirmedTask.Actions)
    if (
        $confirmedActions.Count -ne 1 -or
        [string]$confirmedActions[0].Execute -ne 'powershell.exe' -or
        [string]$confirmedActions[0].Arguments -ne $expectedArguments -or
        -not [string]::IsNullOrWhiteSpace(
            [string]$confirmedActions[0].WorkingDirectory
        )
    ) {
        throw 'the scheduled task action was not retained exactly'
    }
    if (
        [string]$confirmedTask.Principal.LogonType -ne 'Interactive' -or
        [string]$confirmedTask.Principal.RunLevel -ne 'Limited' -or
        [string]::IsNullOrWhiteSpace([string]$confirmedTask.Principal.UserId)
    ) {
        throw 'the interactive non-elevated QMT principal was not retained'
    }
    if (
        [string]$confirmedTask.Settings.MultipleInstances -ne 'IgnoreNew' -or
        $confirmedTask.Settings.StartWhenAvailable -ne $true -or
        [int]$confirmedTask.Settings.RestartCount -ne $RestartCount -or
        [Xml.XmlConvert]::ToTimeSpan(
            [string]$confirmedTask.Settings.RestartInterval
        ) -ne (New-TimeSpan -Minutes $RestartIntervalMinutes) -or
        [Xml.XmlConvert]::ToTimeSpan(
            [string]$confirmedTask.Settings.ExecutionTimeLimit
        ) -ne (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes)
    ) {
        throw 'the QMT task recovery contract was not retained'
    }
    $confirmedTriggers = @($confirmedTask.Triggers)
    $triggerRetained = $confirmedTriggers.Count -eq 1
    if ($triggerRetained) {
        try {
            $startBoundary = [DateTimeOffset]::Parse(
                [string]$confirmedTriggers[0].StartBoundary,
                [Globalization.CultureInfo]::InvariantCulture
            )
            $triggerRetained = (
                $confirmedTriggers[0].Enabled -eq $true -and
                [int]$confirmedTriggers[0].DaysOfWeek -eq 62 -and
                [int]$confirmedTriggers[0].WeeksInterval -eq 1 -and
                $startBoundary.TimeOfDay -eq [TimeSpan]::Parse($RunAt)
            )
        } catch {
            $triggerRetained = $false
        }
    }
    if (-not $triggerRetained) {
        throw 'the QMT weekly trigger was not retained'
    }
} catch {
    $message = [string]$_.Exception.Message
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $taskFile = Join-Path $env:WINDIR ('System32\Tasks\{0}' -f $TaskName)
    $taskOwner = $null
    if (Test-Path -LiteralPath $taskFile -PathType Leaf) {
        try {
            $taskOwner = (Get-Acl -LiteralPath $taskFile).Owner
        } catch {
            $taskOwner = $null
        }
    }
    if (
        $null -ne $existingTask -and
        $message -match '(?i)(access.*denied|拒绝访问)'
    ) {
        $ownerLabel = if ([string]::IsNullOrWhiteSpace($taskOwner)) {
            'an elevated principal'
        } else {
            $taskOwner
        }
        Write-Error ((
                "scheduled task registration failed because the existing task is owned by {0} " +
                "and the current non-elevated user cannot replace it. " +
                "In an Administrator PowerShell run: " +
                "Unregister-ScheduledTask -TaskName '{1}' -Confirm:`$false ; " +
                "then return to a normal (non-admin) PowerShell and rerun this registration script. " +
                "The recreated task remains Interactive/Limited."
            ) -f $ownerLabel, $TaskName)
    } else {
        Write-Error ("scheduled task registration failed: {0}" -f $message)
    }
    exit 1
}

$null = New-Item -ItemType Directory -Path $ReceiptDir -Force
$registeredAt = (Get-Date).ToString(
    'yyyy-MM-ddTHH:mm:ss.ffffffK',
    [Globalization.CultureInfo]::InvariantCulture
)
$receipt = [ordered]@{
    schema = 'chanlun-qmt-restart-task-registration/v1'
    registered_at = $registeredAt
    task_name = $TaskName
    script_path = [IO.Path]::GetFullPath($ScriptPath)
    action_arguments = $expectedArguments
    catch_up_window_minutes = $CatchUpWindowMinutes
    web_readiness_timeout_seconds = $WebReadinessTimeoutSeconds
    execution_time_limit_minutes = $ExecutionTimeLimitMinutes
    restart_count = $RestartCount
    restart_interval_minutes = $RestartIntervalMinutes
    logon_type = 'Interactive'
    run_level = 'Limited'
    real_account_accessed = $false
    real_order_transport_enabled = $false
    automated_order_authorized = $false
    live_status = 'LIVE_DISABLED'
}
$temporaryReceipt = '{0}.{1}.tmp' -f $ReceiptPath, $PID
try {
    [IO.File]::WriteAllText(
        $temporaryReceipt,
        ($receipt | ConvertTo-Json -Depth 5 -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryReceipt -Destination $ReceiptPath -Force
} finally {
    Remove-Item -LiteralPath $temporaryReceipt -Force -ErrorAction SilentlyContinue
}

Write-Host "Scheduled task registered: $TaskName  (Mon-Fri at $RunAt)"
Write-Host ("Late starts are accepted for {0} minutes after {1}." -f $CatchUpWindowMinutes, $RunAt)
Write-Host ("Cold-start readiness may use {0}s; task limit is {1} minutes with {2} retries every {3} minutes." -f $WebReadinessTimeoutSeconds, $ExecutionTimeLimitMinutes, $RestartCount, $RestartIntervalMinutes)
Write-Host ("Registration receipt: {0}" -f $ReceiptPath)
Write-Host "View / edit it in Task Scheduler:  taskschd.msc"
Write-Host "Test it right now:                 powershell -ExecutionPolicy Bypass -File `"$ScriptPath`" -Force"
Write-Host "Remove it later:                   Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
exit 0
