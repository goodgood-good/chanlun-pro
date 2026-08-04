#Requires -RunAsAdministrator
# Register read-only QMT sector capture and fail-closed human review screening.
# Neither task starts QMT nor imports a trading account/order API.
[CmdletBinding()]
param(
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot 'run_v3_forward_paper_daily.ps1'
$ReceiptDir = Join-Path $ProjectRoot '.cache\chanlun_v3_scheduler'
$ReceiptPath = Join-Path $ReceiptDir 'forward_task_registration.json'
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "forward-paper runner not found: $Runner"
}

function Resolve-ForwardPython {
    param([string]$RequestedPath)

    $candidates = [Collections.Generic.List[string]]::new()
    foreach ($value in @(
        $RequestedPath,
        $env:CHANLUN_FORWARD_PYTHON,
        $env:CHANLUN_PYTHON,
        (Join-Path $ProjectRoot '.venv\Scripts\python.exe')
    )) {
        if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
            $candidates.Add([string]$value)
        }
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace($command.Source)) {
        $candidates.Add([string]$command.Source)
    }
    foreach ($candidate in $candidates) {
        try {
            $resolved = [IO.Path]::GetFullPath($candidate)
        } catch {
            continue
        }
        if (Test-Path -LiteralPath $resolved -PathType Leaf) {
            return $resolved
        }
    }
    throw 'no stable forward-paper Python executable was found'
}

$PinnedPython = Resolve-ForwardPython -RequestedPath $PythonPath

# Capture/Evaluate are deliberately non-interactive: they use local files and
# loopback readiness only, never a GUI, account, order transport, remote share
# or encrypted credential.  S4U therefore lets the frozen forward observation
# continue while the desktop session is disconnected, without storing a
# password.  Keep the separate QMT restart task interactive because it owns the
# terminal UI; this principal is only for the two read-only forward tasks.
$principal = New-ScheduledTaskPrincipal `
    -UserId ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType S4U -RunLevel Limited

function Register-ForwardTask {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Phase,
        [Parameter(Mandatory = $true)][string]$At,
        [Parameter(Mandatory = $true)][int]$ExecutionMinutes,
        [Parameter(Mandatory = $true)][int]$RestartCount,
        [Parameter(Mandatory = $true)][int]$RestartMinutes,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $expectedArguments = (
        '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Phase {1} -PythonExe "{2}"' -f (
            $Runner,
            $Phase,
            $PinnedPython
        )
    )
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $expectedArguments
    $trigger = New-ScheduledTaskTrigger -Weekly `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew `
        -RestartCount $RestartCount `
        -RestartInterval (New-TimeSpan -Minutes $RestartMinutes) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionMinutes)
    $task = Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force
    if ($null -eq $task) { throw "Register-ScheduledTask returned no object for $Name" }
    $confirmed = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
    if ($confirmed.State -eq 'Disabled') { throw "scheduled task is disabled: $Name" }
    if ($confirmed.Actions.Count -ne 1) {
        throw "scheduled task action count was not retained: $Name"
    }
    if (
        [string]$confirmed.Actions[0].Execute -ne 'powershell.exe' -or
        [string]$confirmed.Actions[0].Arguments -ne $expectedArguments -or
        -not [string]::IsNullOrWhiteSpace(
            [string]$confirmed.Actions[0].WorkingDirectory
        )
    ) {
        throw "scheduled task action was not retained: $Name"
    }
    if ([string]$confirmed.Settings.MultipleInstances -ne 'IgnoreNew') {
        throw "scheduled task single-instance contract was not retained: $Name"
    }
    if (
        [string]$confirmed.Principal.LogonType -ne 'S4U' -or
        [string]$confirmed.Principal.RunLevel -ne 'Limited' -or
        [string]::IsNullOrWhiteSpace([string]$confirmed.Principal.UserId)
    ) {
        throw "scheduled task disconnected-session principal was not retained: $Name"
    }
    if ($confirmed.Settings.StartWhenAvailable -ne $true) {
        throw "scheduled task delayed-start contract was not retained: $Name"
    }
    if ([int]$confirmed.Settings.RestartCount -ne $RestartCount) {
        throw "scheduled task restart count was not retained: $Name"
    }
    if ($null -eq $confirmed.Settings.RestartInterval -or (
        [Xml.XmlConvert]::ToTimeSpan(
            [string]$confirmed.Settings.RestartInterval
        ) -ne (New-TimeSpan -Minutes $RestartMinutes)
    )) {
        throw "scheduled task restart interval was not retained: $Name"
    }
    if ($null -eq $confirmed.Settings.ExecutionTimeLimit -or (
        [Xml.XmlConvert]::ToTimeSpan(
            [string]$confirmed.Settings.ExecutionTimeLimit
        ) -ne (New-TimeSpan -Minutes $ExecutionMinutes)
    )) {
        throw "scheduled task execution limit was not retained: $Name"
    }
    $confirmedTriggers = @($confirmed.Triggers)
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
                $startBoundary.TimeOfDay -eq [TimeSpan]::Parse($At)
            )
        } catch {
            $triggerRetained = $false
        }
    }
    if (-not $triggerRetained) {
        throw "scheduled task weekly trigger was not retained: $Name"
    }
    return $confirmed
}

$capture = Register-ForwardTask `
    -Name 'Chanlun-V3-Forward-Capture' `
    -Phase 'Capture' `
    -At '09:10' `
    -ExecutionMinutes 180 `
    -RestartCount 3 `
    -RestartMinutes 5 `
    -Description 'chanlun-pro: read-only daily QMT GICS3 membership snapshot for current-QMT human review screening; LIVE_DISABLED.'
$evaluate = Register-ForwardTask `
    -Name 'Chanlun-V3-Forward-Evaluate' `
    -Phase 'Evaluate' `
    -At '15:20' `
    -ExecutionMinutes 720 `
    -RestartCount 3 `
    -RestartMinutes 5 `
    -Description 'chanlun-pro: fail-closed archive of the staged live human-review screen; zero orders and no account transport.'

$null = New-Item -ItemType Directory -Path $ReceiptDir -Force
$registeredAt = (Get-Date).ToString(
    'yyyy-MM-ddTHH:mm:ss.ffffffK',
    [Globalization.CultureInfo]::InvariantCulture
)
$receipt = [ordered]@{
    schema = 'chanlun-v3-forward-task-registration/v1'
    registered_at = $registeredAt
    runner_path = [IO.Path]::GetFullPath($Runner)
    python_executable = $PinnedPython
    tasks = @(
        [ordered]@{
            name = 'Chanlun-V3-Forward-Capture'
            phase = 'CAPTURE'
            action_arguments = (
                '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Phase Capture -PythonExe "{1}"' -f $Runner, $PinnedPython
            )
        },
        [ordered]@{
            name = 'Chanlun-V3-Forward-Evaluate'
            phase = 'EVALUATE'
            action_arguments = (
                '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Phase Evaluate -PythonExe "{1}"' -f $Runner, $PinnedPython
            )
        }
    )
    logon_type = 'S4U'
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
        ($receipt | ConvertTo-Json -Depth 6 -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryReceipt -Destination $ReceiptPath -Force
} finally {
    Remove-Item -LiteralPath $temporaryReceipt -Force -ErrorAction SilentlyContinue
}

Write-Output ('Registered {0}: Mon-Fri 09:10' -f $capture.TaskName)
Write-Output ('Registered {0}: Mon-Fri 15:20' -f $evaluate.TaskName)
Write-Output ('Pinned forward Python: {0}' -f $PinnedPython)
Write-Output ('Registration receipt: {0}' -f $ReceiptPath)
Write-Output 'Interrupted forward tasks retry at five-minute intervals, at most three times; manifests remain atomic and idempotent.'
Write-Output 'Both tasks remain REVIEW_REQUIRED/LIVE_DISABLED and never access an account or order transport.'
