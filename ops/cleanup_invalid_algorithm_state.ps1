[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$PreserveValidationGate,
    [switch]$PreserveCurrentScreeningState,
    [switch]$PurgeDevelopmentCaches,
    [switch]$PurgeInvalidSectorDailyFacts,
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
if ($PreserveCurrentScreeningState -and $PurgeInvalidSectorDailyFacts) {
    throw "Preserving current screening state conflicts with purging sector facts"
}

function Get-TreeBytes {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $item = Get-Item -LiteralPath $LiteralPath -Force
    if (-not $item.PSIsContainer) {
        return [int64]$item.Length
    }
    $files = @(
        Get-ChildItem -LiteralPath $LiteralPath -File -Force -Recurse `
            -ErrorAction SilentlyContinue
    )
    if ($files.Count -eq 0) {
        return [int64]0
    }
    $measurement = $files | Measure-Object -Property Length -Sum
    return [int64]$(
        if ($null -eq $measurement.Sum) { 0 } else { $measurement.Sum }
    )
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
    "decision_support\trading_screening_sector_member_status_facts",
    "decision_support\trading_screening_native_worker.log",
    "decision_support\trading_screening_native_worker.structure-1.log",
    "decision_support\trading_screening_native_worker.structure-2.log",
    "decision_support\trading_screening_native_worker.structure-3.log",
    "decision_support\trading_screening_native_worker.structure-4.log",
    "chart_cache",
    "cache\symbols",
    "monitor\dingtalk_outbound_dedupe.json",
    "monitor\dingtalk_outbound_dedupe.json.lock",
    "monitor\dingtalk_chart_images",
    "monitor\holding_group_runtime.json",
    "monitor\holding_group_monitor_currency_spot.json",
    "monitor\holding_group_monitor_us.json",
    "monitor\realtime_review_inbox.json",
    "cache\last_chart_state.json"
)
# A running application may reopen any of these paths immediately after the
# lock probe.  PreserveCurrentScreeningState therefore protects the whole live
# runtime surface, not only the large snapshot files.  The bounded pruning
# below still adds obsolete immutable generations and dead owner markers as
# explicit cleanup targets.
$currentScreeningStateTargets = @($runtimeRelativeTargets)
if ($PreserveCurrentScreeningState) {
    $runtimeRelativeTargets = @(
        $runtimeRelativeTargets | Where-Object {
            $_ -notin $currentScreeningStateTargets
        }
    )

    # Keep only the immutable generation referenced by the current main
    # snapshot.  An unreadable or unauthenticated pointer fails closed and
    # preserves every generation instead of guessing which one is current.
    $snapshotPath = Join-Path `
        $roots[0] `
        "decision_support\trading_screening_snapshot.json"
    $generationDirectory = Join-Path `
        $roots[0] `
        "decision_support\.trading_screening_snapshot.json.generations"
    $currentGenerationName = $null
    if (
        (Test-Path -LiteralPath $snapshotPath) -and
        (Test-Path -LiteralPath $generationDirectory)
    ) {
        try {
            $snapshot = Get-Content `
                -LiteralPath $snapshotPath `
                -Raw `
                -Encoding utf8 | ConvertFrom-Json
            $contentIdentity = [string]$snapshot.snapshot_content_sha256
            if ($contentIdentity -match "^sha256:([0-9a-f]{64})$") {
                $currentGenerationName = "$($Matches[1]).json"
                $currentGenerationPath = Join-Path `
                    $generationDirectory `
                    $currentGenerationName
                if (
                    -not (Test-Path -LiteralPath $currentGenerationPath) -or
                    (Get-FileHash -LiteralPath $snapshotPath -Algorithm SHA256).Hash -ne
                    (
                        Get-FileHash `
                            -LiteralPath $currentGenerationPath `
                            -Algorithm SHA256
                    ).Hash
                ) {
                    $currentGenerationName = $null
                }
            }
        } catch {
            $currentGenerationName = $null
        }
    }
    if ($null -ne $currentGenerationName) {
        foreach ($generation in @(
            Get-ChildItem `
                -LiteralPath $generationDirectory `
                -File `
                -Force `
                -ErrorAction SilentlyContinue
        )) {
            if (
                $generation.Name -match "^[0-9a-f]{64}\.json(\.scope)?$" -and
                $generation.Name -ne $currentGenerationName -and
                $generation.Name -ne "$currentGenerationName.scope"
            ) {
                $runtimeRelativeTargets += (
                    "decision_support\.trading_screening_snapshot.json.generations\" +
                    $generation.Name
                )
            }
        }
    }

    $runtimeCacheParent = Join-Path `
        $roots[0] `
        "decision_support\trading_screening_runtime_state_cache"
    if (Test-Path -LiteralPath $runtimeCacheParent) {
        foreach ($ownerMarker in @(
            Get-ChildItem `
                -LiteralPath $runtimeCacheParent `
                -File `
                -Force `
                -ErrorAction SilentlyContinue
        )) {
            if (
                $ownerMarker.Name -match (
                    "^\.runtime-[0-9a-f]{24}\.owner-(\d+)-[0-9a-f]{16}$"
                ) -and
                $null -eq (
                    Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue
                )
            ) {
                $runtimeRelativeTargets += (
                    "decision_support\trading_screening_runtime_state_cache\" +
                    $ownerMarker.Name
                )
            }
        }
    }
}
if ($PurgeInvalidSectorDailyFacts) {
    $runtimeRelativeTargets += (
        "decision_support\trading_screening_sector_daily_facts.json"
    )
}
$repositoryRelativeTargets = @(
    "audit\chanlun_trading_system_backtest\research_sample_smoke_2",
    ".cache\chanlun_scheduler",
    ".cache\chanlun_web_watchdog",
    ".cache\chanlun_human_review_forward\forward_paper_ledger.json.lock",
    ".cache\center_trend_probe_smoke2.json",
    ".cache\center_trend_probe_validation30.json"
)
if ($PreserveCurrentScreeningState) {
    $activeRepositoryRuntimeTargets = @(
        ".cache\chanlun_scheduler",
        ".cache\chanlun_web_watchdog",
        ".cache\chanlun_human_review_forward\forward_paper_ledger.json.lock"
    )
    $repositoryRelativeTargets = @(
        $repositoryRelativeTargets | Where-Object {
            $_ -notin $activeRepositoryRuntimeTargets
        }
    )
}
if (-not $PreserveValidationGate) {
    $repositoryRelativeTargets += (
        "audit\chanlun_trading_system_backtest\research_sample_validation_12"
    )
}
if ($PurgeDevelopmentCaches) {
    $gitRoot = Join-Path $roots[1] ".git"
    foreach ($directory in @(
        Get-ChildItem `
            -LiteralPath $roots[1] `
            -Directory `
            -Force `
            -Recurse `
            -ErrorAction SilentlyContinue | Where-Object {
                $_.Name -in @("__pycache__", ".pytest_cache", ".ruff_cache") -and
                -not $_.FullName.StartsWith(
                    $gitRoot + [IO.Path]::DirectorySeparatorChar,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )) {
        $repositoryRelativeTargets += $directory.FullName.Substring(
            $roots[1].Length + 1
        )
    }
}
$runtimeRelativeTargets = @($runtimeRelativeTargets | Sort-Object -Unique)
$repositoryRelativeTargets = @($repositoryRelativeTargets | Sort-Object -Unique)

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
$preserved = @(
    "$($roots[0])\klines",
    "$($roots[0])\xdxr",
    "$($roots[0])\decision_support\trading_screening_sector_frame_facts",
    "$($roots[1])\audit\chanlun_trading_system_backtest\pit_reference",
    "$($roots[1])\.cache\chanlun_qmt_sector_ledger"
)
if ($PreserveCurrentScreeningState) {
    $preserved += @(
        "$($roots[0])\decision_support\trading_screening_snapshot.json",
        "$($roots[0])\decision_support\.trading_screening_snapshot.json.generations\$currentGenerationName",
        "$($roots[0])\decision_support\trading_screening_runtime_state_cache",
        "$($roots[0])\decision_support\trading_screening_sector_snapshot.json",
        "$($roots[0])\decision_support\trading_screening_sector_member_status_facts"
    )
}
if (-not $PurgeInvalidSectorDailyFacts) {
    $preserved += (
        "$($roots[0])\decision_support\trading_screening_sector_daily_facts.json"
    )
}
if ($PreserveValidationGate) {
    $preserved += (
        "$($roots[1])\audit\chanlun_trading_system_backtest\research_sample_validation_12"
    )
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
    preserved = $preserved
} | ConvertTo-Json -Depth 6
