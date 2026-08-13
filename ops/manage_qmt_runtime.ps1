# 应用拥有的 QMT 运行时助手。只管理明确配置的 QMT 安装，不启动或停止缠论网页进程。
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
    [ValidateRange(1, 65535)]
    [int]$MarketDataPort = 58610,
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
    # 仅清理本助手带日期的日志。当前调用期间日志不可变，即使单个文件超过上限也保留。
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

function Get-QmtProductIdentity([string]$Executable) {
    $productName = [string](Get-Item -LiteralPath $Executable).VersionInfo.ProductName
    $identity = ($productName -replace '^迅投极速策略交易系统交易终端\s*', '').Trim()
    if ([string]::IsNullOrWhiteSpace($identity)) {
        throw "QMT product identity is unavailable: $Executable"
    }
    return $identity
}

function Test-TargetProcess(
    [Diagnostics.Process]$Process,
    [string]$Directory,
    [string]$ProductIdentity
) {
    try {
        if (
            $Process.Path -and
            (Split-Path -LiteralPath $Process.Path) -ieq $Directory
        ) {
            return $true
        }
    } catch { }

    # 提权运行的 QMT 主窗口可能拒绝暴露可执行路径。此时只接受由目标安装
    # 产品名派生的券商专属窗口标识，避免把另一套券商 QMT 纳入管理范围。
    if ($Process.ProcessName -ieq 'XtMiniQmt') {
        try {
            $title = [string]$Process.MainWindowTitle
            if (
                -not [string]::IsNullOrWhiteSpace($title) -and
                $title.IndexOf(
                    $ProductIdentity,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            ) {
                return $true
            }
        } catch { }
    }
    return $false
}

function Get-TargetProcesses(
    [string]$Directory,
    [string]$ProductIdentity
) {
    $allProcesses = @(
        Get-Process -Name $ProcessNames -ErrorAction SilentlyContinue
    )
    $targetIds = [Collections.Generic.HashSet[int]]::new()
    foreach ($process in $allProcesses) {
        if (
            Test-TargetProcess `
                -Process $process `
                -Directory $Directory `
                -ProductIdentity $ProductIdentity
        ) {
            $null = $targetIds.Add([int]$process.Id)
        }
    }

    # 提权主进程的无窗口子进程也可能拒绝暴露 Path。通过父子关系把它们
    # 归入已由券商专属窗口证明的目标实例，防止遗漏实际承载 RPC 的子进程。
    $nativeRows = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $ProcessNames -contains ($_.Name -replace '\.exe$', '')
            }
    )
    do {
        $changed = $false
        foreach ($row in $nativeRows) {
            $processId = [int]$row.ProcessId
            $parentId = [int]$row.ParentProcessId
            if (
                -not $targetIds.Contains($processId) -and
                $targetIds.Contains($parentId)
            ) {
                $null = $targetIds.Add($processId)
                $changed = $true
            }
        }
    } while ($changed)

    @($allProcesses | Where-Object { $targetIds.Contains([int]$_.Id) })
}

function Test-ProcessControllable(
    [Diagnostics.Process]$Process,
    [string]$Directory
) {
    try {
        return [bool](
            $Process.Path -and
            (Split-Path -LiteralPath $Process.Path) -ieq $Directory
        )
    } catch {
        return $false
    }
}

function Get-QmtSnapshot(
    [string]$Executable,
    [string]$Directory,
    [string]$ProductIdentity
) {
    $processes = @(
        Get-TargetProcesses `
            -Directory $Directory `
            -ProductIdentity $ProductIdentity
    )
    $main = @($processes | Where-Object { $_.ProcessName -ieq 'XtMiniQmt' })
    $targetPids = @($processes | ForEach-Object { [int]$_.Id })
    $uncontrollable = @(
        $processes |
            Where-Object { -not (Test-ProcessControllable $_ $Directory) }
    )
    $rpcListeners = @(
        Get-NetTCPConnection `
            -State Listen `
            -LocalPort $MarketDataPort `
            -ErrorAction SilentlyContinue |
            Where-Object { $targetPids -contains [int]$_.OwningProcess }
    )
    $rpcReady = $rpcListeners.Count -gt 0
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
                    identity_source = if (
                        Test-ProcessControllable $_ $Directory
                    ) {
                        'EXACT_CONFIGURED_PATH'
                    } elseif ($_.ProcessName -ieq 'XtMiniQmt') {
                        'CONFIGURED_PRODUCT_WINDOW'
                    } else {
                        'CONFIGURED_PROCESS_DESCENDANT'
                    }
                }
            }
    )
    $ready = $main.Count -eq 1 -and $rpcReady
    $reason = if (-not $ready -and $uncontrollable.Count -gt 0) {
        'QMT_MANUAL_RESTART_REQUIRED'
    } elseif ($main.Count -gt 1) {
        'MULTIPLE_QMT_MAIN_PROCESSES'
    } elseif ($main.Count -eq 0) {
        'QMT_MAIN_PROCESS_MISSING'
    } elseif (-not $rpcReady) {
        'QMT_MARKET_DATA_RPC_NOT_READY'
    } else {
        'READY'
    }
    return [ordered]@{
        schema = 'chanlun-qmt-app-runtime-observation'
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
        product_identity = $ProductIdentity
        market_data_port = $MarketDataPort
        market_data_rpc_ready = $rpcReady
        market_data_listener_pids = @(
            $rpcListeners | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique
        )
        automatic_control_ready = $uncontrollable.Count -eq 0
        uncontrollable_process_ids = @(
            $uncontrollable | ForEach-Object { [int]$_.Id } | Sort-Object -Unique
        )
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
$productIdentity = $null
$mutex = $null
$acquired = $false
try {
    $resolvedExe = Resolve-QmtExecutable
    $qmtDir = Split-Path -LiteralPath $resolvedExe
    $productIdentity = Get-QmtProductIdentity -Executable $resolvedExe
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
        $busy = Get-QmtSnapshot `
            -Executable $resolvedExe `
            -Directory $qmtDir `
            -ProductIdentity $productIdentity
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
        # 诊断清理不能导致 QMT 不可用。
        Write-QmtLog (
            'log retention warning: {0}: {1}' -f `
                $_.Exception.GetType().Name,
                $_.Exception.Message
        )
    }

    $before = Get-QmtSnapshot `
        -Executable $resolvedExe `
        -Directory $qmtDir `
        -ProductIdentity $productIdentity
    Write-QmtLog ("before ready={0} reason={1} processes={2}" -f $before.ready, $before.reason_code, $before.process_count)
    if ($Action -eq 'Status') {
        Write-Observation -Observation $before -ExitCode $(if ($before.ready) { 0 } else { 3 })
    }

    if ($Action -eq 'Restart') {
        $targets = @(
            Get-TargetProcesses `
                -Directory $qmtDir `
                -ProductIdentity $productIdentity
        )
        $uncontrollable = @(
            $targets |
                Where-Object { -not (Test-ProcessControllable $_ $qmtDir) }
        )
        if ($uncontrollable.Count -gt 0) {
            throw (
                'configured QMT requires manual restart because process control ' +
                'is unavailable for PID(s): ' +
                (($uncontrollable.Id | Sort-Object -Unique) -join ',')
            )
        }
        if ($targets.Count -gt 0) {
            Write-QmtLog ('stopping exact configured QMT processes: {0}' -f (($targets.Id | Sort-Object) -join ','))
            $targets | Stop-Process -Force -ErrorAction Stop
        }
        for ($index = 0; $index -lt 30; $index++) {
            if (@(
                Get-TargetProcesses `
                    -Directory $qmtDir `
                    -ProductIdentity $productIdentity
            ).Count -eq 0) { break }
            Start-Sleep -Seconds 1
        }
        if (@(
            Get-TargetProcesses `
                -Directory $qmtDir `
                -ProductIdentity $productIdentity
        ).Count -ne 0) {
            throw 'configured QMT processes remained after bounded shutdown'
        }
    }

    $current = Get-QmtSnapshot `
        -Executable $resolvedExe `
        -Directory $qmtDir `
        -ProductIdentity $productIdentity
    $started = $false
    if (-not $current.ready) {
        if ($current.main_process_count -gt 1) {
            throw 'multiple configured QMT main processes are running'
        }
        if ($current.main_process_count -eq 1) {
            throw 'configured QMT process exists but market-data RPC is not ready; restart is required'
        }
        $launcher = @(
            Get-TargetProcesses `
                -Directory $qmtDir `
                -ProductIdentity $productIdentity |
                Where-Object { $_.ProcessName -ieq 'XtItClient' }
        )
        if ($launcher.Count -eq 0) {
            Write-QmtLog "starting QMT launcher: $resolvedExe"
            # QMT 是交互式终端，窗口需要留给已登录用户；这里不接触凭据或账户接口。
            Start-Process -FilePath $resolvedExe -WorkingDirectory $qmtDir -ErrorAction Stop | Out-Null
            $started = $true
        } else {
            Write-QmtLog 'existing QMT launcher is still starting; wait for main process'
        }
        $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
        do {
            Start-Sleep -Seconds 1
            $current = Get-QmtSnapshot `
                -Executable $resolvedExe `
                -Directory $qmtDir `
                -ProductIdentity $productIdentity
            if ($current.ready) { break }
        } while ((Get-Date) -lt $deadline)
        if (-not $current.ready) {
            throw "QMT process and market-data RPC did not become ready within ${StartupTimeoutSeconds}s"
        }
        if ($WarmupSeconds -gt 0) {
            Write-QmtLog "QMT main process ready; warm for ${WarmupSeconds}s"
            Start-Sleep -Seconds $WarmupSeconds
        }
    }

    $final = Get-QmtSnapshot `
        -Executable $resolvedExe `
        -Directory $qmtDir `
        -ProductIdentity $productIdentity
    $final.changed = ($Action -eq 'Restart' -or $started)
    Write-QmtLog ("completed ready={0} reason={1} changed={2}" -f $final.ready, $final.reason_code, $final.changed)
    Write-Observation -Observation $final -ExitCode $(if ($final.ready) { 0 } else { 3 })
} catch {
    Write-QmtLog ("failed: {0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message)
    $failure = if ($resolvedExe -and $qmtDir) {
        Get-QmtSnapshot `
            -Executable $resolvedExe `
            -Directory $qmtDir `
            -ProductIdentity $productIdentity
    } else {
        [ordered]@{
            schema = 'chanlun-qmt-app-runtime-observation'
            observed_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.ffffffK')
            action = $Action.ToUpperInvariant()
            ready = $false
            status = 'not_ready'
            reason_code = 'QMT_RUNTIME_OPERATION_FAILED'
            changed = $false
            qmt_executable = $resolvedExe
            qmt_directory = $qmtDir
            product_identity = $productIdentity
            market_data_port = $MarketDataPort
            market_data_rpc_ready = $false
            market_data_listener_pids = @()
            automatic_control_ready = $false
            uncontrollable_process_ids = @()
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
