# ============================================================================
# restart_web.ps1
#
# 重启 chanlun-pro 网页应用。应用进程通过 app_qmt_runtime.py 管理 QMT 生命周期；
# 部署脚本本身不修改 QMT。
# ============================================================================

[CmdletBinding()]
param(
    [switch]$PreflightOnly,
    [switch]$SkipWatchdog,
    [switch]$OpenBrowser,
    [switch]$EnableLargeScreeningScope,
    [switch]$EnableLargeHoldingMonitorScope,
    [switch]$EnableFullSymbolCatalog,
    [switch]$EnableFullCoverage,
    [switch]$ForceFullCoverageUntilComplete,
    [ValidateRange(30, 1800)]
    [int]$WebReadinessTimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy_common.ps1')

if ($EnableFullCoverage -and -not $EnableLargeScreeningScope) {
    throw '-EnableFullCoverage requires -EnableLargeScreeningScope'
}
if ($ForceFullCoverageUntilComplete -and -not $EnableFullCoverage) {
    throw '-ForceFullCoverageUntilComplete requires -EnableFullCoverage'
}

# -------------------------------- 配置 ---------------------------------------
$ProjectRoot  = Split-Path -Parent $PSScriptRoot
$AppDir       = Join-Path $ProjectRoot 'web\chanlun_chart'
$SrcPath      = Join-Path $ProjectRoot 'src'
$AppScript    = Join-Path $AppDir 'app.py'
$verifyScript = Join-Path $ProjectRoot 'ops\verify_deploy.ps1'
$watchdogScript = Join-Path $ProjectRoot 'ops\watch_web.ps1'
$watchdogInstaller = Join-Path $ProjectRoot 'ops\install_web_watchdog.ps1'
$watchdogStateRoot = Join-Path $ProjectRoot '.cache\chanlun_web_watchdog'
$watchdogScopePath = Join-Path $watchdogStateRoot 'deployment_scope.json'
$PreflightTimeoutSec = 30
$LargeScopePriorityMaxSymbols = 384
$LargeScopeMonitorUniverseSymbols = 384
$LargeScopeCandidateFiveMinuteSymbols = 128
$FullCoverageBatchSymbols = 240
$LogDir       = Join-Path $PSScriptRoot 'logs'
# ----------------------------------------------------------------------------

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$LogFile = Join-Path $LogDir ('restart_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))

function Log([string]$msg) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line
}

function Write-WatchdogDeploymentScope {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$Port,
        [bool]$LargeScreeningScopeEnabled,
        [bool]$LargeHoldingMonitorScopeEnabled,
        [bool]$FullSymbolCatalogEnabled,
        [bool]$FullCoverageEnabled,
        [bool]$ForcedFullCoverageEnabled
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $payload = [ordered]@{
        schema = 'chanlun-web-watchdog-deployment-scope-v1'
        project_root = $ProjectRoot
        web_port = $Port
        updated_at = (Get-Date).ToString('o')
        enable_large_screening_scope = $LargeScreeningScopeEnabled
        enable_large_holding_monitor_scope = $LargeHoldingMonitorScopeEnabled
        enable_full_symbol_catalog = $FullSymbolCatalogEnabled
        enable_full_coverage = $FullCoverageEnabled
        force_full_coverage_until_complete = $ForcedFullCoverageEnabled
    }
    $temporary = '{0}.{1}.tmp' -f $Path, $PID
    try {
        [IO.File]::WriteAllText(
            $temporary,
            (($payload | ConvertTo-Json -Depth 3 -Compress) + [Environment]::NewLine),
            (New-Object Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    Log (
        'persisted watchdog deployment scope: large={0}; holding={1}; catalog={2}; coverage={3}; force={4}' -f `
            $LargeScreeningScopeEnabled,
            $LargeHoldingMonitorScopeEnabled,
            $FullSymbolCatalogEnabled,
            $FullCoverageEnabled,
            $ForcedFullCoverageEnabled
    )
}

function Open-WebApplication([string]$Uri) {
    try {
        Start-Process -FilePath $Uri | Out-Null
        Log ('browser launch requested: {0}' -f $Uri)
    } catch {
        Log ('WARNING: unable to open browser automatically: {0}' -f $_.Exception.Message)
    }
}

function Get-DeploymentMutexName {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$Port
    )

    # 互斥量只绑定当前检出目录与配置端口。其他检出目录或明确分离的端口可以独立部署；
    # 可能停止或启动同一服务的两个调用必须串行执行。
    $normalizedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\').ToUpperInvariant()
    $identity = '{0}|{1}' -f $normalizedRoot, $Port
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity))
    } finally {
        $sha.Dispose()
    }
    $token = ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
    return 'Local\ChanlunProDeploy_{0}' -f $token.Substring(0, 32)
}

function Enter-DeploymentMutex {
    param([Parameter(Mandatory = $true)][string]$Name)

    $mutex = New-Object Threading.Mutex($false, $Name)
    try {
        try {
            # 立即失败：第二个部署进程不能静默等待后，再修改先前已完成归属校验的服务。
            $acquired = $mutex.WaitOne(0)
        } catch [Threading.AbandonedMutexException] {
            # 前一个进程未释放互斥量便退出时，Windows 会把所有权转交当前线程；
            # 因此该状态可以恢复，且仍能保证独占。
            $acquired = $true
        }
        if (-not $acquired) {
            $mutex.Dispose()
            return $null
        }
        return $mutex
    } catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-DeploymentMutex {
    param([Parameter(Mandatory = $true)][Threading.Mutex]$Mutex)

    try {
        $Mutex.ReleaseMutex()
    } finally {
        $Mutex.Dispose()
    }
}

function Test-CurrentProcessElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Register-LimitedWebLaunchTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [bool]$LargeScopeEnabled = $false,
        [bool]$LargeHoldingMonitorScopeEnabled = $false,
        [bool]$FullSymbolCatalogEnabled = $false,
        [bool]$FullCoverageEnabled = $false,
        [bool]$ForcedFullCoverageEnabled = $false
    )

    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ([string]::IsNullOrWhiteSpace($currentUser)) {
        throw 'current interactive user identity is unavailable'
    }
    $argumentParts = @(
        '-NoProfile',
        '-WindowStyle Hidden',
        '-ExecutionPolicy Bypass',
        ('-File "{0}"' -f $ScriptPath),
        '-SkipWatchdog',
        ('-WebReadinessTimeoutSeconds {0}' -f $TimeoutSeconds)
    )
    if ($LargeScopeEnabled) { $argumentParts += '-EnableLargeScreeningScope' }
    if ($LargeHoldingMonitorScopeEnabled) {
        $argumentParts += '-EnableLargeHoldingMonitorScope'
    }
    if ($FullSymbolCatalogEnabled) {
        $argumentParts += '-EnableFullSymbolCatalog'
    }
    if ($FullCoverageEnabled) { $argumentParts += '-EnableFullCoverage' }
    if ($ForcedFullCoverageEnabled) {
        $argumentParts += '-ForceFullCoverageUntilComplete'
    }
    $arguments = $argumentParts -join ' '
    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument $arguments
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Seconds ($TimeoutSeconds + 300))
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Principal $principal `
        -Settings $settings `
        -Description 'One-shot limited-token chanlun-pro Web deployment handoff.' `
        -Force | Out-Null
}

function Remove-LimitedWebLaunchTask {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask `
        -TaskName $TaskName `
        -Confirm:$false `
        -ErrorAction SilentlyContinue
}

function Import-ProjectDotEnv {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$OverrideNames = @()
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    try {
        $bytes = [IO.File]::ReadAllBytes($Path)
        $strictUtf8 = New-Object Text.UTF8Encoding($true, $true)
        $content = $strictUtf8.GetString($bytes)
        if ($content.Length -gt 0 -and $content[0] -eq [char]0xfeff) {
            $content = $content.Substring(1)
        }
    } catch {
        throw ".env 必须是有效的 UTF-8 文件：$($_.Exception.Message)"
    }
    foreach ($line in ($content -split "`r?`n")) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or $trimmed -notmatch '=') {
            continue
        }
        $separatorIndex = $trimmed.IndexOf('=')
        $parts = @($trimmed.Substring(0, $separatorIndex), $trimmed.Substring($separatorIndex + 1))
        $key = $parts[0].Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $currentValue = [Environment]::GetEnvironmentVariable($key, 'Process')
        if ($null -ne $currentValue -and $OverrideNames -notcontains $key) {
            continue
        }
        $value = $parts[1].Trim()
        if (
            $value.Length -ge 2 -and
            (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or
             ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
}

function Import-UserEnvironmentFallback {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    foreach ($name in $Names) {
        $processValue = [Environment]::GetEnvironmentVariable($name, 'Process')
        if (-not [string]::IsNullOrWhiteSpace($processValue)) { continue }

        # 已运行的终端不会自动看到后来写入 Windows 用户环境的变量。项目文件未配置
        # 该项时，从用户环境补齐到本次部署进程，子进程便能继承完整的提供方凭据。
        $userValue = [Environment]::GetEnvironmentVariable($name, 'User')
        if ([string]::IsNullOrWhiteSpace($userValue)) { continue }
        [Environment]::SetEnvironmentVariable($name, $userValue, 'Process')
    }
}

function Resolve-ProjectPython {
    $configured = [Environment]::GetEnvironmentVariable('CHANLUN_PYTHON', 'Process')
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) {
            throw "CHANLUN_PYTHON does not exist: $configured"
        }
        return (Resolve-Path -LiteralPath $configured).Path
    }

    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $venvPython).Path
    }

    $poetry = Get-Command poetry -ErrorAction SilentlyContinue
    if ($null -eq $poetry) {
        throw 'no project Python found; set CHANLUN_PYTHON or install the Poetry environment'
    }
    Push-Location $ProjectRoot
    # Poetry 可能把跳过虚拟环境等提示写入标准错误，但仍以退出码 0 在标准输出返回
    # 可用的 Python 路径。全局 ErrorActionPreference=Stop 会让 Windows PowerShell
    # 在检查 LASTEXITCODE 前把这类提示提升为终止错误，所以只在这个只读解析调用中
    # 临时放宽错误偏好；下方仍以退出码与实际可执行文件为最终依据。
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $poetry.Source run python -I -c 'import sys; print(sys.executable)' 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw ('Poetry could not resolve project Python: {0}' -f ($output -join ' '))
    }
    foreach ($line in $output) {
        # 原生标准错误在 2>&1 后仍可能是 ErrorRecord，不能把含 ANSI 装饰的显示文本
        # 交给 Test-Path。
        if ($line -isnot [string]) { continue }
        $candidate = ([string]$line).Trim()
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Poetry returned no usable Python executable'
}

function Test-LoginUsersConfig {
    param([AllowEmptyString()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    try {
        $accounts = $Value | ConvertFrom-Json
    } catch {
        return $false
    }
    if ($accounts -isnot [pscustomobject]) { return $false }
    $properties = @($accounts.PSObject.Properties)
    if ($properties.Count -eq 0) { return $false }
    foreach ($property in $properties) {
        $username = [string]$property.Name
        $passwordHash = [string]$property.Value
        if (
            [string]::IsNullOrWhiteSpace($username) -or
            $username.Trim().Length -gt 64 -or
            [string]::IsNullOrWhiteSpace($passwordHash) -or
            -not (
                $passwordHash.StartsWith('pbkdf2:') -or
                $passwordHash.StartsWith('scrypt:')
            )
        ) {
            return $false
        }
    }
    return $true
}

function Get-WebProcs {
    $appPattern = '(?i)(?:^|[\s"])' + [regex]::Escape($AppScript) + '(?:[\s"]|$)'
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -match $appPattern
        })
}

function Get-AttestedWebProcs {
    param(
        [Parameter(Mandatory = $true)][int[]]$PortOwnerIds,
        [Parameter(Mandatory = $true)][string]$HealthUri,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceRevision
    )

    if ($PortOwnerIds.Count -eq 0) { return @() }
    try {
        $health = Invoke-RestMethod -Uri $HealthUri -Method Get -TimeoutSec 3
    } catch {
        return @()
    }

    $reportedPid = 0
    if (
        [string]$health.status -ne 'ready' -or
        -not [int]::TryParse([string]$health.pid, [ref]$reportedPid) -or
        $PortOwnerIds -notcontains $reportedPid
    ) {
        return @()
    }

    # 计划任务或提权会话中的同用户进程可能拒绝向当前 WMI 会话暴露命令行。
    # 只有端口 PID、当前 Git 提交前缀和应用专属就绪组件同时吻合时才接纳，不能
    # 单凭一个可伪造的 HTTP 响应把未知端口所有者纳入停止范围。
    $treeMarker = '.tree.'
    $treeMarkerIndex = $ExpectedSourceRevision.IndexOf(
        $treeMarker,
        [StringComparison]::OrdinalIgnoreCase
    )
    if ($treeMarkerIndex -le 0) { return @() }
    $expectedCommitPrefix = $ExpectedSourceRevision.Substring(
        0,
        $treeMarkerIndex + $treeMarker.Length
    )
    $reportedRevision = [string]$health.revision
    if (
        [string]::IsNullOrWhiteSpace($reportedRevision) -or
        -not $reportedRevision.StartsWith(
            $expectedCommitPrefix,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return @()
    }

    if ($null -eq $health.components) { return @() }
    $componentNames = @($health.components.PSObject.Properties.Name)
    foreach ($requiredComponent in @('scheduler', 'qmt_runtime', 'trading_screening')) {
        if ($componentNames -notcontains $requiredComponent) { return @() }
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$reportedPid" -ErrorAction SilentlyContinue
    if ($null -eq $process -or [string]$process.Name -ne 'python.exe') { return @() }
    return $process
}

function Get-ListeningProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { [int]$_.LocalPort -eq $Port } |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Start-WebProcess {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    $stamp = '{0}_{1}' -f (Get-Date -Format 'yyyy-MM-dd_HH-mm-ss-fff'), ([guid]::NewGuid().ToString('N').Substring(0, 8))
    $outLog = Join-Path $LogDir ("web_${Purpose}_stdout_${stamp}.log")
    $errLog = Join-Path $LogDir ("web_${Purpose}_stderr_${stamp}.log")
    $script:LastWebErrorLog = $errLog
    Log ('start web project ({0}): {1} {2} nobrowser' -f $Purpose, $PythonPath, $AppScript)
    $spArgs = @{
        FilePath               = $PythonPath
        ArgumentList           = @($AppScript, 'nobrowser')
        WorkingDirectory       = $AppDir
        RedirectStandardOutput = $outLog
        RedirectStandardError  = $errLog
        WindowStyle            = 'Hidden'
        PassThru               = $true
        ErrorAction            = 'Stop'
    }
    Start-Process @spArgs
}

function Test-WebLiveness {
    param(
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Process,
        [int]$TimeoutSeconds = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 1
        $Process.Refresh()
        if ($Process.HasExited) { return $false }
        $owners = @(Get-ListeningProcessIds -Port $webPort)
        if ($owners -notcontains $Process.Id) { continue }
        try {
            $live = Invoke-RestMethod -Uri $liveUri -Method Get -TimeoutSec 2
            if ($live.status -eq 'alive' -and $live.revision -eq $deploymentRevision) {
                return $true
            }
        } catch {
            # 启动可能仍在进行。
        }
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Restore-WebService {
    param([Parameter(Mandatory = $true)][string]$Reason)

    if (-not $script:PreviousWebWasRunning) {
        Log ('recovery skipped because no previous web service existed ({0})' -f $Reason)
        return $false
    }
    if (@(Get-ListeningProcessIds -Port $webPort).Count -gt 0) {
        Log ('recovery skipped because port {0} is already owned ({1})' -f $webPort, $Reason)
        return $false
    }
    $restorePython = $script:RollbackPythonExe
    if ([string]::IsNullOrWhiteSpace($restorePython) -or -not (Test-Path -LiteralPath $restorePython -PathType Leaf)) {
        $restorePython = $PythonExe
    }
    try {
        $restored = Start-WebProcess -PythonPath $restorePython -Purpose 'recovery'
    } catch {
        Log ('ERROR: web recovery start failed: {0}' -f $_.Exception.Message)
        return $false
    }
    if (Test-WebLiveness -Process $restored -TimeoutSeconds 30) {
        Log ('web recovery is alive PID={0}; reason={1}' -f $restored.Id, $Reason)
        return $true
    }
    $restored.Refresh()
    if (-not $restored.HasExited -and @(Get-ListeningProcessIds -Port $webPort) -contains $restored.Id) {
        Log ('web recovery owns the port but is not yet live; preserving PID={0} for diagnostics' -f $restored.Id)
        return $true
    }
    if (-not $restored.HasExited) {
        Stop-Process -Id $restored.Id -Force -ErrorAction SilentlyContinue
    }
    Log ('ERROR: web recovery failed; check {0}' -f $script:LastWebErrorLog)
    return $false
}

function Abort-AfterWebStop {
    param([Parameter(Mandatory = $true)][string]$Reason)
    Log ('ERROR: {0}' -f $Reason)
    $null = Restore-WebService -Reason $Reason
    Log '===== web restart ABORTED ====='
    exit 1
}

Log '===== web restart START ====='

# 在停止任何进程前校验全部网页服务前置条件。
foreach ($requiredDir in @($ProjectRoot, $AppDir, $SrcPath)) {
    if (-not (Test-Path -LiteralPath $requiredDir -PathType Container)) {
        Log ('ERROR: required directory not found: {0}' -f $requiredDir)
        Log '===== web restart ABORTED ====='
        exit 1
    }
}
foreach ($requiredFile in @($AppScript, $verifyScript)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        Log ('ERROR: required file not found: {0}' -f $requiredFile)
        Log '===== web restart ABORTED ====='
        exit 1
    }
}
if (-not (Test-Path -LiteralPath $watchdogScript -PathType Leaf)) {
    Log ('ERROR: watchdog script not found: {0}' -f $watchdogScript)
    Log '===== web restart ABORTED ====='
    exit 1
}
if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
    Log 'ERROR: git is required for source attestation'
    Log '===== web restart ABORTED ====='
    exit 1
}
if ($null -eq (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    Log 'ERROR: Get-NetTCPConnection is required for port ownership verification'
    Log '===== web restart ABORTED ====='
    exit 1
}

try {
    $deploymentManagedNames = @(
        'CHANLUN_LOGIN_USERS',
        'LONGBRIDGE_APP_KEY',
        'LONGBRIDGE_APP_SECRET',
        'LONGBRIDGE_ACCESS_TOKEN'
    )
    Import-ProjectDotEnv -Path (Join-Path $ProjectRoot '.env') `
        -OverrideNames $deploymentManagedNames
    # Identity-catalog enumeration is independent from strategy/full-coverage
    # authorization.  A normal restart always restores the explicit 12-symbol
    # cohort after .env import so stale process/user values cannot enumerate a
    # whole exchange during cold start, periodic refresh, or search fallback.
    [Environment]::SetEnvironmentVariable(
        'CHANLUN_SYMBOL_CATALOG_VALIDATION_CODES',
        'SZ.000932,SZ.000923,SH.600516,SZ.001203,SZ.000783,SZ.000987,SH.601377,SH.601628,SZ.002377,SH.601808,SZ.000698,SH.600583',
        'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'CHANLUN_SYMBOL_CATALOG_FULL_REFRESH_AUTHORIZED',
        $(if ($EnableFullSymbolCatalog) { '1' } else { '0' }),
        'Process'
    )
    # A normal restart is always a validation restart.  Stale process, user or
    # .env values cannot reactivate broad/full processing; operators must use
    # the independent explicit switches above for this invocation.
    if (-not $EnableLargeScreeningScope) {
        $boundedScreeningNumericNames = @(
            'CHANLUN_TRADING_SCREENING_VALIDATION_COHORT_SIZE',
            'CHANLUN_TRADING_SCREENING_CANDIDATE_5M_MAX_SYMBOLS',
            'CHANLUN_TRADING_SCREENING_CANDIDATE_30M_MAX_SYMBOLS',
            'CHANLUN_TRADING_SCREENING_SUPPORTIVE_DISCOVERY_MAX_SECTOR_RANK',
            'CHANLUN_TRADING_SCREENING_SYMBOLS_PER_REFRESH',
            'CHANLUN_TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH',
            'CHANLUN_TRADING_SCREENING_PRIORITY_MAX_SYMBOLS'
        )
        foreach ($name in $boundedScreeningNumericNames) {
            [Environment]::SetEnvironmentVariable($name, '12', 'Process')
        }
        [Environment]::SetEnvironmentVariable(
            'CHANLUN_TRADING_SCREENING_MAX_ADMITTED_UNIVERSE_SYMBOLS',
            '20',
            'Process'
        )
    } else {
        # Full-market discovery remains cadence-bounded, while every currently
        # confirmed 5m setup must fit the one-minute locator admission wave.
        # Twelve affinity shards retain 48 hot 1m runtimes each. A 384-symbol
        # admission ceiling covers the exact locator pool plus mandatory
        # watch/holding symbols without binding it to the ordinary 240-symbol
        # five-minute rotation; the 128-symbol 5m ceiling also fits the current
        # first-center forming set at a new bar boundary. The absolute
        # 58-second budget still fails closed on a real throughput shortfall.
        [Environment]::SetEnvironmentVariable(
            'CHANLUN_TRADING_SCREENING_PRIORITY_MAX_SYMBOLS',
            [string]$LargeScopePriorityMaxSymbols,
            'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'CHANLUN_TRADING_SCREENING_MAX_ADMITTED_UNIVERSE_SYMBOLS',
            [string]$LargeScopeMonitorUniverseSymbols,
            'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'CHANLUN_TRADING_SCREENING_CANDIDATE_5M_MAX_SYMBOLS',
            [string]$LargeScopeCandidateFiveMinuteSymbols,
            'Process'
        )
        if ($EnableFullCoverage) {
            # Full-market rebuilds use a separate, deeper work queue so all
            # structure processes stay occupied between durable checkpoints.
            # This does not enlarge the latency-sensitive 5m candidate lane.
            [Environment]::SetEnvironmentVariable(
                'CHANLUN_TRADING_SCREENING_SYMBOLS_PER_REFRESH',
                [string]$FullCoverageBatchSymbols,
                'Process'
            )
            [Environment]::SetEnvironmentVariable(
                'CHANLUN_TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH',
                [string]$FullCoverageBatchSymbols,
                'Process'
            )
        }
    }
    [Environment]::SetEnvironmentVariable(
        'CHANLUN_TRADING_SCREENING_ALLOW_LARGE_SCOPE',
        $(if ($EnableLargeScreeningScope) { '1' } else { '0' }),
        'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'CHANLUN_TRADING_SCREENING_FULL_COVERAGE_ENABLED',
        $(if ($EnableFullCoverage) { '1' } else { '0' }),
        'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'CHANLUN_TRADING_SCREENING_FORCE_FULL_COVERAGE_UNTIL_COMPLETE',
        $(if ($ForceFullCoverageUntilComplete) { '1' } else { '0' }),
        'Process'
    )
    if (-not $EnableLargeHoldingMonitorScope) {
        [Environment]::SetEnvironmentVariable(
            'CHANLUN_HOLDING_GROUP_MONITOR_MAX_SYMBOLS',
            '12',
            'Process'
        )
    }
    [Environment]::SetEnvironmentVariable(
        'CHANLUN_HOLDING_GROUP_MONITOR_LARGE_SCOPE_AUTHORIZED',
        $(if ($EnableLargeHoldingMonitorScope) { '1' } else { '0' }),
        'Process'
    )
    Import-UserEnvironmentFallback -Names @(
        'LONGBRIDGE_APP_KEY',
        'LONGBRIDGE_APP_SECRET',
        'LONGBRIDGE_ACCESS_TOKEN'
    )
    $PythonExe = Resolve-ProjectPython
} catch {
    Log ('ERROR: Python/environment preflight failed: {0}' -f $_.Exception.Message)
    Log '===== web restart ABORTED ====='
    exit 1
}
if (-not (Test-LoginUsersConfig -Value $env:CHANLUN_LOGIN_USERS)) {
    Log 'ERROR: CHANLUN_LOGIN_USERS 必须是用户名到 pbkdf2:/scrypt: 哈希的 JSON 对象；现有服务未停止'
    Log '===== web restart ABORTED ====='
    exit 1
}
Log ('project Python = {0}' -f $PythonExe)

$largeScopeRequested = (
    $EnableLargeScreeningScope -or
    $EnableLargeHoldingMonitorScope -or
    $EnableFullSymbolCatalog -or
    $EnableFullCoverage -or
    $ForceFullCoverageUntilComplete
)
if ($largeScopeRequested) {
    $validationDirectory = Join-Path `
        $ProjectRoot `
        'audit\chanlun_trading_system_backtest\research_sample_validation_12'
    # Windows PowerShell promotes native stderr to ``NativeCommandError`` when
    # the script-wide ErrorActionPreference is Stop.  A rejected gate is an
    # expected, controlled preflight result: capture its complete traceback and
    # exit code so the operator sees the actual rejection while the old service
    # remains untouched.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $validationOutput = @(& $PythonExe `
            (Join-Path $ProjectRoot 'tools\verify_qmt_validation_gate.py') `
            '--directory' $validationDirectory `
            '--expected-symbol-count' '12' 2>&1)
        $validationExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($validationExitCode -ne 0) {
        $validationDetail = ($validationOutput | ForEach-Object {
            ([string]$_).Trim()
        } | Where-Object { $_ }) -join ' '
        Log (
            'ERROR: large-scope validation12 gate rejected startup before service stop: {0}' -f `
                $validationDetail
        )
        Log '===== web restart ABORTED ====='
        exit 1
    }
    Log 'current validation12 gate verified before large-scope startup'
}

$webPort = 9900
if (-not [string]::IsNullOrWhiteSpace($env:CHANLUN_WEB_PORT)) {
    $parsedWebPort = 0
    if (-not [int]::TryParse($env:CHANLUN_WEB_PORT, [ref]$parsedWebPort) -or $parsedWebPort -lt 1 -or $parsedWebPort -gt 65535) {
        Log ('ERROR: invalid CHANLUN_WEB_PORT: {0}' -f $env:CHANLUN_WEB_PORT)
        Log '===== web restart ABORTED ====='
        exit 1
    }
    $webPort = $parsedWebPort
}
$env:CHANLUN_WEB_PORT = [string]$webPort
if ([string]::IsNullOrWhiteSpace($env:CHANLUN_WEB_HOST)) {
    $env:CHANLUN_WEB_HOST = '127.0.0.1'
}
$probeHost = $env:CHANLUN_WEB_HOST.Trim()
if ($probeHost -eq '0.0.0.0' -or $probeHost -eq '::') { $probeHost = '127.0.0.1' }
if ($probeHost.Contains(':') -and -not $probeHost.StartsWith('[')) { $probeHost = "[$probeHost]" }
$healthUri = "http://${probeHost}:$webPort/readyz?market=a"
$liveUri = "http://${probeHost}:$webPort/livez"
$webUri = "http://${probeHost}:$webPort/"

try {
    $sourceRevision = Get-ApplicationSourceRevision -Root $ProjectRoot
} catch {
    Log ('ERROR: source attestation failed: {0}' -f $_.Exception.Message)
    Log '===== web restart ABORTED ====='
    exit 1
}
$deploymentRevision = '{0}.run.{1}' -f $sourceRevision, [guid]::NewGuid().ToString('N')
$env:CHANLUN_BUILD_REVISION = $deploymentRevision

$pyPathParts = @($SrcPath, $AppDir)
if ($env:PYTHONPATH) { $pyPathParts += $env:PYTHONPATH }
$env:PYTHONPATH = ($pyPathParts -join ';')
Log ('web bind host = {0}:{1}; source revision = {2}' -f $env:CHANLUN_WEB_HOST, $webPort, $sourceRevision)

# 无副作用预检：在内存中编译源码并检查运行模块。
$preflightCode = @"
import importlib.util
import os
from pathlib import Path
root = Path(os.environ['CHANLUN_PREFLIGHT_ROOT'])
for base in (root / 'src' / 'chanlun', root / 'web' / 'chanlun_chart'):
    for path in base.rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        source = path.read_bytes()
        compile(source, str(path), 'exec')
required = ('apscheduler', 'flask', 'flask_login', 'flask_wtf', 'numpy', 'pandas', 'sqlalchemy', 'tornado')
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit('missing runtime modules: ' + ', '.join(missing))
"@
$encodedPreflight = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($preflightCode))
$preflightLauncher = "import base64;exec(compile(base64.b64decode('$encodedPreflight'),'<chanlun-preflight>','exec'))"
$preflightInfo = New-Object Diagnostics.ProcessStartInfo
$preflightInfo.FileName = $PythonExe
$preflightInfo.Arguments = '-I -B -c "{0}"' -f $preflightLauncher
$preflightInfo.WorkingDirectory = $ProjectRoot
$preflightInfo.UseShellExecute = $false
$preflightInfo.CreateNoWindow = $true
$preflightInfo.RedirectStandardOutput = $true
$preflightInfo.RedirectStandardError = $true
$preflightInfo.EnvironmentVariables['CHANLUN_PREFLIGHT_ROOT'] = $ProjectRoot
$preflightProcess = New-Object Diagnostics.Process
$preflightProcess.StartInfo = $preflightInfo
try {
    $null = $preflightProcess.Start()
    if (-not $preflightProcess.WaitForExit($PreflightTimeoutSec * 1000)) {
        $preflightProcess.Kill()
        $preflightProcess.WaitForExit()
        throw "preflight timed out after ${PreflightTimeoutSec}s"
    }
    $preflightStdout = $preflightProcess.StandardOutput.ReadToEnd()
    $preflightStderr = $preflightProcess.StandardError.ReadToEnd()
    if ($preflightProcess.ExitCode -ne 0) {
        throw ('source preflight failed: {0} {1}' -f $preflightStdout.Trim(), $preflightStderr.Trim()).Trim()
    }
} catch {
    Log ('ERROR: side-effect-free preflight failed; existing service was not stopped: {0}' -f $_.Exception.Message)
    Log '===== web restart ABORTED ====='
    exit 1
} finally {
    $preflightProcess.Dispose()
}
Log 'side-effect-free source preflight passed'
if ($PreflightOnly) {
    Log '===== preflight validation DONE (no process was stopped) ====='
    exit 0
}

$deploymentMutexName = Get-DeploymentMutexName -Root $ProjectRoot -Port $webPort
try {
    $deploymentMutex = Enter-DeploymentMutex -Name $deploymentMutexName
} catch {
    Log ('ERROR: unable to acquire deployment single-flight lock {0}: {1}' -f $deploymentMutexName, $_.Exception.Message)
    Log '===== web restart ABORTED; no process was stopped ====='
    exit 1
}
if ($null -eq $deploymentMutex) {
    Log ('ERROR: another restart invocation owns deployment lock {0}; no process was stopped' -f $deploymentMutexName)
    Log '===== web restart ABORTED; no process was stopped ====='
    exit 1
}
Log ('deployment single-flight lock acquired: {0}; owner PID={1}' -f $deploymentMutexName, $PID)

try {
# --- 1. 先停止网页项目 -------------------------------------------------------
$portOwners = @(Get-ListeningProcessIds -Port $webPort)
$webProcs = @(Get-WebProcs)
$webProcIds = @($webProcs | ForEach-Object { [int]$_.ProcessId })
$unrecognizedOwners = @($portOwners | Where-Object { $webProcIds -notcontains [int]$_ })
if ($unrecognizedOwners.Count -gt 0) {
    $attestedWebProcs = @(
        Get-AttestedWebProcs `
            -PortOwnerIds $unrecognizedOwners `
            -HealthUri $healthUri `
            -ExpectedSourceRevision $sourceRevision
    )
    foreach ($process in $attestedWebProcs) {
        if ($webProcIds -contains [int]$process.ProcessId) { continue }
        $webProcs += $process
        $webProcIds += [int]$process.ProcessId
        Log ('accepted endpoint-attested web PID={0} with restricted process metadata' -f $process.ProcessId)
    }
}
$foreignOwners = @($portOwners | Where-Object { $webProcIds -notcontains [int]$_ })
if ($foreignOwners.Count -gt 0) {
    Log ('ERROR: configured port {0} is owned by unrelated PID(s): {1}; existing processes were not stopped' -f $webPort, ($foreignOwners -join ','))
    Log '===== web restart ABORTED ====='
    exit 1
}
$targetWebProcs = @($webProcs | Where-Object { $portOwners -contains [int]$_.ProcessId })
if ($portOwners.Count -eq 0 -and $webProcs.Count -gt 0) {
    Log ('found app.py process(es) not owning configured port {0}; leave them untouched' -f $webPort)
}
$script:PreviousWebWasRunning = $targetWebProcs.Count -gt 0
$script:RollbackPythonExe = if ($script:PreviousWebWasRunning) { [string]$targetWebProcs[0].ExecutablePath } else { '' }

foreach ($process in $targetWebProcs) {
    try {
        Log ('stop web project PID={0} owning port {1}' -f $process.ProcessId, $webPort)
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    } catch {
        Log ('WARNING: stop request failed for PID={0}: {1}' -f $process.ProcessId, $_.Exception.Message)
    }
}
foreach ($process in $targetWebProcs) {
    try {
        Wait-Process -Id $process.ProcessId -Timeout 15 -ErrorAction Stop
    } catch {
        # 下方会独立确认；进程已退出时 Wait-Process 也可能抛错。
    }
}
$remainingWebIds = @($targetWebProcs | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue } | ForEach-Object { [int]$_.ProcessId })
$remainingPortOwners = @(Get-ListeningProcessIds -Port $webPort)
# Windows 可能先释放监听套接字，终止进程稍后才从进程表消失。为已停止进程保留有界
# 宽限期，避免因短暂残留 PID 错误中止。
for ($i = 0; $i -lt 15 -and ($remainingWebIds.Count -gt 0 -or $remainingPortOwners.Count -gt 0); $i++) {
    Start-Sleep -Seconds 1
    $remainingWebIds = @(
        $targetWebProcs |
            Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue } |
            ForEach-Object { [int]$_.ProcessId }
    )
    $remainingPortOwners = @(Get-ListeningProcessIds -Port $webPort)
}
if ($remainingWebIds.Count -gt 0 -or $remainingPortOwners.Count -gt 0) {
    Log ('ERROR: web process or listening port remained after stop; PIDs={0}; port owners={1}' -f ($remainingWebIds -join ','), ($remainingPortOwners -join ','))
    Log '===== web restart ABORTED ====='
    exit 1
}
if (-not $script:PreviousWebWasRunning) { Log 'web project not running on configured port, skip stop' }

# An elevated deployment needs its extra privilege only to stop an elevated
# legacy process.  Starting the replacement in the same token would make every
# later release require UAC again.  Hand the empty port to a one-shot scheduled
# task that explicitly runs under the interactive user's limited token.
if (Test-CurrentProcessElevated) {
    $handoffToken = ($deploymentMutexName -replace '[^A-Za-z0-9]', '')
    if ($handoffToken.Length -gt 16) {
        $handoffToken = $handoffToken.Substring($handoffToken.Length - 16)
    }
    $handoffTaskName = 'ChanlunProWebLaunch-{0}-{1}' -f $handoffToken, $PID
    try {
        Register-LimitedWebLaunchTask `
            -TaskName $handoffTaskName `
            -ScriptPath $PSCommandPath `
            -TimeoutSeconds $WebReadinessTimeoutSeconds `
            -LargeScopeEnabled $EnableLargeScreeningScope.IsPresent `
            -LargeHoldingMonitorScopeEnabled `
                $EnableLargeHoldingMonitorScope.IsPresent `
            -FullSymbolCatalogEnabled $EnableFullSymbolCatalog.IsPresent `
            -FullCoverageEnabled $EnableFullCoverage.IsPresent `
            -ForcedFullCoverageEnabled $ForceFullCoverageUntilComplete.IsPresent
    } catch {
        Abort-AfterWebStop -Reason (
            'failed to register limited-token Web launch handoff: {0}' -f `
                $_.Exception.Message
        )
    }

    # HANDOFF-LOCK-RELEASE: the limited child runs the same deployment script
    # and must be able to acquire the single-flight mutex before starting Web.
    Log ('deployment single-flight lock released for limited-token handoff: {0}; owner PID={1}' -f $deploymentMutexName, $PID)
    Exit-DeploymentMutex -Mutex $deploymentMutex
    $deploymentMutex = $null

    $handoffHealthy = $false
    $handoffHealth = $null
    $handoffPid = 0
    $handoffLastDetail = 'limited-token launch not started'
    try {
        # HANDOFF-LIMITED-START: the task principal is RunLevel Limited.
        Start-ScheduledTask -TaskName $handoffTaskName
        Log ('limited-token Web launch requested via scheduled task {0}' -f $handoffTaskName)
        $handoffDeadline = (Get-Date).AddSeconds($WebReadinessTimeoutSeconds)
        $expectedHandoffRevisionPrefix = '{0}.run.' -f $sourceRevision
        do {
            Start-Sleep -Seconds 2
            try {
                $candidateHealth = Invoke-RestMethod `
                    -Uri $healthUri `
                    -Method Get `
                    -TimeoutSec 3
                $handoffLastDetail = (
                    $candidateHealth | ConvertTo-Json -Compress -Depth 5
                )
                $candidatePid = 0
                $candidateOwners = @(Get-ListeningProcessIds -Port $webPort)
                if (
                    $candidateHealth.status -eq 'ready' -and
                    [string]$candidateHealth.revision -and
                    [string]$candidateHealth.revision.StartsWith(
                        $expectedHandoffRevisionPrefix,
                        [StringComparison]::Ordinal
                    ) -and
                    [int]::TryParse(
                        [string]$candidateHealth.pid,
                        [ref]$candidatePid
                    ) -and
                    $candidateOwners -contains $candidatePid
                ) {
                    $handoffHealthy = $true
                    $handoffHealth = $candidateHealth
                    $handoffPid = $candidatePid
                    break
                }
            } catch {
                $handoffLastDetail = $_.Exception.Message
            }
        } while ((Get-Date) -lt $handoffDeadline)

        if (-not $handoffHealthy) {
            Log ('ERROR: limited-token Web launch failed readiness; last readiness: {0}' -f $handoffLastDetail)
            $handoffOwners = @(Get-ListeningProcessIds -Port $webPort)
            if ($handoffOwners.Count -eq 0) {
                $null = Restore-WebService -Reason 'limited-token launch did not bind its port'
            } else {
                Log ('preserving not-ready handoff PID(s)={0} because the configured port is owned' -f ($handoffOwners -join ','))
            }
            Log '===== web restart ABORTED ====='
            exit 1
        }

        Log ('web project ready under limited token PID={0}; open {1}' -f $handoffPid, $healthUri)
        $handoffRevision = [string]$handoffHealth.revision
        $verifyOutput = & powershell.exe `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $verifyScript `
            -ProjectRoot $ProjectRoot `
            -HealthUri $healthUri `
            -ExpectedRevision $handoffRevision `
            -ExpectedSourceRevision $sourceRevision `
            -ExpectedProcessId $handoffPid `
            -SkipFreshnessCheck 2>&1
        $verifyExit = $LASTEXITCODE
        foreach ($line in $verifyOutput) { Log ('deploy verify: {0}' -f $line) }
        if ($verifyExit -ne 0) {
            Log ('ERROR: limited-token deployment verification failed; preserving ready PID={0}' -f $handoffPid)
            Log '===== web restart ABORTED ====='
            exit 1
        }
        try {
            Write-WatchdogDeploymentScope `
                -Path $watchdogScopePath `
                -Port $webPort `
                -LargeScreeningScopeEnabled $EnableLargeScreeningScope.IsPresent `
                -LargeHoldingMonitorScopeEnabled `
                    $EnableLargeHoldingMonitorScope.IsPresent `
                -FullSymbolCatalogEnabled $EnableFullSymbolCatalog.IsPresent `
                -FullCoverageEnabled $EnableFullCoverage.IsPresent `
                -ForcedFullCoverageEnabled `
                    $ForceFullCoverageUntilComplete.IsPresent
        } catch {
            Log ('ERROR: unable to persist watchdog deployment scope: {0}' -f $_.Exception.Message)
            Log '===== web restart ABORTED ====='
            exit 1
        }
        if (-not $SkipWatchdog) {
            # The handoff child exits after starting Web and deliberately cannot
            # own a long-running watchdog.  Replace an exact stale instance,
            # then let the installed Limited task load the authenticated scope
            # persisted above.  This keeps both Web and its recovery path out of
            # the elevated deployment token.
            $watchdogScriptToken = '-File {0}' -f $watchdogScript
            $watchdogRootToken = '-ProjectRoot {0}' -f $ProjectRoot
            $watchdogPortToken = '-WebPort {0}' -f $webPort
            $existingWatchdogs = @(
                Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
                    Where-Object {
                        if ([string]::IsNullOrWhiteSpace($_.CommandLine)) {
                            return $false
                        }
                        $unquotedCommandLine = $_.CommandLine.Replace('"', '')
                        return (
                            $_.ProcessId -ne $PID -and
                            $unquotedCommandLine.IndexOf(
                                $watchdogScriptToken,
                                [StringComparison]::OrdinalIgnoreCase
                            ) -ge 0 -and
                            $unquotedCommandLine.IndexOf(
                                $watchdogRootToken,
                                [StringComparison]::OrdinalIgnoreCase
                            ) -ge 0 -and
                            $unquotedCommandLine.IndexOf(
                                $watchdogPortToken,
                                [StringComparison]::OrdinalIgnoreCase
                            ) -ge 0
                        )
                    }
            )
            foreach ($existingWatchdog in $existingWatchdogs) {
                Stop-Process `
                    -Id $existingWatchdog.ProcessId `
                    -Force `
                    -ErrorAction Stop
                Log ('stopped superseded web watchdog PID={0}' -f $existingWatchdog.ProcessId)
            }
            $watchdogInstallOutput = & powershell.exe `
                -NoProfile `
                -ExecutionPolicy Bypass `
                -File $watchdogInstaller `
                -ProjectRoot $ProjectRoot `
                -WebPort $webPort 2>&1
            $watchdogInstallExit = $LASTEXITCODE
            foreach ($line in $watchdogInstallOutput) {
                Log ('watchdog install: {0}' -f $line)
            }
            if ($watchdogInstallExit -ne 0) {
                Log ('ERROR: limited-token watchdog installation failed; preserving ready PID={0}' -f $handoffPid)
                Log '===== web restart ABORTED ====='
                exit 1
            }
            Log 'web watchdog launched from Limited scheduled task with persisted deployment scope'
        }
        if ($OpenBrowser) { Open-WebApplication -Uri $webUri }
        Log '===== web restart DONE ====='
        exit 0
    } finally {
        Remove-LimitedWebLaunchTask -TaskName $handoffTaskName
    }
}

# --- 2. 启动网页服务 ---------------------------------------------------------
try {
    $startedProcess = Start-WebProcess -PythonPath $PythonExe -Purpose 'restart'
} catch {
    Abort-AfterWebStop -Reason ('failed to start web project: {0}' -f $_.Exception.Message)
}

# --- 3. 校验就绪状态、精确 PID、端口所有者和源码 ----------------------------
# 全新选股周期必须先发布一批因果结构，严格就绪接口才可进入就绪状态。冷启动路径可能
# 超过 120 秒，因此部署门槛保持有界且可配置，但不放宽下方任何就绪判据。
$deadline = (Get-Date).AddSeconds($WebReadinessTimeoutSeconds)
$healthy = $false
$lastReadinessDetail = 'not checked'
do {
    Start-Sleep -Seconds 2
    $startedProcess.Refresh()
    if ($startedProcess.HasExited) { break }
    try {
        $health = Invoke-RestMethod -Uri $healthUri -Method Get -TimeoutSec 3
        $lastReadinessDetail = ($health | ConvertTo-Json -Compress -Depth 5)
        $owners = @(Get-ListeningProcessIds -Port $webPort)
        if (
            $health.status -eq 'ready' -and
            $health.revision -eq $deploymentRevision -and
            [string]$health.pid -eq [string]$startedProcess.Id -and
            $owners -contains $startedProcess.Id
        ) {
            $healthy = $true
            break
        }
    } catch {
        $lastReadinessDetail = $_.Exception.Message
    }
} while ((Get-Date) -lt $deadline)

if (-not $healthy) {
    Log ('ERROR: web project failed readiness; check {0}; last readiness: {1}' -f $script:LastWebErrorLog, $lastReadinessDetail)
    $startedProcess.Refresh()
    $newOwners = @(Get-ListeningProcessIds -Port $webPort)
    if (-not $startedProcess.HasExited -and $newOwners -contains $startedProcess.Id) {
        Log ('preserving not-ready web PID={0} because it owns the configured port; service remains available for diagnostics' -f $startedProcess.Id)
    } else {
        if (-not $startedProcess.HasExited) {
            Stop-Process -Id $startedProcess.Id -Force -ErrorAction SilentlyContinue
        }
        $null = Restore-WebService -Reason 'replacement web process did not bind its port'
    }
    Log '===== web restart ABORTED ====='
    exit 1
}
Log ('web project ready PID={0}; open {1}' -f $startedProcess.Id, $healthUri)

$verifyOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $verifyScript -ProjectRoot $ProjectRoot -HealthUri $healthUri -ExpectedRevision $deploymentRevision -ExpectedSourceRevision $sourceRevision -ExpectedProcessId $startedProcess.Id -SkipFreshnessCheck 2>&1
$verifyExit = $LASTEXITCODE
foreach ($line in $verifyOutput) { Log ('deploy verify: {0}' -f $line) }
if ($verifyExit -ne 0) {
    Log ('ERROR: deployment verification failed; preserving ready PID={0}' -f $startedProcess.Id)
    Log '===== web restart ABORTED ====='
    exit 1
}
try {
    Write-WatchdogDeploymentScope `
        -Path $watchdogScopePath `
        -Port $webPort `
        -LargeScreeningScopeEnabled $EnableLargeScreeningScope.IsPresent `
        -LargeHoldingMonitorScopeEnabled `
            $EnableLargeHoldingMonitorScope.IsPresent `
        -FullSymbolCatalogEnabled $EnableFullSymbolCatalog.IsPresent `
        -FullCoverageEnabled $EnableFullCoverage.IsPresent `
        -ForcedFullCoverageEnabled $ForceFullCoverageUntilComplete.IsPresent
} catch {
    Log ('ERROR: unable to persist watchdog deployment scope: {0}' -f $_.Exception.Message)
    Log '===== web restart ABORTED ====='
    exit 1
}
if (-not $SkipWatchdog) {
    # A watchdog is part of the deployed configuration, not merely a duplicate
    # background process.  Reusing one from an earlier bounded/full-scope launch
    # would silently restore stale flags after the next crash.  Replace only the
    # exact watchdog for this project root and port while the deployment mutex is
    # held; watchdogs for other worktrees or ports remain untouched.
    $watchdogScriptToken = '-File {0}' -f $watchdogScript
    $watchdogRootToken = '-ProjectRoot {0}' -f $ProjectRoot
    $watchdogPortToken = '-WebPort {0}' -f $webPort
    $existingWatchdogs = @(
        Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
            Where-Object {
                if ([string]::IsNullOrWhiteSpace($_.CommandLine)) {
                    return $false
                }
                $unquotedCommandLine = $_.CommandLine.Replace('"', '')
                return (
                    $_.ProcessId -ne $PID -and
                    $unquotedCommandLine.IndexOf(
                        $watchdogScriptToken,
                        [StringComparison]::OrdinalIgnoreCase
                    ) -ge 0 -and
                    $unquotedCommandLine.IndexOf(
                        $watchdogRootToken,
                        [StringComparison]::OrdinalIgnoreCase
                    ) -ge 0 -and
                    $unquotedCommandLine.IndexOf(
                        $watchdogPortToken,
                        [StringComparison]::OrdinalIgnoreCase
                    ) -ge 0
                )
            }
    )
    foreach ($existingWatchdog in $existingWatchdogs) {
        Stop-Process `
            -Id $existingWatchdog.ProcessId `
            -Force `
            -ErrorAction Stop
        Log ('stopped superseded web watchdog PID={0}' -f $existingWatchdog.ProcessId)
    }
    $watchdogArguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        ('"{0}"' -f $watchdogScript),
        '-ProjectRoot',
        ('"{0}"' -f $ProjectRoot),
        '-WebPort',
        [string]$webPort
    )
    if ($EnableLargeScreeningScope) {
        $watchdogArguments += '-EnableLargeScreeningScope'
    }
    if ($EnableLargeHoldingMonitorScope) {
        $watchdogArguments += '-EnableLargeHoldingMonitorScope'
    }
    if ($EnableFullSymbolCatalog) {
        $watchdogArguments += '-EnableFullSymbolCatalog'
    }
    if ($EnableFullCoverage) {
        $watchdogArguments += '-EnableFullCoverage'
    }
    if ($ForceFullCoverageUntilComplete) {
        $watchdogArguments += '-ForceFullCoverageUntilComplete'
    }
    $watchdogProcess = Start-Process `
        -FilePath 'powershell.exe' `
        -ArgumentList $watchdogArguments `
        -WindowStyle Hidden `
        -PassThru
    Log ('web watchdog launched PID={0} with current deployment scope' -f $watchdogProcess.Id)
}
if ($OpenBrowser) { Open-WebApplication -Uri $webUri }
Log '===== web restart DONE ====='
} finally {
    if ($null -ne $deploymentMutex) {
        try {
            Log ('deployment single-flight lock released: {0}; owner PID={1}' -f $deploymentMutexName, $PID)
        } finally {
            Exit-DeploymentMutex -Mutex $deploymentMutex
        }
    }
}
exit 0
