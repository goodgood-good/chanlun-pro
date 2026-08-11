[CmdletBinding()]
param(
    [switch]$Execute
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

    $sum = (
        Get-ChildItem -LiteralPath $LiteralPath -File -Force -Recurse `
            -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum
    ).Sum
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

$fixedTargets = @(
    @{ path = ".playwright-cli"; category = "browser_debug" },
    @{ path = ".pytest_cache"; category = "test_cache" },
    @{ path = ".ruff_cache"; category = "lint_cache" },
    @{ path = "2026072510712998.dmp"; category = "crash_dump" },
    @{ path = "20260725110105013.dmp"; category = "crash_dump" }
)
foreach ($target in $fixedTargets) {
    Add-CleanupCandidate `
        -LiteralPath (Join-Path $repositoryRoot $target.path) `
        -Category $target.category
}

$pythonCaches = @(
    Get-ChildItem -LiteralPath $repositoryRoot -Directory -Force -Recurse `
        -Filter "__pycache__" -ErrorAction SilentlyContinue
)
foreach ($target in $pythonCaches) {
    Add-CleanupCandidate -LiteralPath $target.FullName -Category "python_bytecode"
}

$logRoot = Join-Path $repositoryRoot "ops\logs"
if (Test-Path -LiteralPath $logRoot) {
    $generatedLogs = @(
        Get-ChildItem -LiteralPath $logRoot -File -Force |
            Where-Object {
                $_.Name -like "web_restart_*" -or
                $_.Name -like "web_stdout_*" -or
                $_.Name -like "web_stderr_*" -or
                $_.Name -like "forward_paper_*" -or
                $_.Name -like "restart_*"
            }
    )
    foreach ($target in $generatedLogs) {
        Add-CleanupCandidate -LiteralPath $target.FullName -Category "generated_log"
    }
}

$orderedCandidates = @(
    $candidates |
        Sort-Object @{ Expression = { $_.path.Length }; Descending = $true }
)
$totalBytes = [int64](
    ($orderedCandidates | Measure-Object -Property bytes -Sum).Sum
)

if ($Execute) {
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
        qmt_runtime_logs = @(
            Get-ChildItem -LiteralPath $logRoot -File `
                -Filter "qmt_runtime_*.log" -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
        )
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
if ($Execute) {
    $receiptPath = Join-Path `
        $repositoryRoot `
        ".cache\chanlun_scheduler\local_cleanup_latest.json"
    $receiptDirectory = Split-Path -Parent $receiptPath
    New-Item -ItemType Directory -Path $receiptDirectory -Force | Out-Null
    $temporaryReceipt = $receiptPath + ".tmp"
    Set-Content -LiteralPath $temporaryReceipt -Value $json -Encoding UTF8
    Move-Item -LiteralPath $temporaryReceipt -Destination $receiptPath -Force
}
$json
