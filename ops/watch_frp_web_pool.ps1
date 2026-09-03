[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [ValidateRange(1, 8)]
    [int]$PoolSize = 3,
    [ValidateRange(2, 60)]
    [int]$PollSeconds = 10,
    [string]$FrpRoot = 'C:\frp_0.64.0_windows_amd64'
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$FrpRoot = [IO.Path]::GetFullPath($FrpRoot)
$frpcPath = Join-Path $FrpRoot 'frpc.exe'
$mainConfigPath = Join-Path $FrpRoot 'frpc.toml'
$poolConfigPath = Join-Path $ProjectRoot 'ops\frpc_web_pool.toml'
$logRoot = Join-Path $ProjectRoot 'ops\logs\frpc_web_pool'

foreach ($path in @($frpcPath, $mainConfigPath, $poolConfigPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "required FRP pool file is unavailable: $path"
    }
}
if (-not (Test-Path -LiteralPath $logRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
}

function Get-RequiredConfigValue {
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $match = [regex]::Match($Content, $Pattern)
    if (-not $match.Success -or [string]::IsNullOrWhiteSpace($match.Groups[1].Value)) {
        throw "FRP main config is missing $Name"
    }
    return $match.Groups[1].Value
}

function Get-FrpConnectionSettings {
    $content = Get-Content -LiteralPath $mainConfigPath -Raw -Encoding UTF8
    return [pscustomobject]@{
        ServerAddress = Get-RequiredConfigValue `
            -Content $content `
            -Pattern '(?m)^\s*serverAddr\s*=\s*"([^"]+)"' `
            -Name 'serverAddr'
        ServerPort = Get-RequiredConfigValue `
            -Content $content `
            -Pattern '(?m)^\s*serverPort\s*=\s*(\d+)' `
            -Name 'serverPort'
        Token = Get-RequiredConfigValue `
            -Content $content `
            -Pattern '(?m)^\s*auth\.token\s*=\s*"([^"]+)"' `
            -Name 'auth.token'
    }
}

function Stop-OrphanedPoolClients {
    $configToken = [IO.Path]::GetFullPath($poolConfigPath)
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'frpc.exe' `
            -and $_.CommandLine `
            -and $_.CommandLine.IndexOf(
                $configToken,
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Start-PoolClient {
    param(
        [Parameter(Mandatory = $true)][int]$Slot,
        [Parameter(Mandatory = $true)]$Settings
    )
    $variables = [ordered]@{
        CHANLUN_FRP_POOL_SERVER_ADDR = $Settings.ServerAddress
        CHANLUN_FRP_POOL_SERVER_PORT = [string]$Settings.ServerPort
        CHANLUN_FRP_POOL_TOKEN = $Settings.Token
        CHANLUN_FRP_POOL_PROXY_NAME = "chanlun-tcp-pool-$Slot"
        CHANLUN_FRP_POOL_LOG_FILE = (
            (Join-Path $logRoot "frpc_pool_$Slot.log") -replace '\\', '/'
        )
    }
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $frpcPath
    $startInfo.Arguments = '-c "{0}"' -f $poolConfigPath
    $startInfo.WorkingDirectory = $ProjectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    foreach ($entry in $variables.GetEnumerator()) {
        $startInfo.EnvironmentVariables[$entry.Key] = [string]$entry.Value
    }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "unable to start FRP pool slot $Slot"
    }
    return $process
}

$mutex = New-Object Threading.Mutex($false, 'Local\ChanlunProFrpWebPool')
$ownsMutex = $false
$children = @{}
try {
    $ownsMutex = $mutex.WaitOne(0)
    if (-not $ownsMutex) {
        throw 'another FRP Web pool supervisor is already running'
    }
    Stop-OrphanedPoolClients
    while ($true) {
        $settings = Get-FrpConnectionSettings
        foreach ($slot in 1..$PoolSize) {
            $child = $children[$slot]
            if ($null -eq $child -or $child.HasExited) {
                $children[$slot] = Start-PoolClient -Slot $slot -Settings $settings
            }
        }
        Start-Sleep -Seconds $PollSeconds
    }
} finally {
    foreach ($child in $children.Values) {
        if ($null -ne $child -and -not $child.HasExited) {
            Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if ($ownsMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
