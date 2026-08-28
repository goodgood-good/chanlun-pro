[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$PurgeInvalidBacktestFacts,
    [switch]$PurgeRetiredRuntimeState
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$repositoryPrefix = $repositoryRoot + [IO.Path]::DirectorySeparatorChar
$candidates = [System.Collections.Generic.List[object]]::new()
$seen = [System.Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)

function Get-ArtifactBytes {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $item = Get-Item -LiteralPath $LiteralPath -Force
    if (-not $item.PSIsContainer) {
        return [int64]$item.Length
    }

    $measurement = (
        Get-ChildItem -LiteralPath $LiteralPath -File -Force -Recurse `
            -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum
    )
    $sum = if ($null -eq $measurement) { $null } else { $measurement.Sum }
    if ($null -eq $sum) {
        return [int64]0
    }
    return [int64]$sum
}

function Add-CleanupCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Category
    )

    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        return
    }

    $resolved = (Resolve-Path -LiteralPath $LiteralPath).Path
    if (
        $resolved -eq $repositoryRoot -or
        -not $resolved.StartsWith(
            $repositoryPrefix,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Refusing cleanup target outside repository: $resolved"
    }
    if (-not $seen.Add($resolved)) {
        return
    }

    $item = Get-Item -LiteralPath $resolved -Force
    $candidates.Add([pscustomobject]@{
        category = $Category
        path = $resolved
        bytes = Get-ArtifactBytes -LiteralPath $resolved
        directory = [bool]$item.PSIsContainer
    })
}

function Get-LockedCleanupCandidateFiles {
    param([Parameter(Mandatory = $true)][object[]]$Targets)

    $locked = [System.Collections.Generic.List[string]]::new()
    foreach ($target in $Targets) {
        if (-not (Test-Path -LiteralPath $target.path)) {
            continue
        }
        $files = if ($target.directory) {
            @(
                Get-ChildItem -LiteralPath $target.path -File -Force -Recurse `
                    -ErrorAction SilentlyContinue
            )
        } else {
            @(Get-Item -LiteralPath $target.path -Force)
        }
        foreach ($file in $files) {
            $exclusiveHandle = $null
            try {
                $exclusiveHandle = [IO.File]::Open(
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
                if ($null -ne $exclusiveHandle) {
                    $exclusiveHandle.Dispose()
                }
            }
        }
    }
    return @($locked)
}

$fixedTargets = @(
    @{ path = ".playwright-cli"; category = "browser_debug" },
    @{ path = ".pytest_cache"; category = "test_cache" },
    @{ path = ".ruff_cache"; category = "lint_cache" },
    @{ path = ".omc"; category = "agent_session_artifact" },
    @{ path = "output"; category = "generated_output" },
    @{ path = "tmp"; category = "temporary_artifact" }
)
foreach ($target in $fixedTargets) {
    Add-CleanupCandidate `
        -LiteralPath (Join-Path $repositoryRoot $target.path) `
        -Category $target.category
}

if ($PurgeInvalidBacktestFacts) {
    $invalidBacktestTargets = @(
        # A portfolio report and its PIT metadata are inseparable from the
        # extracted symbol facts that produced them. Purging only the fact
        # shards leaves a stale result that can be mistaken for current
        # evidence, so invalidate the explicitly bounded run directory and
        # the two root-level publication artifacts as one unit.
        # Add-CleanupCandidate still refuses repository-root escapes.
        "audit\chanlun_trading_system_backtest\full_market_explicit",
        "audit\chanlun_trading_system_backtest\certified_report.json",
        "audit\chanlun_trading_system_backtest\causality_gate.json"
    )
    foreach ($target in $invalidBacktestTargets) {
        Add-CleanupCandidate `
            -LiteralPath (Join-Path $repositoryRoot $target) `
            -Category "invalid_backtest_artifact"
    }
    $backtestAuditRoot = Join-Path `
        $repositoryRoot `
        "audit\chanlun_trading_system_backtest"
    if (Test-Path -LiteralPath $backtestAuditRoot) {
        $targetedScratchDirectories = @(
            Get-ChildItem -LiteralPath $backtestAuditRoot -Directory -Force `
                -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.Name -like "targeted_v*" -or
                    $_.Name -like "research_diagnostic_*"
                }
        )
        foreach ($target in $targetedScratchDirectories) {
            Add-CleanupCandidate `
                -LiteralPath $target.FullName `
                -Category "invalid_backtest_artifact"
        }
    }
}

if ($PurgeRetiredRuntimeState) {
    $retiredRuntimeTargets = @(
        ".cache\chanlun_human_review",
        ".cache\chanlun_human_review_forward",
        ".cache\chanlun_scheduler",
        ".cache\chanlun_web_watchdog"
    )
    foreach ($target in $retiredRuntimeTargets) {
        Add-CleanupCandidate `
            -LiteralPath (Join-Path $repositoryRoot $target) `
            -Category "retired_runtime_state"
    }
}

# Crash dump names are timestamped and cannot be maintained as a finite list.
# Only root-level ``.dmp`` files are eligible; source and data subtrees remain
# outside this cleanup rule.
$rootCrashDumps = @(
    Get-ChildItem -LiteralPath $repositoryRoot -File -Force -Filter "*.dmp" `
        -ErrorAction SilentlyContinue
)
foreach ($target in $rootCrashDumps) {
    Add-CleanupCandidate -LiteralPath $target.FullName -Category "crash_dump"
}

# These cache namespaces and one-off probe outputs were retired before the
# current unversioned runtime ledgers. Keep the list explicit so cleanup can
# never expand to the active chanlun_human_review*, chanlun_qmt_sector_ledger,
# chanlun_scheduler, or chanlun_web_watchdog directories by accident.
$legacyCacheTargets = @(
    ".cache\chanlun_v3_available_data",
    ".cache\chanlun_v3_external_pit",
    ".cache\chanlun_v3_human_review",
    ".cache\chanlun_v3_human_review_forward",
    ".cache\chanlun_v3_qmt_sector_ledger",
    ".cache\chanlun_v3_scheduler",
    ".cache\chanlun_v31_159919",
    ".cache\chanlun_v31_159925",
    ".cache\chanlun_v31_510330",
    ".cache\chanlun_v31_510360",
    ".cache\chanlun_v31_510380",
    ".cache\chanlun_v31_510390",
    ".cache\chanlun_v31_csi300_broad_pool",
    ".cache\chanlun_v31_csi300_etfs",
    ".cache\diagnostics",
    ".cache\dingtalk_sdk_probe",
    ".cache\fixed_year_local_probe",
    ".cache\fixed_year_local_probe_16",
    ".cache\fixed_year_preflight_current",
    ".cache\fixed_year_preflight_current_v2",
    ".cache\fixed_year_probe",
    ".cache\chanlun_backtest_six_month_target_sh600216_afterfix_20260126_20260724",
    ".cache\chanlun_backtest_six_month_target_sz301500_afterfix_20260126_20260724",
    ".cache\chanlun_backtest_six_month_full_20260126_20260724",
    ".cache\chanlun_backtest_six_month_validation12_20260126_20260724",
    ".cache\chanlun_backtest_six_month_validation32_20260126_20260724",
    ".cache\historical_backtest_preflight_fix_20260816",
    ".cache\historical_backtest_preflight_report_20260816",
    ".cache\icon-preview"
)
foreach ($target in $legacyCacheTargets) {
    Add-CleanupCandidate `
        -LiteralPath (Join-Path $repositoryRoot $target) `
        -Category "legacy_cache"
}

$pythonCaches = @(
    Get-ChildItem -LiteralPath $repositoryRoot -Directory -Force -Recurse `
        -Filter "__pycache__" -ErrorAction SilentlyContinue
)
foreach ($target in $pythonCaches) {
    Add-CleanupCandidate -LiteralPath $target.FullName -Category "python_bytecode"
}

# Root application logs are generated artifacts too, but ``app.log`` may still
# be held by the currently running Web process.  Probe exclusive access and
# defer only files that are genuinely active; all historical log files remain
# eligible immediately.
$activeRootAppLogs = [System.Collections.Generic.List[string]]::new()
$rootGeneratedLogPath = Join-Path $repositoryRoot "logs"
if (Test-Path -LiteralPath $rootGeneratedLogPath) {
    $rootGeneratedLogs = @(
        Get-ChildItem -LiteralPath $rootGeneratedLogPath -File -Force `
            -ErrorAction SilentlyContinue
    )
    foreach ($target in $rootGeneratedLogs) {
        if ($target.Name -eq "app.log") {
            $exclusiveHandle = $null
            try {
                $exclusiveHandle = [IO.File]::Open(
                    $target.FullName,
                    [IO.FileMode]::Open,
                    [IO.FileAccess]::ReadWrite,
                    [IO.FileShare]::None
                )
            } catch [IO.IOException] {
                $activeRootAppLogs.Add($target.FullName)
                continue
            } finally {
                if ($null -ne $exclusiveHandle) {
                    $exclusiveHandle.Dispose()
                }
            }
        }
        Add-CleanupCandidate `
            -LiteralPath $target.FullName `
            -Category "generated_log"
    }
}

# Flask's rotating file handler writes beside the Web application.  This is a
# separate generated-log root from the repository-level operational log and
# was missed by the original cleanup contract.  Apply the same exclusive-file
# guard to every file so an active handler is preserved while rotated and
# historical logs remain safely removable.
$appGeneratedLogPath = Join-Path $repositoryRoot "web\chanlun_chart\logs"
if (Test-Path -LiteralPath $appGeneratedLogPath) {
    $appGeneratedLogs = @(
        Get-ChildItem -LiteralPath $appGeneratedLogPath -File -Force `
            -ErrorAction SilentlyContinue
    )
    foreach ($target in $appGeneratedLogs) {
        $exclusiveHandle = $null
        try {
            $exclusiveHandle = [IO.File]::Open(
                $target.FullName,
                [IO.FileMode]::Open,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )
        } catch [IO.IOException] {
            $activeRootAppLogs.Add($target.FullName)
            continue
        } finally {
            if ($null -ne $exclusiveHandle) {
                $exclusiveHandle.Dispose()
            }
        }
        Add-CleanupCandidate `
            -LiteralPath $target.FullName `
            -Category "generated_log"
    }
}

# Retired launchers wrote directly beside ``app.py`` before operational logs
# moved under ``ops\logs``.  Keep an exclusive-access guard in case an older
# deployment is still attached, but otherwise remove these historical files.
$legacyWebLogRoot = Join-Path $repositoryRoot "web\chanlun_chart"
if (Test-Path -LiteralPath $legacyWebLogRoot) {
    $legacyWebLogs = @(
        Get-ChildItem -LiteralPath $legacyWebLogRoot -File -Force `
            -Filter "_restart_*.log" -ErrorAction SilentlyContinue
    )
    foreach ($target in $legacyWebLogs) {
        $exclusiveHandle = $null
        try {
            $exclusiveHandle = [IO.File]::Open(
                $target.FullName,
                [IO.FileMode]::Open,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )
        } catch [IO.IOException] {
            $activeRootAppLogs.Add($target.FullName)
            continue
        } finally {
            if ($null -ne $exclusiveHandle) {
                $exclusiveHandle.Dispose()
            }
        }
        Add-CleanupCandidate `
            -LiteralPath $target.FullName `
            -Category "generated_log"
    }
}

$logRoot = Join-Path $repositoryRoot "ops\logs"
$activeOpsLogs = [System.Collections.Generic.List[string]]::new()
if (Test-Path -LiteralPath $logRoot) {
    $generatedLogs = @(
        Get-ChildItem -LiteralPath $logRoot -File -Force |
            Where-Object {
                $_.Name -like "web_restart_*" -or
                $_.Name -like "web_stdout_*" -or
                $_.Name -like "web_stderr_*" -or
                $_.Name -like "web_recovery_*" -or
                $_.Name -like "web_watchdog_*" -or
                $_.Name -like "forward_paper_*" -or
                $_.Name -like "restart_*" -or
                $_.Name -eq "historical_backtest.lock" -or
                $_.Name -match '^qmt_runtime_\d{4}-\d{2}-\d{2}\.log$'
            }
    )
    foreach ($target in $generatedLogs) {
        $exclusiveHandle = $null
        try {
            $exclusiveHandle = [IO.File]::Open(
                $target.FullName,
                [IO.FileMode]::Open,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )
        } catch [IO.IOException] {
            $activeOpsLogs.Add($target.FullName)
            continue
        } finally {
            if ($null -ne $exclusiveHandle) {
                $exclusiveHandle.Dispose()
            }
        }
        Add-CleanupCandidate -LiteralPath $target.FullName -Category "generated_log"
    }
}

$auditRoot = Join-Path $repositoryRoot "audit"
if (Test-Path -LiteralPath $auditRoot) {
    $generatedAuditLogs = @(
        Get-ChildItem -LiteralPath $auditRoot -File -Force |
            Where-Object {
                $_.Name -like "server-*.stdout.log" -or
                $_.Name -like "server-*.stderr.log"
            }
    )
    foreach ($target in $generatedAuditLogs) {
        Add-CleanupCandidate `
            -LiteralPath $target.FullName `
            -Category "generated_log"
    }
}

$orderedCandidates = @(
    $candidates |
        Sort-Object @{ Expression = { $_.path.Length }; Descending = $true }
)
$totalBytes = [int64]0
foreach ($target in $orderedCandidates) {
    $totalBytes += [int64]$target.bytes
}

if ($Execute) {
    $lockedCandidateFiles = @(
        Get-LockedCleanupCandidateFiles -Targets $orderedCandidates
    )
    if ($lockedCandidateFiles.Count -gt 0) {
        throw (
            "Refusing cleanup because candidate files are active: {0}" -f `
                ($lockedCandidateFiles -join ", ")
        )
    }
    foreach ($target in $orderedCandidates) {
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

$remaining = @(
    $orderedCandidates |
        Where-Object { Test-Path -LiteralPath $_.path }
)
$byCategory = @(
    $orderedCandidates |
        Group-Object -Property category |
        ForEach-Object {
            [pscustomobject]@{
                category = $_.Name
                count = $_.Count
                bytes = [int64](
                    ($_.Group | Measure-Object -Property bytes -Sum).Sum
                )
            }
        }
)

$result = [ordered]@{
    schema = "chanlun-local-generated-artifact-cleanup"
    observed_at = [DateTimeOffset]::Now.ToString("o")
    mode = if ($Execute) { "EXECUTE" } else { "DRY_RUN" }
    repository_root = $repositoryRoot
    candidate_count = $orderedCandidates.Count
    candidate_bytes = $totalBytes
    removed_count = if ($Execute) {
        $orderedCandidates.Count - $remaining.Count
    } else {
        0
    }
    remaining_count = $remaining.Count
    categories = $byCategory
    protected = [ordered]@{
        active_app_logs = @($activeRootAppLogs)
        active_ops_logs = @($activeOpsLogs)
        active_forward_cache = Test-Path -LiteralPath (
            Join-Path $repositoryRoot ".cache\chanlun_human_review_forward"
        )
        human_review_ledger = Test-Path -LiteralPath (
            Join-Path $repositoryRoot ".cache\chanlun_human_review"
        )
        qmt_sector_ledger = Test-Path -LiteralPath (
            Join-Path $repositoryRoot ".cache\chanlun_qmt_sector_ledger"
        )
    }
}

$json = $result | ConvertTo-Json -Depth 6
$json
