# ============================================================================
# restart_web.ps1
#
# 重启 chanlun-pro 网页应用。应用进程通过 app_qmt_runtime.py 管理 QMT 生命周期；
# 部署脚本本身不修改 QMT。
# ============================================================================

[CmdletBinding()]
param(
    [switch]$PreflightOnly,
    [ValidateRange(30, 1800)]
    [int]$WebReadinessTimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy_common.ps1')

# -------------------------------- 配置 ---------------------------------------
$ProjectRoot  = Split-Path -Parent $PSScriptRoot
$AppDir       = Join-Path $ProjectRoot 'web\chanlun_chart'
$SrcPath      = Join-Path $ProjectRoot 'src'
$AppScript    = Join-Path $AppDir 'app.py'
$verifyScript = Join-Path $ProjectRoot 'ops\verify_deploy.ps1'
$PreflightTimeoutSec = 30
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

function Import-ProjectDotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

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
        if ($null -ne $currentValue) { continue }
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

function Get-WebProcs {
    $appPattern = '(?i)(?:^|[\s"])' + [regex]::Escape($AppScript) + '(?:[\s"]|$)'
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -match $appPattern
        })
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
    Import-ProjectDotEnv -Path (Join-Path $ProjectRoot '.env')
    $PythonExe = Resolve-ProjectPython
} catch {
    Log ('ERROR: Python/environment preflight failed: {0}' -f $_.Exception.Message)
    Log '===== web restart ABORTED ====='
    exit 1
}
Log ('project Python = {0}' -f $PythonExe)

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
$webProcs = @(Get-WebProcs)
$webProcIds = @($webProcs | ForEach-Object { [int]$_.ProcessId })
$portOwners = @(Get-ListeningProcessIds -Port $webPort)
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
