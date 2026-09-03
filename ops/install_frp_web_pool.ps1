[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [ValidateRange(1, 8)]
    [int]$PoolSize = 3,
    [ValidateRange(2, 60)]
    [int]$PollSeconds = 10
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$watchScript = Join-Path $ProjectRoot 'ops\watch_frp_web_pool.ps1'
if (-not (Test-Path -LiteralPath $watchScript -PathType Leaf)) {
    throw "FRP Web pool supervisor is unavailable: $watchScript"
}

$sha = [Security.Cryptography.SHA256]::Create()
try {
    $scopeBytes = [Text.Encoding]::UTF8.GetBytes(
        $ProjectRoot.TrimEnd('\').ToLowerInvariant()
    )
    $scopeHash = ([BitConverter]::ToString(
        $sha.ComputeHash($scopeBytes)
    ) -replace '-', '').Substring(0, 12).ToLowerInvariant()
} finally {
    $sha.Dispose()
}
$taskName = "ChanlunFrpWebPool-$scopeHash"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = @(
    '-NoProfile',
    '-WindowStyle Hidden',
    '-ExecutionPolicy Bypass',
    ('-File "{0}"' -f $watchScript),
    ('-ProjectRoot "{0}"' -f $ProjectRoot),
    ('-PoolSize {0}' -f $PoolSize),
    ('-PollSeconds {0}' -f $PollSeconds)
) -join ' '
$action = New-ScheduledTaskAction `
    -Execute "$PSHOME\powershell.exe" `
    -Argument $arguments `
    -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

[pscustomobject]@{
    TaskName = $taskName
    User = $identity
    PoolSize = $PoolSize
    State = (Get-ScheduledTask -TaskName $taskName).State
}
