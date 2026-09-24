[CmdletBinding()]
param(
    [string]$DataRoot = 'D:\chanlun_pro',
    [string]$AuditRoot = 'D:\project\chanlun-pro\output\market_segment_audit_20260918',
    [switch]$StructureOnly,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$cacheDataRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd('\')
$cacheAuditRoot = [IO.Path]::GetFullPath($AuditRoot).TrimEnd('\')
$cacheRelativePaths = @('chart_cache', 'screening\analysis_cache', 'signal_monitor\runs\analysis_cache')
if (-not $StructureOnly) { $cacheRelativePaths += @('cache\symbols', 'klines') }
$cacheBackupRoot = Join-Path $cacheDataRoot ('cache_archive\segment_review_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
$cachePlan = @()

function Assert-CachePath([string]$Path) {
    $resolvedCachePath = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedCachePath.StartsWith($cacheDataRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Cache target outside the named data directory: $resolvedCachePath"
    }
    $ancestorCachePath = $resolvedCachePath
    while ($ancestorCachePath.Length -ge $cacheDataRoot.Length) {
        if (Test-Path -LiteralPath $ancestorCachePath) {
            $item = Get-Item -LiteralPath $ancestorCachePath -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Linked cache paths are not cleared: $ancestorCachePath"
            }
        }
        if ($ancestorCachePath -eq $cacheDataRoot) { break }
        $ancestorCachePath = Split-Path -Parent $ancestorCachePath
    }
    return $resolvedCachePath
}

foreach ($relativeCachePath in $cacheRelativePaths) {
    $sourceCachePath = Assert-CachePath (Join-Path $cacheDataRoot $relativeCachePath)
    $destinationCachePath = Assert-CachePath (Join-Path $cacheBackupRoot $relativeCachePath)
    if (-not (Test-Path -LiteralPath $sourceCachePath -PathType Container)) { continue }
    $recordsOnly = $relativeCachePath -in @('screening\analysis_cache', 'signal_monitor\runs\analysis_cache')
    # The inputs subdirectory is immutable audit evidence. Clearing reusable
    # calculations must preserve its absolute paths for frozen audit records.
    $files = if ($recordsOnly) {
        @(Get-ChildItem -LiteralPath $sourceCachePath -File -Filter '*.json.gz' -Force)
    } else {
        @(Get-ChildItem -LiteralPath $sourceCachePath -File -Recurse -Force)
    }
    $bytes = ($files | Measure-Object -Property Length -Sum).Sum
    $cachePlan += [ordered]@{ source = $sourceCachePath; backup = $destinationCachePath; files = $files.Count; bytes = [long]$bytes; records_only = $recordsOnly }
}

$cacheRecord = [ordered]@{
    created_at = (Get-Date).ToString('o')
    applied = [bool]$Apply
    data_root = $cacheDataRoot
    backup_root = $cacheBackupRoot
    targets = $cachePlan
    preserved = @('historical screening runs and evidence', 'immutable calculation input snapshots', 'settings and watchlists', 'notification preferences', 'QMT source history', 'credentials')
    structure_only = [bool]$StructureOnly
}
if ($Apply) {
    if (-not (Test-Path -LiteralPath (Join-Path $cacheAuditRoot 'screening_preflight.json'))) {
        throw 'Verified screening preflight is required before moving caches'
    }
    foreach ($cacheEntry in $cachePlan) {
        # Both absolute endpoints were checked above; use the same native shell
        # for the entire move. Moving keeps rollback evidence without making it
        # visible to any normal cache reader.
        $sourceCachePath = Assert-CachePath $cacheEntry.source
        $destinationCachePath = Assert-CachePath $cacheEntry.backup
        if ($cacheEntry.records_only) {
            New-Item -ItemType Directory -Path $destinationCachePath -Force | Out-Null
            foreach ($cacheFile in @(Get-ChildItem -LiteralPath $sourceCachePath -File -Filter '*.json.gz' -Force)) {
                $recordSource = Assert-CachePath $cacheFile.FullName
                $recordDestination = Assert-CachePath (Join-Path $destinationCachePath $cacheFile.Name)
                Move-Item -LiteralPath $recordSource -Destination $recordDestination
            }
        } else {
            New-Item -ItemType Directory -Path (Split-Path -Parent $destinationCachePath) -Force | Out-Null
            Move-Item -LiteralPath $sourceCachePath -Destination $destinationCachePath
            New-Item -ItemType Directory -Path $sourceCachePath -Force | Out-Null
        }
    }
}
New-Item -ItemType Directory -Path $cacheAuditRoot -Force | Out-Null
$cacheRecordName = if ($Apply) { 'cache_clear.json' } else { 'cache_plan.json' }
[IO.File]::WriteAllText((Join-Path $cacheAuditRoot $cacheRecordName), ($cacheRecord | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
$cacheRecord | ConvertTo-Json -Depth 6
