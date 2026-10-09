[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [ValidateRange(1,65535)][int]$WebPort = 9900
)

$ErrorActionPreference = 'Stop'
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
$action = New-ScheduledTaskAction -Execute "$PSHOME\powershell.exe" -Argument $arguments -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$trigger.Delay = 'PT10S'
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Start the configured chanlun-pro market-data client after Windows sign-in.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

# Reuse the existing single-instance Web supervisor and its recovery policy.
& $webInstaller -ProjectRoot $ProjectRoot -WebPort $WebPort
if (-not $?) { throw 'Web startup registration did not complete' }
Write-Output ('APP-AUTOSTART-INSTALLED user={0} qmt_task={1} web_port={2} trigger=sign-in' -f $identity,$taskName,$WebPort)
