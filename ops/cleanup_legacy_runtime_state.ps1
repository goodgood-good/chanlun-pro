[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$RuntimeRoot = "D:\chanlun_pro"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$runtimeRootPath = [IO.Path]::GetFullPath($RuntimeRoot)
if (-not (Test-Path -LiteralPath $runtimeRootPath -PathType Container)) {
    throw "Runtime root does not exist: $runtimeRootPath"
}
$runtimeRootPath = (Resolve-Path -LiteralPath $runtimeRootPath).Path
$runtimePrefix = $runtimeRootPath + [IO.Path]::DirectorySeparatorChar

function Assert-DirectRuntimeChild {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $resolved = (Resolve-Path -LiteralPath $LiteralPath -ErrorAction Stop).Path
    if (
        -not $resolved.StartsWith(
            $runtimePrefix,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        (Split-Path -Parent $resolved) -ne $runtimeRootPath
    ) {
        throw "Refusing non-direct runtime child: $resolved"
    }
    return $resolved
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
    $sum = [int64]0
    foreach ($file in $files) {
        $sum += [int64]$file.Length
    }
    return $sum
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

$retiredDirectoryNames = @(
    "cache",
    "cache_pkl",
    "chart_cache",
    "cl_data",
    "decision_support",
    "json",
    "logs",
    "monitor",
    "png",
    "prewarm_status",
    "reports"
)
$candidates = [System.Collections.Generic.List[object]]::new()
foreach ($name in $retiredDirectoryNames) {
    $path = Join-Path $runtimeRootPath $name
    if (-not (Test-Path -LiteralPath $path)) {
        continue
    }
    $resolved = Assert-DirectRuntimeChild -LiteralPath $path
    $item = Get-Item -LiteralPath $resolved -Force
    if (-not $item.PSIsContainer) {
        throw "Expected retired runtime directory: $resolved"
    }
    $candidates.Add([pscustomobject]@{
        category = "retired_generated_directory"
        path = $resolved
        bytes = Get-TreeBytes -LiteralPath $resolved
        directory = $true
    })
}

$currentQuotaName = 'lb_quota_{0}.json' -f (Get-Date -Format 'yyyy-MM')
$oldQuotaFiles = @(
    Get-ChildItem -LiteralPath $runtimeRootPath -File -Force `
        -Filter "lb_quota_*.json" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne $currentQuotaName }
)
foreach ($item in $oldQuotaFiles) {
    $resolved = Assert-DirectRuntimeChild -LiteralPath $item.FullName
    $candidates.Add([pscustomobject]@{
        category = "expired_monthly_quota"
        path = $resolved
        bytes = [int64]$item.Length
        directory = $false
    })
}

$ordered = @($candidates | Sort-Object path)
$totalBytes = [int64]0
foreach ($target in $ordered) {
    $totalBytes += [int64]$target.bytes
}
if ($Execute) {
    $lockedCandidateFiles = @(
        Get-LockedCleanupCandidateFiles -Targets $ordered
    )
    if ($lockedCandidateFiles.Count -gt 0) {
        throw (
            "Refusing cleanup because candidate files are active: {0}" -f `
                ($lockedCandidateFiles -join ", ")
        )
    }
    foreach ($target in $ordered) {
        if (-not (Test-Path -LiteralPath $target.path)) {
            continue
        }
        $checked = Assert-DirectRuntimeChild -LiteralPath $target.path
        if ($target.directory) {
            Remove-Item -LiteralPath $checked -Recurse -Force
        } else {
            Remove-Item -LiteralPath $checked -Force
        }
    }
}

$remaining = @(
    $ordered | Where-Object { Test-Path -LiteralPath $_.path }
)
[ordered]@{
    schema = "chanlun-retired-runtime-state-cleanup"
    observed_at = [DateTimeOffset]::Now.ToString("o")
    mode = if ($Execute) { "EXECUTE" } else { "DRY_RUN" }
    runtime_root = $runtimeRootPath
    candidate_count = $ordered.Count
    candidate_bytes = $totalBytes
    removed_count = if ($Execute) { $ordered.Count - $remaining.Count } else { 0 }
    remaining_count = $remaining.Count
    preserved = @(
        ".flask_secret_key",
        ".flask_secret_key.lock",
        "klines",
        "xdxr",
        $currentQuotaName
    )
    candidates = @($ordered)
} | ConvertTo-Json -Depth 5
