[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 9900,
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$PremarketStart = '08:20'
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$watchdogScript = Join-Path $ProjectRoot 'ops\watch_web.ps1'
if (-not (Test-Path -LiteralPath $watchdogScript -PathType Leaf)) {
    throw "watchdog script is unavailable: $watchdogScript"
}

$identityText = ('{0}|{1}' -f $ProjectRoot.TrimEnd('\').ToUpperInvariant(), $WebPort)
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($identityText))
} finally {
    $sha.Dispose()
}
$token = ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
$taskName = 'ChanlunProWebWatchdog-{0}' -f $token.Substring(0, 12)
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -ProjectRoot "{1}" -WebPort {2}' -f `
    $watchdogScript, $ProjectRoot, $WebPort
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$premarketTime = [datetime]::Today.Add([TimeSpan]::ParseExact(
    $PremarketStart,
    'hh\:mm',
    [Globalization.CultureInfo]::InvariantCulture
))
$premarketTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At $premarketTime
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($logonTrigger, $premarketTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description 'Keep chanlun-pro Web, screening monitor, and manual-trade alerts alive.' `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output (
    'WATCHDOG-INSTALLED task={0} user={1} port={2} premarket={3}' -f `
        $taskName, $currentUser, $WebPort, $PremarketStart
)
