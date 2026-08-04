# App-owned QMT runtime helper.  It manages only the explicitly configured QMT
# installation and never starts/stops the chanlun web process.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Status', 'Ensure', 'Restart')]
    [string]$Action,
    [string]$QmtExe = '',
    [ValidateRange(10, 600)]
    [int]$StartupTimeoutSeconds = 120,
    [ValidateRange(0, 1800)]
    [int]$WarmupSeconds = 90,
    [ValidateRange(1, 3650)]
    [int]$LogRetentionDays = 30,
    [ValidateRange(1048576, 10737418240)]
    [long]$LogMaxTotalBytes = 104857600
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PathFile = Join-Path $PSScriptRoot 'qmt_exe_path.txt'
$LogDir = Join-Path $PSScriptRoot 'logs'
$null = New-Item -ItemType Directory -Path $LogDir -Force
$LogPath = Join-Path $LogDir ('qmt_runtime_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
$InvocationId = [Guid]::NewGuid().ToString('N')
$ProcessNames = @('XtItClient', 'XtMiniQmt', 'miniquote')

function Write-QmtLog([string]$Message) {
    $line = '{0} [{1}] [run={2} pid={3}] {4}' -f (
        Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    ), $Action, $InvocationId, $PID, $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Invoke-QmtLogRetention(
    [string]$Directory,
    [string]$CurrentLog,
    [int]$RetentionDays,
    [long]$MaxTotalBytes
) {
    # Delete only this helper's dated logs.  The current log is immutable for
    # the duration of the invocation even when it alone exceeds the cap.
    $currentFullPath = [IO.Path]::GetFullPath($CurrentLog)
    $cutoff = (Get-Date).Date.AddDays(-($RetentionDays - 1))
    $removedCount = 0
    [long]$removedBytes = 0

    $datedLogs = @(
        Get-ChildItem -LiteralPath $Directory -File -Force -ErrorAction Stop |
            Where-Object {
                $_.Name -match '^qmt_runtime_\d{4}-\d{2}-\d{2}\.log$'
            }
    )
    foreach ($log in $datedLogs) {
        if ($log.FullName -ieq $currentFullPath) { continue }
        $match = [regex]::Match(
            $log.Name,
            '^qmt_runtime_(\d{4}-\d{2}-\d{2})\.log$'
        )
        $logDate = [datetime]::MinValue
        $validDate = [datetime]::TryParseExact(
            $match.Groups[1].Value,
            'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None,
            [ref]$logDate
        )
        if ($validDate -and $logDate.Date -lt $cutoff) {
            $removedBytes += [long]$log.Length
            Remove-Item -LiteralPath $log.FullName -Force -ErrorAction Stop
            $removedCount += 1
        }
    }

    $remainingLogs = @(
        Get-ChildItem -LiteralPath $Directory -File -Force -ErrorAction Stop |
            Where-Object {
                $_.Name -match '^qmt_runtime_\d{4}-\d{2}-\d{2}\.log$'
            } |
            Sort-Object Name, LastWriteTime
    )
    [long]$remainingBytes = (
        $remainingLogs | Measure-Object -Property Length -Sum
    ).Sum
    foreach ($log in $remainingLogs) {
        if ($remainingBytes -le $MaxTotalBytes) { break }
        if ($log.FullName -ieq $currentFullPath) { continue }
        $removedBytes += [long]$log.Length
        $remainingBytes -= [long]$log.Length
        Remove-Item -LiteralPath $log.FullName -Force -ErrorAction Stop
        $removedCount += 1
    }

    return [ordered]@{
        retention_days = $RetentionDays
        max_total_bytes = $MaxTotalBytes
        removed_count = $removedCount
        removed_bytes = $removedBytes
        remaining_bytes = $remainingBytes
        current_log_preserved = (Test-Path -LiteralPath $currentFullPath)
    }
}

function Resolve-QmtExecutable {
    if (-not [string]::IsNullOrWhiteSpace($QmtExe)) {
        $candidate = [IO.Path]::GetFullPath($QmtExe)
    } else {
        if (-not (Test-Path -LiteralPath $PathFile -PathType Leaf)) {
            throw "QMT path file is unavailable: $PathFile"
        }
        $candidate = $null
        foreach ($line in (Get-Content -LiteralPath $PathFile -Encoding UTF8)) {
            $value = $line.Trim()
            if ($value -and -not $value.StartsWith('#')) {
                $candidate = [IO.Path]::GetFullPath($value)
                break
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        throw 'QMT executable is not configured'
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "QMT executable does not exist: $candidate"
    }
    if ([IO.Path]::GetFileName($candidate) -ine 'XtItClient.exe') {
        throw "QMT executable must be XtItClient.exe: $candidate"
    }
    return $candidate
}

function Get-TargetProcesses([string]$Directory) {
    @(
        Get-Process -Name $ProcessNames -ErrorAction SilentlyContinue |
            Where-Object {
                try {
                    $_.Path -and (
                        (Split-Path -LiteralPath $_.Path) -ieq $Directory
                    )
                } catch {
                    $false
                }
            }
    )
}

function Get-QmtSnapshot([string]$Executable, [string]$Directory) {
    $processes = @(Get-TargetProcesses -Directory $Directory)
    $main = @($processes | Where-Object { $_.ProcessName -ieq 'XtMiniQmt' })
    $mainStartedAt = $null
    if ($main.Count -eq 1) {
        try {
            $mainStartedAt = $main[0].StartTime.ToString(
                'yyyy-MM-ddTHH:mm:ss.ffffffK',
                [Globalization.CultureInfo]::InvariantCulture
            )
        } catch {
            $mainStartedAt = $null
        }
    }
    $rows = @(
        $processes |
            Sort-Object ProcessName, Id |
            ForEach-Object {
                $startedAt = $null
                try {
                    $startedAt = $_.StartTime.ToString(
                        'yyyy-MM-ddTHH:mm:ss.ffffffK',
                        [Globalization.CultureInfo]::InvariantCulture
                    )
                } catch {
                    $startedAt = $null
                }
                [ordered]@{
                    name = $_.ProcessName
                    pid = [int]$_.Id
                    started_at = $startedAt
                    executable = $_.Path
                }
            }
    )
    $ready = $main.Count -eq 1
    $reason = if ($ready) {
        'READY'
    } elseif ($main.Count -gt 1) {
        'MULTIPLE_QMT_MAIN_PROCESSES'
    } else {
        'QMT_MAIN_PROCESS_MISSING'
    }
    return [ordered]@{
        schema = 'chanlun-qmt-app-runtime-observation/v1'
        observed_at = (Get-Date).ToString(
            'yyyy-MM-ddTHH:mm:ss.ffffffK',
            [Globalization.CultureInfo]::InvariantCulture
        )
        action = $Action.ToUpperInvariant()
        ready = $ready
        status = if ($ready) { 'ready' } else { 'not_ready' }
        reason_code = $reason
        changed = $false
        qmt_executable = $Executable
        qmt_directory = $Directory
        log_retention_days = $LogRetentionDays
        log_max_total_bytes = $LogMaxTotalBytes
        main_process_count = $main.Count
        main_started_at = $mainStartedAt
        processes = $rows
        process_count = $rows.Count
        error = $null
        real_account_accessed = $false
        real_order_transport_enabled = $false
        automated_order_authorized = $false
        live_status = 'LIVE_DISABLED'
    }
}

function Write-Observation([Collections.IDictionary]$Observation, [int]$ExitCode) {
    $Observation | ConvertTo-Json -Depth 8 -Compress
    exit $ExitCode
}

$resolvedExe = $null
$qmtDir = $null
$mutex = $null
$acquired = $false
try {
    $resolvedExe = Resolve-QmtExecutable
    $qmtDir = Split-Path -LiteralPath $resolvedExe
    $identityBytes = [Text.Encoding]::UTF8.GetBytes(
        [IO.Path]::GetFullPath($qmtDir).ToUpperInvariant()
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($identityBytes)
    } finally {
        $sha.Dispose()
    }
    $token = ([BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 32)
    $mutex = New-Object Threading.Mutex($false, "Local\ChanlunQmtRuntime_$token")
    try {
        $acquired = $mutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $acquired = $true
    }
    if (-not $acquired) {
        $busy = Get-QmtSnapshot -Executable $resolvedExe -Directory $qmtDir
        $busy.ready = $false
        $busy.status = 'not_ready'
        $busy.reason_code = 'QMT_RUNTIME_OPERATION_IN_PROGRESS'
        Write-Observation -Observation $busy -ExitCode 75
    }

    try {
        $retention = Invoke-QmtLogRetention `
            -Directory $LogDir `
            -CurrentLog $LogPath `
            -RetentionDays $LogRetentionDays `
            -MaxTotalBytes $LogMaxTotalBytes
        if ($retention.removed_count -gt 0) {
            Write-QmtLog (
                'log retention removed={0} bytes={1} remaining={2}' -f `
                    $retention.removed_count,
                    $retention.removed_bytes,
                    $retention.remaining_bytes
            )
        }
    } catch {
        # Diagnostic cleanup must never make QMT unavailable.
        Write-QmtLog (
            'log retention warning: {0}: {1}' -f `
                $_.Exception.GetType().Name,
                $_.Exception.Message
        )
    }

    $before = Get-QmtSnapshot -Executable $resolvedExe -Directory $qmtDir
    Write-QmtLog ("before ready={0} reason={1} processes={2}" -f $before.ready, $before.reason_code, $before.process_count)
    if ($Action -eq 'Status') {
        Write-Observation -Observation $before -ExitCode $(if ($before.ready) { 0 } else { 3 })
    }

    if ($Action -eq 'Restart') {
        $targets = @(Get-TargetProcesses -Directory $qmtDir)
        if ($targets.Count -gt 0) {
            Write-QmtLog ('stopping exact configured QMT processes: {0}' -f (($targets.Id | Sort-Object) -join ','))
            $targets | Stop-Process -Force -ErrorAction Stop
        }
        for ($index = 0; $index -lt 30; $index++) {
            if (@(Get-TargetProcesses -Directory $qmtDir).Count -eq 0) { break }
            Start-Sleep -Seconds 1
        }
        if (@(Get-TargetProcesses -Directory $qmtDir).Count -ne 0) {
            throw 'configured QMT processes remained after bounded shutdown'
        }
    }

    $current = Get-QmtSnapshot -Executable $resolvedExe -Directory $qmtDir
    $started = $false
    if (-not $current.ready) {
        if ($current.main_process_count -gt 1) {
            throw 'multiple configured QMT main processes are running'
        }
        $launcher = @(
            Get-TargetProcesses -Directory $qmtDir |
                Where-Object { $_.ProcessName -ieq 'XtItClient' }
        )
        if ($launcher.Count -eq 0) {
            Write-QmtLog "starting QMT launcher: $resolvedExe"
            # QMT is an interactive terminal; leave its window available to
            # the signed-in user.  No credentials or account API are touched.
            Start-Process -FilePath $resolvedExe -WorkingDirectory $qmtDir -ErrorAction Stop | Out-Null
            $started = $true
        } else {
            Write-QmtLog 'existing QMT launcher is still starting; wait for main process'
        }
        $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
        do {
            Start-Sleep -Seconds 1
            $current = Get-QmtSnapshot -Executable $resolvedExe -Directory $qmtDir
            if ($current.ready) { break }
        } while ((Get-Date) -lt $deadline)
        if (-not $current.ready) {
            throw "QMT main process did not become ready within ${StartupTimeoutSeconds}s"
        }
        if ($WarmupSeconds -gt 0) {
            Write-QmtLog "QMT main process ready; warm for ${WarmupSeconds}s"
            Start-Sleep -Seconds $WarmupSeconds
        }
    }

    $final = Get-QmtSnapshot -Executable $resolvedExe -Directory $qmtDir
    $final.changed = ($Action -eq 'Restart' -or $started)
    Write-QmtLog ("completed ready={0} reason={1} changed={2}" -f $final.ready, $final.reason_code, $final.changed)
    Write-Observation -Observation $final -ExitCode $(if ($final.ready) { 0 } else { 3 })
} catch {
    Write-QmtLog ("failed: {0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message)
    $failure = if ($resolvedExe -and $qmtDir) {
        Get-QmtSnapshot -Executable $resolvedExe -Directory $qmtDir
    } else {
        [ordered]@{
            schema = 'chanlun-qmt-app-runtime-observation/v1'
            observed_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.ffffffK')
            action = $Action.ToUpperInvariant()
            ready = $false
            status = 'not_ready'
            reason_code = 'QMT_RUNTIME_OPERATION_FAILED'
            changed = $false
            qmt_executable = $resolvedExe
            qmt_directory = $qmtDir
            log_retention_days = $LogRetentionDays
            log_max_total_bytes = $LogMaxTotalBytes
            main_process_count = 0
            main_started_at = $null
            processes = @()
            process_count = 0
            error = $null
            real_account_accessed = $false
            real_order_transport_enabled = $false
            automated_order_authorized = $false
            live_status = 'LIVE_DISABLED'
        }
    }
    $failure.ready = $false
    $failure.status = 'not_ready'
    if ($failure.reason_code -eq 'READY') {
        $failure.reason_code = 'QMT_RUNTIME_OPERATION_FAILED'
    }
    $failure.error = '{0}: {1}' -f $_.Exception.GetType().Name, $_.Exception.Message
    Write-Observation -Observation $failure -ExitCode 3
} finally {
    if ($acquired -and $null -ne $mutex) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
