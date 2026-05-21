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

$TaskName   = 'Chanlun-QMT-DailyRestart'
$ScriptPath = Join-Path $PSScriptRoot 'restart_qmt_daily.ps1'
$RunAt      = '08:30'

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    Write-Error "restart script not found: $ScriptPath"
    exit 1
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ScriptPath)

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $RunAt

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

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
Register-ScheduledTask @regArgs | Out-Null

Write-Host "Scheduled task registered: $TaskName  (Mon-Fri at $RunAt)"
Write-Host "View / edit it in Task Scheduler:  taskschd.msc"
Write-Host "Test it right now:                 Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove it later:                   Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
