[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$RuntimeRoot = "D:\chanlun_pro",
    [string]$RepositoryRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Join-Path $PSScriptRoot ".."
}
$roots = @(
    (Resolve-Path -LiteralPath $RuntimeRoot -ErrorAction Stop).Path,
    (Resolve-Path -LiteralPath $RepositoryRoot -ErrorAction Stop).Path
)

function Get-TreeBytes {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $item = Get-Item -LiteralPath $LiteralPath -Force
    if (-not $item.PSIsContainer) {
        return [int64]$item.Length
    }
    $measurement = Get-ChildItem -LiteralPath $LiteralPath -File -Force -Recurse `
        -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum
    return [int64]$(if ($null -eq $measurement.Sum) { 0 } else { $measurement.Sum })
}

function Resolve-BoundedTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $candidate = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $candidate)) {
        return $null
    }
    $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
    $prefix = $Root + [IO.Path]::DirectorySeparatorChar
    if (
        $resolved -eq $Root -or
        -not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Refusing cleanup target outside its bounded root: $resolved"
    }
    return $resolved
}

$runtimeRelativeTargets = @(
    "decision_support\trading_screening_runtime_state_cache",
    "decision_support\.trading_screening_snapshot.json.generations",
    "decision_support\trading_screening_snapshot.json",
    "decision_support\trading_screening_snapshot.json.lock",
    "decision_support\trading_screening_snapshot.json.scope",
    "decision_support\trading_priority_monitor_state.json",
    "decision_support\trading_priority_monitor_state.json.lock",
    "decision_support\trading_notification_outbox.json",
    "decision_support\trading_notification_outbox.json.tmp",
    "decision_support\trading_notification_state.json",
    "decision_support\trading_notification_state.json.tmp",
    "decision_support\trading_screening_sector_snapshot.json",
    "decision_support\trading_screening_sector_snapshot.json.scope",
    "decision_support\trading_screening_native_worker.log",
    "decision_support\trading_screening_native_worker.structure-1.log",
    "decision_support\trading_screening_native_worker.structure-2.log",
    "decision_support\trading_screening_native_worker.structure-3.log",
    "decision_support\trading_screening_native_worker.structure-4.log",
    "chart_cache",
    "monitor\dingtalk_outbound_dedupe.json",
    "monitor\holding_group_runtime.json",
    "monitor\realtime_review_inbox.json",
    "cache\last_chart_state.json"
)
$repositoryRelativeTargets = @(
    "audit\chanlun_trading_system_backtest\research_sample_smoke_2",
    "audit\chanlun_trading_system_backtest\research_sample_validation_12",
    ".cache\chanlun_scheduler",
    ".cache\chanlun_web_watchdog",
    ".cache\chanlun_human_review_forward\forward_paper_ledger.json.lock",
    ".cache\center_trend_probe_smoke2.json",
    ".cache\center_trend_probe_validation30.json"
)

$candidates = [System.Collections.Generic.List[object]]::new()
foreach ($definition in @(
    @{ Root = $roots[0]; Paths = $runtimeRelativeTargets; Scope = "runtime" },
    @{ Root = $roots[1]; Paths = $repositoryRelativeTargets; Scope = "repository" }
)) {
    foreach ($relativePath in $definition.Paths) {
        $resolved = Resolve-BoundedTarget `
            -Root $definition.Root `
            -RelativePath $relativePath
        if ($null -eq $resolved) {
            continue
        }
        $item = Get-Item -LiteralPath $resolved -Force
        $candidates.Add([pscustomobject]@{
            scope = $definition.Scope
            path = $resolved
            bytes = Get-TreeBytes -LiteralPath $resolved
            directory = [bool]$item.PSIsContainer
        })
    }
}

$ordered = @($candidates | Sort-Object path)
if ($Execute) {
    $locked = [System.Collections.Generic.List[string]]::new()
    foreach ($target in $ordered) {
        $files = if ($target.directory) {
            @(Get-ChildItem -LiteralPath $target.path -File -Force -Recurse)
        } else {
            @(Get-Item -LiteralPath $target.path -Force)
        }
        foreach ($file in $files) {
            $handle = $null
            try {
                $handle = [IO.File]::Open(
                    $file.FullName,
                    [IO.FileMode]::Open,
                    [IO.FileAccess]::ReadWrite,
                    [IO.FileShare]::None
                )
            } catch [IO.FileNotFoundException] {
                continue
            } catch {
                $locked.Add($file.FullName)
            } finally {
                if ($null -ne $handle) {
                    $handle.Dispose()
                }
            }
        }
    }
    if ($locked.Count -gt 0) {
        throw "Refusing cleanup because candidate files are active: $($locked -join ', ')"
    }
    foreach ($target in $ordered) {
        if (-not (Test-Path -LiteralPath $target.path)) {
            continue
        }
        if ($target.directory) {
            Remove-Item -LiteralPath $target.path -Recurse -Force
        } else {
            Remove-Item -LiteralPath $target.path -Force
        }
    }
}

$remaining = @($ordered | Where-Object { Test-Path -LiteralPath $_.path })
$totalBytes = [int64]0
foreach ($target in $ordered) {
    $totalBytes += [int64]$target.bytes
}
[ordered]@{
    schema = "chanlun-invalid-algorithm-state-cleanup"
    observed_at = [DateTimeOffset]::Now.ToString("o")
    mode = if ($Execute) { "EXECUTE" } else { "DRY_RUN" }
    candidate_count = $ordered.Count
    candidate_bytes = $totalBytes
    removed_count = if ($Execute) { $ordered.Count - $remaining.Count } else { 0 }
    remaining_count = $remaining.Count
    candidates = $ordered
    preserved = @(
        "$($roots[0])\klines",
        "$($roots[0])\xdxr",
        "$($roots[0])\decision_support\trading_screening_sector_frame_facts",
        "$($roots[0])\decision_support\trading_screening_sector_daily_facts.json",
        "$($roots[1])\audit\chanlun_trading_system_backtest\pit_reference",
        "$($roots[1])\.cache\chanlun_qmt_sector_ledger"
    )
} | ConvertTo-Json -Depth 6
