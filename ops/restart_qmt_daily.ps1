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
    [switch]$WebOnly,
    [ValidateRange(30, 1800)]
    [int]$WebReadinessTimeoutSeconds = 1800,
    [ValidateRange(1, 720)]
    [int]$CatchUpWindowMinutes = 90
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy_common.ps1')

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
$SchedulerStateDir = Join-Path $ProjectRoot '.cache\chanlun_v3_scheduler'
$QmtRegistrationReceipt = Join-Path $SchedulerStateDir 'qmt_restart_registration.json'
$QmtSuccessReceipt = Join-Path $SchedulerStateDir 'qmt_restart_success.json'
$AppQmtOwnerReceipt = Join-Path $SchedulerStateDir 'qmt_execution_owner.json'
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

function Get-LiveAppQmtOwner {
    if (-not (Test-Path -LiteralPath $AppQmtOwnerReceipt -PathType Leaf)) {
        return $null
    }
    try {
        $owner = Get-Content -LiteralPath $AppQmtOwnerReceipt -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if (
            $owner.schema -ne 'chanlun-qmt-execution-owner/v1' -or
            $owner.contract_id -ne 'chanlun-qmt-runtime/app-runtime-contract/v1' -or
            $owner.owner -ne 'APP_RUNTIME' -or
            $owner.project_root -ne $ProjectRoot -or
            $owner.real_account_accessed -ne $false -or
            $owner.real_order_transport_enabled -ne $false -or
            $owner.automated_order_authorized -ne $false -or
            $owner.live_status -ne 'LIVE_DISABLED'
        ) {
            return $null
        }
        $heartbeat = [DateTimeOffset]::Parse(
            [string]$owner.heartbeat_at,
            [Globalization.CultureInfo]::InvariantCulture
        )
        if (([DateTimeOffset]::Now - $heartbeat).Duration().TotalMinutes -gt 15) {
            return $null
        }
        $process = Get-Process -Id ([int]$owner.pid) -ErrorAction Stop
        if ($process.Id -ne [int]$owner.pid) { return $null }
        return $owner
    } catch {
        return $null
    }
}

function Write-QmtSchedulerSuccessReceipt {
    param(
        [Parameter(Mandatory = $true)][int]$WebProcessId,
        [Parameter(Mandatory = $true)][string]$HealthUri,
        [Parameter(Mandatory = $true)][string]$DeploymentRevision,
        [Parameter(Mandatory = $true)][string]$SourceRevision
    )

    if (-not (Test-Path -LiteralPath $QmtRegistrationReceipt -PathType Leaf)) {
        # Keep a successfully recovered QMT/web service available even when an
        # old task definition launches this newer script.  The audit remains
        # fail-closed because no success receipt is published; re-registration
        # is still required before operational verification can turn green.
        Log 'WARNING: QMT task registration receipt is missing; operational success was not attested'
        return
    }
    $registrationHash = 'sha256:{0}' -f (
        (Get-FileHash -LiteralPath $QmtRegistrationReceipt -Algorithm SHA256).Hash.ToLowerInvariant()
    )
    $document = [ordered]@{
        schema = 'chanlun-qmt-restart-task-success/v1'
        completed_at = (Get-Date).ToString(
            'yyyy-MM-ddTHH:mm:ss.ffffffK',
            [Globalization.CultureInfo]::InvariantCulture
        )
        task_name = 'Chanlun-QMT-DailyRestart'
        registration_receipt_sha256 = $registrationHash
        web_process_id = $WebProcessId
        health_uri = $HealthUri
        deployment_revision = $DeploymentRevision
        application_source_revision = $SourceRevision
        qmt_restart_completed = $true
        web_readiness_verified = $true
        real_account_accessed = $false
        real_order_transport_enabled = $false
        automated_order_authorized = $false
        live_status = 'LIVE_DISABLED'
    }
    $null = New-Item -ItemType Directory -Path $SchedulerStateDir -Force
    $temporary = '{0}.{1}.tmp' -f $QmtSuccessReceipt, $PID
    try {
        [IO.File]::WriteAllText(
            $temporary,
            ($document | ConvertTo-Json -Depth 5 -Compress),
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $QmtSuccessReceipt -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    Log ('scheduler success receipt published: {0}' -f $QmtSuccessReceipt)
}

function Get-DeploymentMutexName {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$Port
    )

    # Scope the lock to the exact checkout and configured listener.  Another
    # checkout or a deliberately separate port may deploy independently, while
    # two invocations that could stop/start the same service are serialized.
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
            # Fail fast.  A second deploy must never wait invisibly and then
            # mutate a service whose ownership facts were measured earlier.
            $acquired = $mutex.WaitOne(0)
        } catch [Threading.AbandonedMutexException] {
            # Windows transfers ownership to this thread when the previous
            # process died without releasing the mutex.  The abandoned state
            # is therefore recoverable and still provides exclusive ownership.
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
    # Poetry may emit an informational message such as
    # "Skipping virtualenv creation, as specified in config file." on stderr
    # while still returning exit code 0 and printing a perfectly usable Python
    # path on stdout.  With the script-wide ErrorActionPreference=Stop,
    # Windows PowerShell promotes that harmless native stderr record to a
    # terminating error before we can inspect LASTEXITCODE.  Limit the relaxed
    # preference to this read-only resolver call; the exit code and returned
    # executable remain the authoritative checks below.
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
        # Native stderr records remain ErrorRecord objects after 2>&1.  Do not
        # pass their ANSI-decorated display text to Test-Path.
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
    # Older/manual launchers may preserve the repository-relative script path
    # in Win32_Process.CommandLine.  Recognize only this exact project-relative
    # path; the caller still requires the PID to own the configured port before
    # it is eligible for shutdown.
    $relativeAppPattern = '(?i)(?:^|[\s"])web[\\/]+chanlun_chart[\\/]+app\.py(?:[\s"]|$)'
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -match $appPattern -or
                $_.CommandLine -match $relativeAppPattern
            )
        })
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

# Migration single-owner guard.  Once app.py has claimed and is heartbeating
# the QMT runtime, the legacy scheduled task must never stop that same app or
# race its 08:30 job.  -Force remains an explicit operator rollback; -WebOnly
# remains a deploy operation that intentionally does not touch QMT.
if (-not $Force -and -not $WebOnly -and -not $PreflightOnly) {
    $appQmtOwner = Get-LiveAppQmtOwner
    if ($null -ne $appQmtOwner) {
        Log ('app.py PID={0} owns QMT runtime; legacy daily task refused to run' -f $appQmtOwner.pid)
        Log '===== daily restart SKIPPED (APP_RUNTIME owner) ====='
        exit 76
    }
}

$now = Get-Date
$scheduledGate = $now.Date.AddHours(8).AddMinutes(30)
$catchUpDeadline = $scheduledGate.AddMinutes($CatchUpWindowMinutes)
if (-not $Force -and ($now -lt $scheduledGate -or $now -gt $catchUpDeadline)) {
    Log ('outside the scheduled catch-up window ({0:HH:mm}-{1:HH:mm}); skip. Use -Force for a manual run' -f $scheduledGate, $catchUpDeadline)
    Log '===== daily restart SKIPPED ====='
    exit 0
}

# Resolve target QMT before touching any running process.
if (-not $PreflightOnly -and -not $WebOnly) {
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

$deploymentMutexName = Get-DeploymentMutexName -Root $ProjectRoot -Port $webPort
try {
    $deploymentMutex = Enter-DeploymentMutex -Name $deploymentMutexName
} catch {
    Log ('ERROR: unable to acquire deployment single-flight lock {0}: {1}' -f $deploymentMutexName, $_.Exception.Message)
    Log '===== daily restart ABORTED; no process was stopped ====='
    exit 1
}
if ($null -eq $deploymentMutex) {
    Log ('ERROR: another restart invocation owns deployment lock {0}; no process was stopped' -f $deploymentMutexName)
    Log '===== daily restart ABORTED; no process was stopped ====='
    exit 1
}
Log ('deployment single-flight lock acquired: {0}; owner PID={1}' -f $deploymentMutexName, $PID)

try {
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
# Windows can release the listening socket before the terminated process
# disappears from the process table.  Give that already-stopped process a
# bounded grace period instead of aborting on a transient stale PID.
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
    Log '===== daily restart ABORTED; QMT was not touched ====='
    exit 1
}
if (-not $script:PreviousWebWasRunning) { Log 'web project not running on configured port, skip stop' }

# --- 2. Stop only the configured QMT instance -------------------------------
if (-not $WebOnly) {
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
} else {
    Log 'web-only reload requested; QMT processes were not touched'
}

# --- 5. Start web ------------------------------------------------------------
try {
    $startedProcess = Start-WebProcess -PythonPath $PythonExe -Purpose 'restart'
} catch {
    Abort-AfterWebStop -Reason ('failed to start web project: {0}' -f $_.Exception.Message)
}

# --- 6. Verify readiness, exact PID, port owner, and source -----------------
# A clean screening epoch must publish one causal structure batch before the
# strict readiness endpoint may become ready.  That cold path is intentionally
# slower than the historical 120-second process-start timeout, so keep the
# deployment gate bounded but configurable without weakening any readiness
# predicate below.
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
if (-not $Force -and -not $WebOnly) {
    Write-QmtSchedulerSuccessReceipt `
        -WebProcessId $startedProcess.Id `
        -HealthUri $healthUri `
        -DeploymentRevision $deploymentRevision `
        -SourceRevision $sourceRevision
}
Log '===== daily restart DONE ====='
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
