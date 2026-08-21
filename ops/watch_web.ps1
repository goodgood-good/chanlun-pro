[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 9900,
    [ValidateRange(2, 300)]
    [int]$PollSeconds = 10,
    [ValidateRange(1, 30)]
    [int]$FailureThreshold = 3,
    [ValidateRange(1, 60)]
    [int]$ReadinessFailureThreshold = 6,
    [ValidateRange(5, 120)]
    [int]$LivenessTimeoutSeconds = 15,
    [ValidateRange(5, 120)]
    [int]$ReadinessTimeoutSeconds = 15,
    [ValidateRange(1, 120)]
    [int]$StartupReadinessFailureThreshold = 24,
    [ValidateRange(10, 3600)]
    [int]$RestartCooldownSeconds = 60,
    [ValidateSet('a')]
    [string]$ReadinessMarket = 'a',
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$restartScript = Join-Path $ProjectRoot 'ops\restart_web.ps1'
$stateRoot = Join-Path $ProjectRoot '.cache\chanlun_web_watchdog'
$logRoot = Join-Path $ProjectRoot 'ops\logs'
$heartbeatPath = Join-Path $stateRoot 'heartbeat.json'
$lockPath = Join-Path $stateRoot 'watchdog.lock'
$liveUri = 'http://127.0.0.1:{0}/livez' -f $WebPort
$healthUri = 'http://127.0.0.1:{0}/readyz?market={1}' -f `
    $WebPort, [Uri]::EscapeDataString($ReadinessMarket)

foreach ($directory in @($stateRoot, $logRoot)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}
if (-not (Test-Path -LiteralPath $restartScript -PathType Leaf)) {
    throw "restart script is unavailable: $restartScript"
}

function Write-WatchdogLog([string]$Message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    $path = Join-Path $logRoot ('web_watchdog_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
    Add-Content -LiteralPath $path -Value $line -Encoding UTF8
}

function Write-WatchdogHeartbeat {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][int]$ConsecutiveFailures,
        [AllowNull()][string]$Detail,
        [bool]$RecoveryRecommended = $false,
        [AllowNull()]$AppPid,
        [AllowNull()]$RealtimeSessionOpen,
        [AllowNull()]$PriorityMonitorStatus,
        [AllowNull()]$PriorityMonitorAgeSeconds,
        [AllowNull()]$CandidateMonitorStatus,
        [AllowNull()]$RealtimeAlertStatus
    )
    $payload = [ordered]@{
        schema = 'chanlun-web-watchdog-heartbeat'
        observed_at = (Get-Date).ToString('o')
        watchdog_pid = $PID
        web_port = $WebPort
        live_uri = $liveUri
        health_uri = $healthUri
        status = $Status
        consecutive_failures = $ConsecutiveFailures
        recovery_recommended = $RecoveryRecommended
        app_pid = $AppPid
        realtime_session_open = $RealtimeSessionOpen
        priority_monitor_status = $PriorityMonitorStatus
        priority_monitor_age_seconds = $PriorityMonitorAgeSeconds
        candidate_monitor_status = $CandidateMonitorStatus
        realtime_alert_status = $RealtimeAlertStatus
        detail = $Detail
    }
    $temporary = '{0}.{1}.tmp' -f $heartbeatPath, $PID
    [IO.File]::WriteAllText(
        $temporary,
        (($payload | ConvertTo-Json -Depth 4 -Compress) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
    Move-Item -LiteralPath $temporary -Destination $heartbeatPath -Force
}

function Get-JsonHttpResponse {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    # Windows PowerShell's Invoke-RestMethod throws before returning the JSON
    # body for HTTP 503.  /readyz deliberately uses 503 for structured business
    # readiness states (candidate warm-up, snapshot rebuild, etc.); discarding
    # that body turns every normal warm-up into an indistinguishable transport
    # failure and creates a destructive restart loop.
    $request = [Net.HttpWebRequest]::Create($Uri)
    $request.Method = 'GET'
    $request.Timeout = $TimeoutSeconds * 1000
    $request.ReadWriteTimeout = $TimeoutSeconds * 1000
    $response = $null
    try {
        try {
            $response = $request.GetResponse()
        } catch [Net.WebException] {
            if ($null -eq $_.Exception.Response) {
                throw
            }
            $response = $_.Exception.Response
        }
        $stream = $response.GetResponseStream()
        if ($null -eq $stream) {
            throw 'HTTP response has no body'
        }
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
        try {
            $body = $reader.ReadToEnd()
        } finally {
            $reader.Dispose()
        }
        if ([string]::IsNullOrWhiteSpace($body)) {
            throw 'HTTP response body is empty'
        }
        return ($body | ConvertFrom-Json)
    } finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
    }
}

function Test-WebHealth {
    try {
        # A liveness route is logically cheap, but it shares the Web event loop.
        # A CPU-bound strict-chart response can therefore delay even this probe;
        # use bounded host-load tolerance so a live process is not killed merely
        # because the request waited behind useful work.
        $live = Invoke-RestMethod `
            -Uri $liveUri `
            -Method Get `
            -TimeoutSec $LivenessTimeoutSeconds
        if ($live.status -ne 'alive') {
            return [pscustomobject]@{
                Healthy = $false
                FailureClass = 'liveness_failed'
                RecoveryRecommended = $true
                Detail = 'live endpoint returned a non-alive status'
                AppPid = $live.pid
                RealtimeSessionOpen = $null
                PriorityMonitorStatus = $null
                PriorityMonitorAgeSeconds = $null
                CandidateMonitorStatus = $null
                RealtimeAlertStatus = $null
            }
        }
    } catch {
        return [pscustomobject]@{
            Healthy = $false
            FailureClass = 'liveness_failed'
            RecoveryRecommended = $true
            Detail = $_.Exception.Message
            AppPid = $null
            RealtimeSessionOpen = $null
            PriorityMonitorStatus = $null
            PriorityMonitorAgeSeconds = $null
            CandidateMonitorStatus = $null
            RealtimeAlertStatus = $null
        }
    }

    try {
        # The endpoint returns a cached proof but can still wait briefly for an
        # HTTP worker while strict chart serialization is CPU-bound.  Five
        # seconds caused a healthy live process to be killed during a measured
        # 6-9 second burst; a bounded 15 second probe preserves recovery for a
        # real failure without turning normal computation into a restart loop.
        $health = Get-JsonHttpResponse `
            -Uri $healthUri `
            -TimeoutSeconds $ReadinessTimeoutSeconds
    } catch {
        return [pscustomobject]@{
            Healthy = $false
            FailureClass = 'readiness_failed'
            RecoveryRecommended = $true
            Detail = ('ready endpoint failed: {0}' -f $_.Exception.Message)
            AppPid = $live.pid
            RealtimeSessionOpen = $null
            PriorityMonitorStatus = $null
            PriorityMonitorAgeSeconds = $null
            CandidateMonitorStatus = $null
            RealtimeAlertStatus = $null
        }
    }

    $startupFailures = [Collections.Generic.List[string]]::new()
    $recoverableFailures = [Collections.Generic.List[string]]::new()
    $configurationFailures = [Collections.Generic.List[string]]::new()
    $operationalFailures = [Collections.Generic.List[string]]::new()
    $components = $health.components
    $screening = $components.trading_screening
    $nativeGateway = $screening.native_gateway
    $notification = $screening.notification_delivery

    if ($components.scheduler.ready -ne $true) {
        $recoverableFailures.Add('scheduler_not_ready')
    }
    if ($components.runtime.ready -ne $true) {
        $recoverableFailures.Add('background_runtime_not_ready')
    }
    if ($components.qmt_runtime.ready -ne $true) {
        $recoverableFailures.Add('qmt_runtime_not_ready')
    }
    if ($components.ticks.ready -ne $true) {
        $recoverableFailures.Add('ticks_not_ready')
    }
    if ($screening.worker_alive -ne $true) {
        $recoverableFailures.Add('trading_screening_worker_not_alive')
    } elseif (
        $null -ne $screening.heartbeat_max_age_seconds -and
        $null -ne $screening.heartbeat_age_seconds -and
        [double]$screening.heartbeat_age_seconds -gt
            [double]$screening.heartbeat_max_age_seconds
    ) {
        $recoverableFailures.Add('trading_screening_heartbeat_stale')
    }
    if ($nativeGateway.ready -ne $true) {
        $recoverableFailures.Add('native_gateway_not_ready')
    }
    if ($nativeGateway.market_data_probe.ready -ne $true) {
        $recoverableFailures.Add('native_market_data_probe_not_ready')
    }

    $notificationBlocksRealtimeAlert = $false
    if ($screening.notification_dispatcher_configured -ne $true) {
        $configurationFailures.Add('notification_dispatcher_not_configured')
        $notificationBlocksRealtimeAlert = $true
    } elseif ($notification.configured -ne $true) {
        $configurationFailures.Add('notification_delivery_not_configured')
        $notificationBlocksRealtimeAlert = $true
    } else {
        $hasOutboxWorkerHealth = (
            $notification.PSObject.Properties.Name -contains 'outbox_worker_alive'
        )
        if ($hasOutboxWorkerHealth -and $notification.outbox_worker_alive -ne $true) {
            $recoverableFailures.Add('outbox_worker_not_alive')
            $notificationBlocksRealtimeAlert = $true
        }
        if ($notification.status -in @('degraded', 'unavailable')) {
            # Transport credentials and remote endpoint failures are visible but a
            # Web restart cannot repair them, so do not create a restart loop.
            $configurationFailures.Add(
                'notification_delivery_{0}' -f [string]$notification.status
            )
            $notificationBlocksRealtimeAlert = $true
        }
    }

    $realtimeSessionOpen = $screening.priority_monitor_session_open -eq $true
    $priorityMonitorStatus = [string]$screening.priority_monitor_status
    $candidateMonitorStatus = [string]$screening.candidate_monitor_status
    $realtimeAlertStatus = [string]$screening.realtime_alert_status
    $priorityMonitorStarting = $priorityMonitorStatus -in @(
        'awaiting_runtime_verification',
        'awaiting_first_run',
        'starting',
        'warming'
    )
    $priorityMonitorOperationallyDegraded = $priorityMonitorStatus -in @(
        'degraded',
        'clock_regressed'
    )
    if (
        $screening.runtime_ready -ne $true -and
        $screening.worker_alive -eq $true -and
        $nativeGateway.ready -eq $true
    ) {
        # A missing/rebuilding selection snapshot makes /readyz return 503 but
        # does not mean the process runtime is dead.  The background worker owns
        # retry/checkpoint recovery; restarting it discards that progress.  The
        # live priority lane is assessed independently below when the session is
        # open, so this remains visible without recommending a restart.
        $runtimeStatus = [string]$screening.runtime_status
        if ([string]::IsNullOrWhiteSpace($runtimeStatus)) {
            $runtimeStatus = 'not_ready'
        }
        $operationalFailures.Add(
            'trading_screening_{0}' -f $runtimeStatus
        )
    }
    if ($realtimeSessionOpen -and $screening.priority_monitor_ready -ne $true) {
        if ($priorityMonitorStarting) {
            # A current-process attestation can legitimately take almost the full
            # 50-second monitor budget.  Classify it as startup so it receives the
            # bounded startup threshold instead of being killed at the ordinary
            # 60-second readiness threshold and starting the same work again.
            $startupFailures.Add('priority_monitor_starting')
        } elseif ($priorityMonitorOperationallyDegraded) {
            # A completed round can reject an individual stale/malformed market
            # fact while the service, native gateway and retry scheduler remain
            # healthy.  A Web restart cannot refresh that external fact and would
            # discard useful caches, so retain the explicit degraded heartbeat.
            $operationalFailures.Add(
                'priority_monitor_{0}' -f $priorityMonitorStatus
            )
        } else {
            $recoverableFailures.Add('priority_monitor_not_ready')
        }
    }
    if (
        $realtimeSessionOpen -and
        $screening.realtime_alert_ready -ne $true -and
        -not $notificationBlocksRealtimeAlert
    ) {
        if (
            $realtimeAlertStatus -eq 'candidate_monitor_degraded' -and
            $screening.priority_monitor_ready -eq $true
        ) {
            # Candidate discovery cadence is an operational SLO.  Restarting the
            # Web process discards its warm caches and can only make that backlog
            # worse, so expose the degradation without recommending recovery.
            $candidateReason = if ([string]::IsNullOrWhiteSpace($candidateMonitorStatus)) {
                'candidate_monitor_not_ready'
            } else {
                'candidate_monitor_{0}' -f $candidateMonitorStatus
            }
            $operationalFailures.Add($candidateReason)
        } elseif (
            $realtimeAlertStatus -eq 'priority_monitor_degraded' -and
            $screening.priority_monitor_ready -ne $true
        ) {
            # The priority lane has already been classified above, including the
            # longer current-process startup attestation threshold.
        } else {
            $recoverableFailures.Add('realtime_alert_not_ready')
        }
    }

    $allFailures = @($startupFailures) + @($recoverableFailures) + `
        @($configurationFailures) + @($operationalFailures)
    $detail = if ($allFailures.Count -gt 0) {
        $allFailures -join ','
    } else {
        'pid={0}; revision={1}; priority_age_seconds={2}; candidate_status={3}' -f `
            $health.pid,
            $health.revision,
            $screening.priority_monitor_age_seconds,
            $screening.candidate_monitor_status
    }
    if ($allFailures.Count -gt 0) {
        $failureClass = if (
            $startupFailures.Count -gt 0 -or
            $recoverableFailures.Contains('qmt_runtime_not_ready')
        ) {
            'startup_readiness_failed'
        } elseif ($recoverableFailures.Count -gt 0) {
            'readiness_failed'
        } elseif ($configurationFailures.Count -gt 0) {
            'configuration_failed'
        } else {
            'operational_degraded'
        }
        return [pscustomobject]@{
            Healthy = $false
            FailureClass = $failureClass
            RecoveryRecommended = (
                $startupFailures.Count -gt 0 -or $recoverableFailures.Count -gt 0
            )
            Detail = $detail
            AppPid = $health.pid
            RealtimeSessionOpen = $realtimeSessionOpen
            PriorityMonitorStatus = $priorityMonitorStatus
            PriorityMonitorAgeSeconds = $screening.priority_monitor_age_seconds
            CandidateMonitorStatus = $candidateMonitorStatus
            RealtimeAlertStatus = $realtimeAlertStatus
        }
    }

    return [pscustomobject]@{
        Healthy = $true
        FailureClass = 'healthy'
        RecoveryRecommended = $false
        Detail = $detail
        AppPid = $health.pid
        RealtimeSessionOpen = $realtimeSessionOpen
        PriorityMonitorStatus = $priorityMonitorStatus
        PriorityMonitorAgeSeconds = $screening.priority_monitor_age_seconds
        CandidateMonitorStatus = $candidateMonitorStatus
        RealtimeAlertStatus = $realtimeAlertStatus
    }
}

function Invoke-WebRecovery {
    Write-WatchdogLog ('recovery requested after health failure: {0}' -f $healthUri)
    # Do not capture restart output here. A long-running Web descendant can keep
    # the capture pipe open after the restart script exits and deadlock recovery.
    # The deployment has its own log; wait only for the direct child exit code.
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        ('"{0}"' -f $restartScript),
        '-SkipWatchdog',
        '-WebReadinessTimeoutSeconds',
        '1800'
    )
    $restartProcess = Start-Process `
        -FilePath 'powershell.exe' `
        -ArgumentList $arguments `
        -WindowStyle Hidden `
        -PassThru
    $restartProcess.WaitForExit()
    $exitCode = $restartProcess.ExitCode
    if ($exitCode -ne 0) {
        Write-WatchdogLog ('recovery failed with exit code {0}' -f $exitCode)
        return $false
    }
    Write-WatchdogLog 'recovery completed successfully'
    return $true
}

$lockStream = $null
try {
    $lockStream = [IO.File]::Open(
        $lockPath,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
} catch {
    # The file handle provides cross-session exclusion for this project watcher.
    exit 0
}
if ($null -eq $lockStream) {
    exit 0
}
$lockPayload = [Text.Encoding]::UTF8.GetBytes(
    ('pid={0}; port={1}; started_at={2}' -f $PID, $WebPort, (Get-Date).ToString('o'))
)
$lockStream.SetLength(0)
$lockStream.Write($lockPayload, 0, $lockPayload.Length)
$lockStream.Flush()

try {
    Write-WatchdogLog (
        'watchdog started; pid={0}; live_uri={1}; health_uri={2}' -f `
            $PID, $liveUri, $healthUri
    )
    $consecutiveFailures = 0
    $lastFailureClass = $null
    $lastObservedAppPid = $null
    $lastRestartAt = [datetime]::MinValue
    while ($true) {
        $probe = Test-WebHealth
        $observedAppPid = [string]$probe.AppPid
        if (
            -not [string]::IsNullOrWhiteSpace($observedAppPid) -and
            -not [string]::IsNullOrWhiteSpace([string]$lastObservedAppPid) -and
            $observedAppPid -ne [string]$lastObservedAppPid
        ) {
            # A replacement process must receive its own complete startup budget;
            # failures accumulated by the prior PID cannot be inherited.
            Write-WatchdogLog (
                'application PID changed {0} -> {1}; reset failure budget' -f `
                    $lastObservedAppPid, $observedAppPid
            )
            $consecutiveFailures = 0
            $lastFailureClass = $null
        }
        if (-not [string]::IsNullOrWhiteSpace($observedAppPid)) {
            $lastObservedAppPid = $observedAppPid
        }
        if ($probe.Healthy) {
            $consecutiveFailures = 0
            $lastFailureClass = $null
            Write-WatchdogHeartbeat `
                -Status 'healthy' `
                -ConsecutiveFailures 0 `
                -Detail $probe.Detail `
                -AppPid $probe.AppPid `
                -RealtimeSessionOpen $probe.RealtimeSessionOpen `
                -PriorityMonitorStatus $probe.PriorityMonitorStatus `
                -PriorityMonitorAgeSeconds $probe.PriorityMonitorAgeSeconds `
                -CandidateMonitorStatus $probe.CandidateMonitorStatus `
                -RealtimeAlertStatus $probe.RealtimeAlertStatus
        } else {
            if ($lastFailureClass -ne $probe.FailureClass) {
                $consecutiveFailures = 0
                $lastFailureClass = $probe.FailureClass
            }
            $consecutiveFailures += 1
            Write-WatchdogHeartbeat `
                -Status $probe.FailureClass `
                -ConsecutiveFailures $consecutiveFailures `
                -Detail $probe.Detail `
                -RecoveryRecommended $probe.RecoveryRecommended `
                -AppPid $probe.AppPid `
                -RealtimeSessionOpen $probe.RealtimeSessionOpen `
                -PriorityMonitorStatus $probe.PriorityMonitorStatus `
                -PriorityMonitorAgeSeconds $probe.PriorityMonitorAgeSeconds `
                -CandidateMonitorStatus $probe.CandidateMonitorStatus `
                -RealtimeAlertStatus $probe.RealtimeAlertStatus
            $threshold = switch ($probe.FailureClass) {
                'liveness_failed' { $FailureThreshold; break }
                'startup_readiness_failed' {
                    $StartupReadinessFailureThreshold
                    break
                }
                default { $ReadinessFailureThreshold; break }
            }
            if (
                $probe.RecoveryRecommended -and
                $consecutiveFailures -ge $threshold
            ) {
                $cooldownElapsed = ((Get-Date) - $lastRestartAt).TotalSeconds
                if ($cooldownElapsed -ge $RestartCooldownSeconds) {
                    $lastRestartAt = Get-Date
                    Write-WatchdogHeartbeat `
                        -Status 'recovering' `
                        -ConsecutiveFailures $consecutiveFailures `
                        -Detail $probe.Detail `
                        -RecoveryRecommended $true `
                        -AppPid $probe.AppPid `
                        -RealtimeSessionOpen $probe.RealtimeSessionOpen `
                        -PriorityMonitorStatus $probe.PriorityMonitorStatus `
                        -PriorityMonitorAgeSeconds $probe.PriorityMonitorAgeSeconds `
                        -CandidateMonitorStatus $probe.CandidateMonitorStatus `
                        -RealtimeAlertStatus $probe.RealtimeAlertStatus
                    $recovered = Invoke-WebRecovery
                    $consecutiveFailures = 0
                    $recoveryStatus = if ($recovered) {
                        'recovered'
                    } else {
                        'recovery_failed'
                    }
                    Write-WatchdogHeartbeat `
                        -Status $recoveryStatus `
                        -ConsecutiveFailures 0 `
                        -Detail $probe.Detail `
                        -RecoveryRecommended (-not $recovered) `
                        -AppPid $probe.AppPid `
                        -RealtimeSessionOpen $probe.RealtimeSessionOpen `
                        -PriorityMonitorStatus $probe.PriorityMonitorStatus `
                        -PriorityMonitorAgeSeconds $probe.PriorityMonitorAgeSeconds `
                        -CandidateMonitorStatus $probe.CandidateMonitorStatus `
                        -RealtimeAlertStatus $probe.RealtimeAlertStatus
                }
            }
        }
        if ($Once) {
            if ($probe.Healthy) {
                exit 0
            }
            exit 1
        }
        Start-Sleep -Seconds $PollSeconds
    }
} finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
