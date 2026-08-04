# Frozen V3 human-review runner.  It never starts QMT, opens an account, builds
# a replay order, or sends an order.  QMT is used only through its read-only
# local data directory (and the Python capture tool's market-data RPC
# attempt/fallback).
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Capture', 'Evaluate', 'Status')]
    [string]$Phase,
    [string]$Session = '',
    [string]$PythonExe = '',
    [ValidateRange(0, 720)]
    [int]$CoverageWaitMinutes = 460,
    [ValidateRange(5, 300)]
    [int]$CoveragePollSeconds = 60,
    [ValidateRange(1, 20)]
    [int]$DataGateRetryCount = 5,
    [ValidateRange(1, 60)]
    [int]$DataGateRetryDelaySeconds = 30
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = if ($env:CHANLUN_FORWARD_PYTHON) {
        $env:CHANLUN_FORWARD_PYTHON
    } elseif ($env:CHANLUN_PYTHON) {
        $env:CHANLUN_PYTHON
    } else {
        (Get-Command python -ErrorAction Stop).Source
    }
}
try {
    $PythonExe = [IO.Path]::GetFullPath($PythonExe)
} catch {
    throw "Forward-paper Python path is invalid: $PythonExe"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Forward-paper Python executable not found: $PythonExe"
}
$Tool = Join-Path $ProjectRoot 'tools\run_v3_forward_paper.py'
$LogDir = Join-Path $PSScriptRoot 'logs'
$null = New-Item -ItemType Directory -Path $LogDir -Force
$LogPath = Join-Path $LogDir ('forward_paper_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
$InvocationId = [Guid]::NewGuid().ToString('N')

function Write-ForwardLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    # A scheduled-task retry is a new PowerShell process.  Keep its identity on
    # every line so an interrupted invocation and a later successful recovery
    # cannot be mistaken for one continuous run.  This is provenance only; it
    # never enters a strategy parameter or a decision snapshot.
    $line = '{0} [{1}] [run={2} pid={3}] {4}' -f (
        Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    ), $Phase, $InvocationId, $PID, $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Test-AppForwardRuntimeOwner {
    # During migration the old scheduled tasks may still exist briefly.  A
    # fresh, live app.py owner receipt is an explicit fail-closed hand-off: the
    # Windows process must not run the same Capture/Evaluate phase in parallel.
    $ownerPath = Join-Path $ProjectRoot '.cache\chanlun_v3_scheduler\forward_execution_owner.json'
    if (-not (Test-Path -LiteralPath $ownerPath -PathType Leaf)) {
        return $false
    }
    try {
        $owner = Get-Content -LiteralPath $ownerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $expectedEntry = [IO.Path]::GetFullPath(
            (Join-Path $ProjectRoot 'web\chanlun_chart\app.py')
        )
        $ownerRoot = [IO.Path]::GetFullPath([string]$owner.project_root)
        $ownerEntry = [IO.Path]::GetFullPath([string]$owner.application_entrypoint)
        $heartbeat = [DateTimeOffset]::Parse(
            [string]$owner.heartbeat_at,
            [Globalization.CultureInfo]::InvariantCulture
        )
        # Keep the hand-off valid for the controller's maximum two-hour
        # Evaluate child plus shutdown margin.  The read-only app audit uses a
        # much tighter 15-minute heartbeat SLO; this wider guard exists only to
        # prevent overlap during the short migration period.
        $fresh = ([DateTimeOffset]::Now - $heartbeat).Duration().TotalMinutes -le 180
        $ownerPid = [int]$owner.pid
        $process = Get-Process -Id $ownerPid -ErrorAction Stop
        return (
            $owner.schema -eq 'chanlun-v3-forward-execution-owner/v1' -and
            $owner.owner -eq 'APP_RUNTIME' -and
            $ownerRoot -eq [IO.Path]::GetFullPath($ProjectRoot) -and
            $ownerEntry -eq $expectedEntry -and
            $fresh -and
            $process.Id -eq $ownerPid -and
            $owner.real_account_accessed -eq $false -and
            $owner.real_order_transport_enabled -eq $false -and
            $owner.automated_order_authorized -eq $false -and
            $owner.live_status -eq 'LIVE_DISABLED'
        )
    } catch {
        # A stale/corrupt receipt does not prove ownership.  Continue to the
        # existing mutex and evidence-ledger gates instead of trusting it.
        return $false
    }
}

function Get-ForwardCoverageProbe {
    param([Parameter(Mandatory = $true)][DateTimeOffset]$ExpectedClose)

    $port = if ($env:CHANLUN_WEB_PORT) { [int]$env:CHANLUN_WEB_PORT } else { 9900 }
    $forwardSession = $ExpectedClose.ToString(
        'yyyy-MM-dd',
        [Globalization.CultureInfo]::InvariantCulture
    )
    $uri = 'http://127.0.0.1:{0}/readyz?market=a&forward_session={1}' -f (
        $port,
        [Uri]::EscapeDataString($forwardSession)
    )
    try {
        $ready = Invoke-RestMethod -Uri $uri -TimeoutSec 20 -ErrorAction Stop
        $forwardArchive = $ready.components.forward_archive
        $forwardArchiveReady = $forwardArchive.ready -eq $true
        $forwardArchiveReason = [string]$forwardArchive.reason_code
        $forwardDelivery = $ready.components.forward_delivery
        $forwardDeliveryRequired = $forwardDelivery.required
        $forwardDeliveryRequirementResolved = (
            $forwardDelivery.requirement_resolved -eq $true
        )
        $forwardDeliveryReason = [string]$forwardDelivery.reason_code
        # Resolve terminal delivery states before touching screening fields.
        # On a clean holiday startup there may be no market_data_as_of at all;
        # that absence must not hide an exact official-calendar no-sample day.
        $nonTradingNoSample = (
            $forwardDeliveryRequirementResolved -and
            $forwardDeliveryRequired -eq $false -and
            $forwardDeliveryReason -eq 'NON_TRADING_SESSION_NOT_DUE'
        )
        if ($nonTradingNoSample) {
            return [pscustomobject]@{
                Ready = $false
                Disposition = 'NO_SAMPLE'
                Pending = -1
                AsOf = $null
                ForwardArchiveReason = $forwardArchiveReason
                ForwardDeliveryReason = $forwardDeliveryReason
                Reason = 'non_trading_session_not_due'
            }
        }
        # Capture and Evaluate are separate scheduled processes.  If any
        # executable source changes between them, continuing would create a
        # terminal event that the next-day qualification audit must discard.
        # Stop before market-data reads; a bounded task retry can recover after
        # the captured source state is restored.
        $implementationContinuityBlocked = (
            $forwardDeliveryRequirementResolved -and
            $forwardDeliveryRequired -eq $true -and
            $forwardDelivery.capture_event_present -eq $true -and
            $forwardDelivery.evaluation_event_present -ne $true -and
            $forwardDeliveryReason -in @(
                'CAPTURE_IMPLEMENTATION_PROVENANCE_UNATTESTED',
                'IMPLEMENTATION_CHANGED_SINCE_CAPTURE',
                'CURRENT_IMPLEMENTATION_PROVENANCE_UNAVAILABLE'
            )
        )
        if ($implementationContinuityBlocked) {
            return [pscustomobject]@{
                Ready = $false
                Disposition = 'BLOCKED'
                Pending = -1
                AsOf = $null
                ForwardArchiveReason = $forwardArchiveReason
                ForwardDeliveryReason = $forwardDeliveryReason
                Reason = 'implementation_continuity_blocked'
            }
        }
        # A same-session QMT Capture remains recoverable during the day, but
        # after 15:00 it cannot be recreated without prohibited hindsight.
        # Evaluation starts at 15:20, so fail that day immediately and visibly
        # even if the screening component itself is unavailable.
        $captureIrrecoverable = (
            $forwardDeliveryRequirementResolved -and
            $forwardDeliveryRequired -eq $true -and
            $forwardDeliveryReason -eq 'CAPTURE_MISSING_AFTER_DUE' -and
            [DateTimeOffset]::Now -ge $ExpectedClose
        )
        if ($captureIrrecoverable) {
            return [pscustomobject]@{
                Ready = $false
                Disposition = 'BLOCKED'
                Pending = -1
                AsOf = $null
                ForwardArchiveReason = $forwardArchiveReason
                ForwardDeliveryReason = $forwardDeliveryReason
                Reason = 'capture_missing_after_close'
            }
        }
        $screening = $ready.components.trading_screening
        # The decision snapshot clock and the last completed market-data clock
        # are separate concepts.  Only the latter may satisfy the close gate;
        # using the generic ``as_of`` field here could start evaluation while
        # QMT is still materialising the final 1m/5m bars.
        $marketDataAsOf = [string]$screening.market_data_as_of
        if ([string]::IsNullOrWhiteSpace($marketDataAsOf)) {
            throw 'trading screening market_data_as_of is unavailable'
        }
        $cutoff = [DateTimeOffset]::Parse(
            $marketDataAsOf,
            [Globalization.CultureInfo]::InvariantCulture
        )
        $pending = [int]$screening.pending_symbol_count
        $complete = $screening.coverage_cycle_complete -eq $true
        $cutoffReady = (
            $cutoff.Date -eq $ExpectedClose.Date -and
            $cutoff -ge $ExpectedClose
        )
        $readyForEvaluation = (
            $ready.status -eq 'ready' -and
            $screening.ready -eq $true -and
            $complete -and
            $pending -eq 0 -and
            $forwardArchiveReady -and
            $cutoffReady
        )
        return [pscustomobject]@{
            Ready = $readyForEvaluation
            Disposition = if ($readyForEvaluation) {
                'READY'
            } else {
                'WAIT'
            }
            Pending = $pending
            AsOf = $cutoff.ToString('o')
            ForwardArchiveReason = $forwardArchiveReason
            ForwardDeliveryReason = $forwardDeliveryReason
            Reason = if (-not $cutoffReady) {
                'market_close_pending'
            } elseif (-not $complete -or $pending -ne 0) {
                'coverage_pending'
            } elseif (-not $forwardArchiveReady) {
                'forward_archive_pending'
            } else {
                'ready'
            }
        }
    } catch {
        return [pscustomobject]@{
            Ready = $false
            Disposition = 'WAIT'
            Pending = -1
            AsOf = $null
            ForwardArchiveReason = $null
            ForwardDeliveryReason = $null
            Reason = 'readiness_unavailable'
        }
    }
}

function Resolve-QmtDataDirectory {
    if ($env:CHANLUN_QMT_LOCAL_DATA_DIR) {
        $candidate = [IO.Path]::GetFullPath($env:CHANLUN_QMT_LOCAL_DATA_DIR)
        if (Test-Path -LiteralPath (Join-Path $candidate 'Sector\Temple\GICS') -PathType Container) {
            return $candidate
        }
        throw "CHANLUN_QMT_LOCAL_DATA_DIR is not a QMT data directory: $candidate"
    }
    $matches = @(
        Get-ChildItem 'D:\software' -Directory -ErrorAction Stop | ForEach-Object {
            $candidate = Join-Path $_.FullName 'userdata_mini\datadir'
            if (Test-Path -LiteralPath (Join-Path $candidate 'Sector\Temple\GICS') -PathType Container) {
                $candidate
            }
        }
    )
    if ($matches.Count -ne 1) {
        throw "Expected exactly one local QMT data directory, found $($matches.Count)"
    }
    return [IO.Path]::GetFullPath($matches[0])
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $Tool -PathType Leaf)) {
    throw "Forward-paper tool not found: $Tool"
}

$sessionLabel = if ($Session) { $Session } else { 'AUTO_CURRENT_SESSION' }
Write-ForwardLog ((
        "invocation started session={0} coverage_wait_minutes={1} " +
        "coverage_poll_seconds={2} data_gate_attempts={3} data_gate_delay_seconds={4} " +
        "python={5}"
    ) -f $sessionLabel, $CoverageWaitMinutes, $CoveragePollSeconds, $DataGateRetryCount, $DataGateRetryDelaySeconds, $PythonExe)

if (Test-AppForwardRuntimeOwner) {
    Write-ForwardLog 'APP_RUNTIME_OWNS_FORWARD; legacy Windows phase refused to prevent duplicate Capture/Evaluate execution; unregister the two Chanlun-V3-Forward-* tasks after app readiness is verified'
    exit 76
}

$mutex = New-Object Threading.Mutex($false, 'Local\ChanlunV3ForwardPaperDaily')
$acquired = $false
try {
    $acquired = $mutex.WaitOne(0)
    if (-not $acquired) {
        # The mutex is shared across Capture/Evaluate/Status, not merely
        # duplicate invocations of one phase.  Reporting contention as success
        # suppresses Task Scheduler recovery and can silently lose a required
        # daily Capture or Evaluate sample.  Exit 75 (temporary failure) so the
        # registered bounded restart policy can retry, while the invocation log
        # remains an explicit no-sample audit trail.
        Write-ForwardLog 'NO_SAMPLE_PHASE_CONCURRENCY_BLOCKED; another forward-paper phase owns the global mutex; temporary failure permits scheduled retry; no review pipeline or order transport ran'
        exit 75
    }
    $qmtData = Resolve-QmtDataDirectory
    $arguments = @($Tool, '--qmt-local-data-dir', $qmtData)
    if ($Session) { $arguments += @('--session', $Session) }
    switch ($Phase) {
        'Capture' { $arguments += @('capture', '--source', 'auto') }
        'Evaluate' { $arguments += 'evaluate' }
        'Status' { $arguments += 'status' }
    }
    if ($Phase -eq 'Evaluate' -and $CoverageWaitMinutes -gt 0) {
        $sessionText = if ($Session) { $Session } else { Get-Date -Format 'yyyy-MM-dd' }
        $expectedClose = [DateTimeOffset]::ParseExact(
            ('{0} 15:00:00 +08:00' -f $sessionText),
            'yyyy-MM-dd HH:mm:ss zzz',
            [Globalization.CultureInfo]::InvariantCulture
        )
        $deadline = (Get-Date).AddMinutes($CoverageWaitMinutes)
        $lastProbeState = ''
        while ($true) {
            $probe = Get-ForwardCoverageProbe -ExpectedClose $expectedClose
            $probeState = '{0}|{1}|{2}|{3}|{4}|{5}|{6}' -f $probe.Ready, $probe.Disposition, $probe.Pending, $probe.AsOf, $probe.Reason, $probe.ForwardArchiveReason, $probe.ForwardDeliveryReason
            if ($probeState -ne $lastProbeState) {
                Write-ForwardLog ("coverage wait ready={0} disposition={1} pending={2} as_of={3} reason={4} forward_archive_reason={5} forward_delivery_reason={6}" -f $probe.Ready, $probe.Disposition, $probe.Pending, $probe.AsOf, $probe.Reason, $probe.ForwardArchiveReason, $probe.ForwardDeliveryReason)
                $lastProbeState = $probeState
            }
            if ($probe.Disposition -eq 'NO_SAMPLE') {
                Write-ForwardLog 'NO_SAMPLE_NON_TRADING_SESSION; official calendar proves no forward sample is due; no review pipeline or order transport ran'
                exit 0
            }
            if ($probe.Disposition -eq 'BLOCKED') {
                Write-ForwardLog ("NO_SAMPLE_DELIVERY_BLOCKED reason={0} forward_delivery_reason={1}; no review pipeline or order transport ran" -f $probe.Reason, $probe.ForwardDeliveryReason)
                exit 4
            }
            if ($probe.Ready) { break }
            if ((Get-Date) -ge $deadline) {
                Write-ForwardLog ("NO_SAMPLE_COVERAGE_BLOCKED after {0} minute(s); no review pipeline or order transport ran" -f $CoverageWaitMinutes)
                exit 4
            }
            Start-Sleep -Seconds $CoveragePollSeconds
        }
    }
    $maximumAttempts = if ($Phase -eq 'Evaluate') { $DataGateRetryCount } else { 1 }
    $attempt = 0
    do {
        $attempt += 1
        Write-ForwardLog ("start attempt {0}/{1}: {2} {3}" -f $attempt, $maximumAttempts, $PythonExe, ($arguments -join ' '))
        Push-Location $ProjectRoot
        try {
            # Native applications may legitimately write diagnostics to stderr.
            # Under ErrorActionPreference=Stop PowerShell converts the first such
            # line into a terminating NativeCommandError and loses the remaining
            # traceback.  Collect the complete stream and decide from the native
            # exit code instead.
            $previousErrorActionPreference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                $output = @(& $PythonExe @arguments 2>&1)
                $exitCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }
        } finally {
            Pop-Location
        }
        foreach ($line in $output) {
            $text = [string]$line
            if ([string]::IsNullOrEmpty($text)) { continue }
            Write-ForwardLog $text
        }
        if (
            $Phase -eq 'Evaluate' -and
            $exitCode -eq 3 -and
            $attempt -lt $maximumAttempts
        ) {
            Write-ForwardLog ("NO_SAMPLE_DATA_BLOCKED attempt {0}/{1}; retry in {2}s" -f $attempt, $maximumAttempts, $DataGateRetryDelaySeconds)
            Start-Sleep -Seconds $DataGateRetryDelaySeconds
        }
    } while ($Phase -eq 'Evaluate' -and $exitCode -eq 3 -and $attempt -lt $maximumAttempts)

    if ($Phase -eq 'Evaluate' -and $exitCode -eq 3) {
        # A safe block is not a successful daily sample.  Preserve exit code 3
        # so Task Scheduler and monitoring can distinguish it from EVALUATED.
        Write-ForwardLog ("NO_SAMPLE_DATA_BLOCKED after {0} attempt(s); no review pipeline or order transport ran" -f $attempt)
        exit 3
    }
    if ($exitCode -ne 0) {
        Write-ForwardLog ("phase failed with exit code $exitCode")
        exit $exitCode
    }
    Write-ForwardLog 'phase completed'
    exit 0
} catch {
    Write-ForwardLog ("ERROR: {0}" -f $_.Exception.Message)
    exit 1
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
