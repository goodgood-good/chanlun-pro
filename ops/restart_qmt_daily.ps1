# ============================================================================
# restart_qmt_daily.ps1
#
# Daily pre-market restart of the miniQMT terminal + chanlun-pro web project.
#
# WHY THIS EXISTS
#   The miniQMT terminal degrades after running continuously for several days.
#   A degraded terminal sends mis-framed BSON; that trips a native assertion
#   inside xtquant ("Assertion failed: u < 1000000 ... bsonobj.cpp") and kills
#   the whole Python process with exit code 0xC0000409. Python cannot catch it.
#   Restarting QMT every trading morning keeps the terminal fresh and prevents
#   the crash. Restarting only the project does NOT help -- a fresh project
#   still connects to the same degraded QMT, so QMT must be restarted too.
#
# SEQUENCE (order matters)
#   stop web project -> stop QMT -> start QMT -> wait warmup -> start web
#   The project is stopped FIRST so QMT is never killed from under a live
#   xtquant call (which would crash the project as well).
#
# QMT TARGET
#   This machine has more than one QMT install. The exact terminal to restart
#   is read from the sidecar file qmt_exe_path.txt (next to this script). Only
#   the QMT instance living in that folder is restarted; other brokerage
#   terminals are left untouched.
#
# USAGE
#   Triggered automatically by a Windows Scheduled Task (see
#   register_qmt_restart_task.ps1). Can also be run manually, pre-market.
# ============================================================================

$ErrorActionPreference = 'Continue'

# ------------------------------- CONFIG -------------------------------------
$ProjectRoot  = 'D:\project\chanlun-pro'
$AppDir       = Join-Path $ProjectRoot 'web\chanlun_chart'
$SrcPath      = Join-Path $ProjectRoot 'src'
# Absolute path to app.py. Launching with the ABSOLUTE path (not bare 'app.py')
# keeps the project folder in the process command line, so Get-WebProcs can
# still find the project process to stop it on the next restart.
$AppScript    = Join-Path $AppDir 'app.py'
# Python interpreter for the project: the conda env PyCharm uses, which has
# every required dependency installed. The project's .venv is incomplete, and
# the global D:\software\Python310 lacks the [us]/[hk] extras (alpaca, futu).
$PythonExe    = 'C:\Users\lc\miniconda3\envs\chanlun-pro\python.exe'
$QmtProcName  = 'XtMiniQmt'    # miniQMT main process (spawned after login)
$QmtQuoteProc = 'miniquote'    # miniQMT quote sub-process
$QmtLauncher  = 'XtItClient'   # QMT launcher process; what we actually start
$QmtPathFile  = Join-Path $PSScriptRoot 'qmt_exe_path.txt'  # holds the QMT exe path
$QmtWarmupSec = 90             # seconds to wait for QMT login + data service
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

# The python process running this project's app.py (matched by command line).
function Get-WebProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'chanlun-pro' -and $_.CommandLine -match 'app\.py' }
}

# QMT processes belonging ONLY to the target install (matched by exe folder),
# so the other brokerage's QMT terminal on this machine is never touched.
function Get-TargetQmt {
    param([string[]]$Names, [string]$Dir)
    # Split-Path -LiteralPath returns the parent by default; do NOT add -Parent
    # (in Windows PowerShell 5.1 that combination is an ambiguous parameter set).
    Get-Process -Name $Names -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and (Split-Path -LiteralPath $_.Path) -ieq $Dir }
}

Log '===== daily restart START ====='

# --- 0. Resolve the target QMT exe from the sidecar file --------------------
$qmtExe = $null
if (Test-Path -LiteralPath $QmtPathFile) {
    foreach ($line in (Get-Content -LiteralPath $QmtPathFile -Encoding UTF8 -ErrorAction SilentlyContinue)) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith('#')) { $qmtExe = $t; break }
    }
}
if (-not $qmtExe -or -not (Test-Path -LiteralPath $qmtExe)) {
    Log ('ERROR: QMT exe not resolved from {0} (value: {1})' -f $QmtPathFile, $qmtExe)
    Log '===== daily restart ABORTED ====='
    exit 1
}
$qmtDir = Split-Path -LiteralPath $qmtExe
Log ('target QMT = {0}' -f $qmtExe)

# --- 1. Stop the web project FIRST ------------------------------------------
$webProcs = Get-WebProcs
if ($webProcs) {
    foreach ($p in $webProcs) {
        Log ('stop web project PID={0}' -f $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Log 'web project not running, skip'
}
Start-Sleep -Seconds 3

# --- 2. Stop ONLY the target QMT instance -----------------------------------
$targets = Get-TargetQmt -Names @($QmtLauncher, $QmtProcName, $QmtQuoteProc) -Dir $qmtDir
if ($targets) {
    Log ('stop target QMT processes (count={0})' -f @($targets).Count)
    $targets | Stop-Process -Force -ErrorAction SilentlyContinue
} else {
    Log 'target QMT not running, skip stop'
}
# wait until the target XtMiniQmt has fully exited (max 20s)
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-TargetQmt -Names @($QmtProcName) -Dir $qmtDir)) { break }
    Start-Sleep -Seconds 1
}
if (Get-TargetQmt -Names @($QmtProcName) -Dir $qmtDir) {
    Log 'ERROR: target QMT still alive after kill; abort to avoid a double instance'
    Log '===== daily restart ABORTED ====='
    exit 1
}

# --- 3. Start QMT via its launcher XtItClient.exe ---------------------------
# XtItClient.exe performs the (auto-)login, then spawns XtMiniQmt.exe and
# miniquote.exe. It MUST be XtItClient.exe -- launching XtMiniQmt.exe directly
# does not log in. $qmtExe comes from qmt_exe_path.txt and points at the
# launcher. Non-elevated, working dir = bin.x64 (same as the desktop shortcut).
Log ('start QMT launcher: {0}' -f $qmtExe)
Start-Process -FilePath $qmtExe -WorkingDirectory $qmtDir

# --- 4. Wait for the QMT process to appear, then warm up the data service ---
$appeared = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 1
    if (Get-TargetQmt -Names @($QmtProcName) -Dir $qmtDir) { $appeared = $true; break }
}
if (-not $appeared) {
    Log 'ERROR: QMT terminal did not appear within 120s; skip starting web project'
    Log '===== daily restart ABORTED ====='
    exit 1
}
Log ('QMT process up; wait {0}s for login + data service warmup' -f $QmtWarmupSec)
Start-Sleep -Seconds $QmtWarmupSec

# --- 5. Start the web project (headless, no auto-opened browser) ------------
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Log ('ERROR: python exe not found: {0}' -f $PythonExe)
    Log '===== daily restart ABORTED ====='
    exit 1
}
$outLog = Join-Path $LogDir ('web_stdout_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
$errLog = Join-Path $LogDir ('web_stderr_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
Log ('start web project: {0} {1} nobrowser (cwd={2})' -f $PythonExe, $AppScript, $AppDir)

# app.py runs `from chanlun...` (its line 13) BEFORE it appends src\ to
# sys.path (its line 16), so the `chanlun` package must already be importable.
# PyCharm injects the source roots automatically; when launching python
# directly we must do the same via PYTHONPATH, otherwise app.py dies with
# ModuleNotFoundError: No module named 'chanlun'.
$pyPathParts = @($SrcPath, $AppDir)
if ($env:PYTHONPATH) { $pyPathParts += $env:PYTHONPATH }
$env:PYTHONPATH = ($pyPathParts -join ';')
Log ('PYTHONPATH = {0}' -f $env:PYTHONPATH)

$spArgs = @{
    FilePath               = $PythonExe
    ArgumentList           = @($AppScript, 'nobrowser')
    WorkingDirectory       = $AppDir
    RedirectStandardOutput = $outLog
    RedirectStandardError  = $errLog
    WindowStyle            = 'Hidden'
}
Start-Process @spArgs

# --- 6. Verify the project came up ------------------------------------------
Start-Sleep -Seconds 6
$check = Get-WebProcs
if ($check) {
    Log ('web project started PID={0}; open http://127.0.0.1:9900' -f (@($check)[0].ProcessId))
} else {
    Log 'WARNING: web project process not detected; check the web_stderr log'
}
Log '===== daily restart DONE ====='
