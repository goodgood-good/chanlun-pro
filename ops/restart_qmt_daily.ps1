# ============================================================================
# restart_qmt_daily.ps1
#
# Restart the configured miniQMT instance and the chanlun-pro web application.
# Scheduled invocations are accepted only during the bounded catch-up window.
# Use -Force for an intentional manual run outside that window.
# ============================================================================

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$PreflightOnly,
    [ValidateRange(1, 720)]
    [int]$CatchUpWindowMinutes = 90
)

$ErrorActionPreference = 'Stop'

# ------------------------------- CONFIG -------------------------------------
$ProjectRoot  = Split-Path -Parent $PSScriptRoot
$AppDir       = Join-Path $ProjectRoot 'web\chanlun_chart'
$SrcPath      = Join-Path $ProjectRoot 'src'
$AppScript    = Join-Path $AppDir 'app.py'
$verifyScript = Join-Path $ProjectRoot 'ops\verify_deploy.ps1'
$QmtProcName  = 'XtMiniQmt'
$QmtQuoteProc = 'miniquote'
$QmtLauncher  = 'XtItClient'
$QmtPathFile  = Join-Path $PSScriptRoot 'qmt_exe_path.txt'
$QmtWarmupSec = 90
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

function Import-ProjectDotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    try {
        $bytes = [IO.File]::ReadAllBytes($Path)
        try {
            $strictUtf8 = New-Object Text.UTF8Encoding($true, $true)
            $content = $strictUtf8.GetString($bytes)
            if ($content.Length -gt 0 -and $content[0] -eq [char]0xfeff) {
                $content = $content.Substring(1)
            }
        } catch [Text.DecoderFallbackException] {
            $content = [Text.Encoding]::GetEncoding(936).GetString($bytes)
        }
    } catch {
        throw "unable to read .env: $($_.Exception.Message)"
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
        if ($null -ne [Environment]::GetEnvironmentVariable($key, 'Process')) { continue }
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
    try {
        $output = @(& $poetry.Source run python -I -c 'import sys; print(sys.executable)' 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw ('Poetry could not resolve project Python: {0}' -f ($output -join ' '))
    }
    foreach ($line in $output) {
        $candidate = ([string]$line).Trim()
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Poetry returned no usable Python executable'
}

function Get-ApplicationSourceRevision {
    param([Parameter(Mandatory = $true)][string]$Root)

    $headOutput = @(& git -C $Root rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $headOutput.Count -eq 0) {
        throw 'unable to resolve deployment git revision'
    }
    $head = ([string]$headOutput[-1]).Trim()
    if ([string]::IsNullOrWhiteSpace($head)) {
        throw 'deployment git revision is empty'
    }

    $paths = @(& git -C $Root -c core.quotePath=false ls-files --cached --others --exclude-standard -- src web/chanlun_chart ops windows_run.bat 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'unable to enumerate application source files'
    }
    $runtimeConfig = 'src/chanlun/config.py'
    if ((Test-Path -LiteralPath (Join-Path $Root $runtimeConfig) -PathType Leaf) -and $paths -notcontains $runtimeConfig) {
        $paths += $runtimeConfig
    }
    $paths = @($paths | Sort-Object -Unique)
    $existing = @($paths | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) -PathType Leaf })
    $hashes = @()
    if ($existing.Count -gt 0) {
        $hashes = @($existing | & git -C $Root hash-object --no-filters --stdin-paths 2>$null)
        if ($LASTEXITCODE -ne 0 -or $hashes.Count -ne $existing.Count) {
            throw 'unable to hash application source files'
        }
    }
    $hashByPath = @{}
    for ($i = 0; $i -lt $existing.Count; $i++) {
        $hashByPath[$existing[$i]] = ([string]$hashes[$i]).Trim()
    }
    $manifest = New-Object System.Collections.Generic.List[string]
    $manifest.Add("HEAD`t$head")
    foreach ($path in $paths) {
        $hash = if ($hashByPath.ContainsKey($path)) { $hashByPath[$path] } else { 'deleted' }
        $manifest.Add(('{0}`t{1}' -f $path, $hash))
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($manifest -join "`n"))
        $digest = -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
    } finally {
        $sha.Dispose()
    }
    return ('{0}.tree.{1}' -f $head, $digest.Substring(0, 24))
}

function Get-WebProcs {
    $appPattern = '(?i)(?:^|[\s"])' + [regex]::Escape($AppScript) + '(?:[\s"]|$)'
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $appPattern })
}

function Get-ListeningProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { [int]$_.LocalPort -eq $Port } |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-TargetQmt {
    param([string[]]$Names, [string]$Dir)
    @(Get-Process -Name $Names -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and (Split-Path -LiteralPath $_.Path) -ieq $Dir })
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
            # Startup may still be in progress.
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
    Log '===== daily restart ABORTED ====='
    exit 1
}

Log '===== daily restart START ====='

$now = Get-Date
$scheduledGate = $now.Date.AddHours(8).AddMinutes(30)
$catchUpDeadline = $scheduledGate.AddMinutes($CatchUpWindowMinutes)
if (-not $Force -and ($now -lt $scheduledGate -or $now -gt $catchUpDeadline)) {
    Log ('outside the scheduled catch-up window ({0:HH:mm}-{1:HH:mm}); skip. Use -Force for a manual run' -f $scheduledGate, $catchUpDeadline)
    Log '===== daily restart SKIPPED ====='
    exit 0
}

# Resolve target QMT before touching any running process.
if (-not $PreflightOnly) {
$qmtExe = $null
if (Test-Path -LiteralPath $QmtPathFile) {
    foreach ($line in (Get-Content -LiteralPath $QmtPathFile -Encoding UTF8 -ErrorAction SilentlyContinue)) {
        $candidate = $line.Trim()
        if ($candidate -and -not $candidate.StartsWith('#')) { $qmtExe = $candidate; break }
    }
}
if (-not $qmtExe -or -not (Test-Path -LiteralPath $qmtExe -PathType Leaf)) {
    Log ('ERROR: QMT exe not resolved from {0} (value: {1})' -f $QmtPathFile, $qmtExe)
    Log '===== daily restart ABORTED ====='
    exit 1
}
$qmtDir = Split-Path -LiteralPath $qmtExe
Log ('target QMT = {0}' -f $qmtExe)
}

# Validate all web prerequisites before stopping anything.
foreach ($requiredDir in @($ProjectRoot, $AppDir, $SrcPath)) {
    if (-not (Test-Path -LiteralPath $requiredDir -PathType Container)) {
        Log ('ERROR: required directory not found: {0}' -f $requiredDir)
        Log '===== daily restart ABORTED ====='
        exit 1
    }
}
foreach ($requiredFile in @($AppScript, $verifyScript)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        Log ('ERROR: required file not found: {0}' -f $requiredFile)
        Log '===== daily restart ABORTED ====='
        exit 1
    }
}
if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
    Log 'ERROR: git is required for source attestation'
    Log '===== daily restart ABORTED ====='
    exit 1
}
if ($null -eq (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    Log 'ERROR: Get-NetTCPConnection is required for port ownership verification'
    Log '===== daily restart ABORTED ====='
    exit 1
}

try {
    Import-ProjectDotEnv -Path (Join-Path $ProjectRoot '.env')
    $PythonExe = Resolve-ProjectPython
} catch {
    Log ('ERROR: Python/environment preflight failed: {0}' -f $_.Exception.Message)
    Log '===== daily restart ABORTED ====='
    exit 1
}
Log ('project Python = {0}' -f $PythonExe)

$webPort = 9900
if (-not [string]::IsNullOrWhiteSpace($env:CHANLUN_WEB_PORT)) {
    $parsedWebPort = 0
    if (-not [int]::TryParse($env:CHANLUN_WEB_PORT, [ref]$parsedWebPort) -or $parsedWebPort -lt 1 -or $parsedWebPort -gt 65535) {
        Log ('ERROR: invalid CHANLUN_WEB_PORT: {0}' -f $env:CHANLUN_WEB_PORT)
        Log '===== daily restart ABORTED ====='
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
    Log '===== daily restart ABORTED ====='
    exit 1
}
$deploymentRevision = '{0}.run.{1}' -f $sourceRevision, [guid]::NewGuid().ToString('N')
$env:CHANLUN_BUILD_REVISION = $deploymentRevision

$pyPathParts = @($SrcPath, $AppDir)
if ($env:PYTHONPATH) { $pyPathParts += $env:PYTHONPATH }
$env:PYTHONPATH = ($pyPathParts -join ';')
Log ('web bind host = {0}:{1}; source revision = {2}' -f $env:CHANLUN_WEB_HOST, $webPort, $sourceRevision)

# Side-effect-free preflight: compile sources in memory and check module specs.
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
    Log '===== daily restart ABORTED ====='
    exit 1
} finally {
    $preflightProcess.Dispose()
}
Log 'side-effect-free source preflight passed'
if ($PreflightOnly) {
    Log '===== preflight validation DONE (no process was stopped) ====='
    exit 0
}

# --- 1. Stop the web project FIRST ------------------------------------------
$webProcs = @(Get-WebProcs)
$webProcIds = @($webProcs | ForEach-Object { [int]$_.ProcessId })
$portOwners = @(Get-ListeningProcessIds -Port $webPort)
$foreignOwners = @($portOwners | Where-Object { $webProcIds -notcontains [int]$_ })
if ($foreignOwners.Count -gt 0) {
    Log ('ERROR: configured port {0} is owned by unrelated PID(s): {1}; existing processes were not stopped' -f $webPort, ($foreignOwners -join ','))
    Log '===== daily restart ABORTED ====='
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
        # Confirm independently below; Wait-Process also throws if already gone.
    }
}
$remainingWebIds = @($targetWebProcs | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue } | ForEach-Object { [int]$_.ProcessId })
$remainingPortOwners = @(Get-ListeningProcessIds -Port $webPort)
if ($remainingWebIds.Count -gt 0 -or $remainingPortOwners.Count -gt 0) {
    Log ('ERROR: web process or listening port remained after stop; PIDs={0}; port owners={1}' -f ($remainingWebIds -join ','), ($remainingPortOwners -join ','))
    Log '===== daily restart ABORTED; QMT was not touched ====='
    exit 1
}
if (-not $script:PreviousWebWasRunning) { Log 'web project not running on configured port, skip stop' }

# --- 2. Stop only the configured QMT instance -------------------------------
$targets = @(Get-TargetQmt -Names @($QmtLauncher, $QmtProcName, $QmtQuoteProc) -Dir $qmtDir)
if ($targets.Count -gt 0) {
    try {
        Log ('stop target QMT processes (count={0})' -f $targets.Count)
        $targets | Stop-Process -Force -ErrorAction Stop
    } catch {
        Abort-AfterWebStop -Reason ('failed to stop target QMT: {0}' -f $_.Exception.Message)
    }
} else {
    Log 'target QMT not running, skip stop'
}
for ($i = 0; $i -lt 20; $i++) {
    if (@(Get-TargetQmt -Names @($QmtProcName) -Dir $qmtDir).Count -eq 0) { break }
    Start-Sleep -Seconds 1
}
if (@(Get-TargetQmt -Names @($QmtProcName) -Dir $qmtDir).Count -gt 0) {
    Abort-AfterWebStop -Reason 'target QMT still alive after stop; refusing a double instance'
}

# --- 3. Start QMT -----------------------------------------------------------
try {
    Log ('start QMT launcher: {0}' -f $qmtExe)
    Start-Process -FilePath $qmtExe -WorkingDirectory $qmtDir -ErrorAction Stop | Out-Null
} catch {
    Abort-AfterWebStop -Reason ('failed to start QMT launcher: {0}' -f $_.Exception.Message)
}

# --- 4. Wait for QMT and warm it up -----------------------------------------
$appeared = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 1
    if (@(Get-TargetQmt -Names @($QmtProcName) -Dir $qmtDir).Count -gt 0) { $appeared = $true; break }
}
if (-not $appeared) {
    Abort-AfterWebStop -Reason 'QMT terminal did not appear within 120s'
}
Log ('QMT process up; wait {0}s for login and service warmup' -f $QmtWarmupSec)
Start-Sleep -Seconds $QmtWarmupSec

# --- 5. Start web ------------------------------------------------------------
try {
    $startedProcess = Start-WebProcess -PythonPath $PythonExe -Purpose 'restart'
} catch {
    Abort-AfterWebStop -Reason ('failed to start web project: {0}' -f $_.Exception.Message)
}

# --- 6. Verify readiness, exact PID, port owner, and source -----------------
$deadline = (Get-Date).AddSeconds(120)
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
    Log '===== daily restart ABORTED ====='
    exit 1
}
Log ('web project ready PID={0}; open {1}' -f $startedProcess.Id, $healthUri)

$verifyOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $verifyScript -ProjectRoot $ProjectRoot -HealthUri $healthUri -ExpectedRevision $deploymentRevision -ExpectedSourceRevision $sourceRevision -ExpectedProcessId $startedProcess.Id -SkipFreshnessCheck 2>&1
$verifyExit = $LASTEXITCODE
foreach ($line in $verifyOutput) { Log ('deploy verify: {0}' -f $line) }
if ($verifyExit -ne 0) {
    Log ('ERROR: deployment verification failed; preserving ready PID={0}' -f $startedProcess.Id)
    Log '===== daily restart ABORTED ====='
    exit 1
}
Log '===== daily restart DONE ====='
exit 0
