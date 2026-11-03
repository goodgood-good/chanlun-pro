[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [ValidateRange(1,65535)][int]$WebPort = 9900,
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')][string]$PremarketStart = '08:20',
    [switch]$QmtOnly
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'no_console.ps1')
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$qmtScript = Join-Path $ProjectRoot 'ops\manage_qmt_runtime.ps1'
$webInstaller = Join-Path $ProjectRoot 'ops\install_web_watchdog.ps1'
foreach ($script in @($qmtScript,$webInstaller)) {
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        throw "startup dependency is unavailable: $script"
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$scope = '{0}|{1}' -f $ProjectRoot.TrimEnd('\').ToUpperInvariant(),$WebPort
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $token = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($scope)))).Replace('-','').ToLowerInvariant().Substring(0,12)
} finally {
    $sha.Dispose()
}
$taskName = 'ChanlunProQmtStartup-{0}' -f $token
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Action Ensure -StartupTimeoutSeconds 120 -WarmupSeconds 0' -f $qmtScript
$action = New-NoConsoleTaskAction -ProjectRoot $ProjectRoot -Label $taskName `
    -FilePath "$PSHOME\powershell.exe" -Argument $arguments -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$trigger.Delay = 'PT10S'
$premarket = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At ([datetime]::Today.Add([TimeSpan]::Parse($PremarketStart)))
# The logon task can succeed and the client can exit days later. Reconcile
# independently of the Web process; IgnoreNew and the runtime mutex bound it.
$recovery = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($trigger,$premarket,$recovery) -Principal $principal `
    -Settings $settings -Description 'Ensure the configured QMT client and market-data catalog after sign-in, before trading and every minute.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

# Reuse the existing single-instance Web supervisor and its recovery policy.
if (-not $QmtOnly) {
    & $webInstaller -ProjectRoot $ProjectRoot -WebPort $WebPort -PremarketStart $PremarketStart
    if (-not $?) { throw 'Web startup registration did not complete' }
}
Write-Output ('APP-AUTOSTART-INSTALLED user={0} qmt_task={1} web_port={2} premarket={3} recovery=1m' -f $identity,$taskName,$WebPort,$PremarketStart)
