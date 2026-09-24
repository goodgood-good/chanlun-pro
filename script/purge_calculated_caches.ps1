[CmdletBinding()]
param(
    [string]$DataRoot = 'D:\chanlun_pro',
    [string]$AuditRoot = 'D:\project\chanlun-pro\output\cache_purge_20260923',
    [switch]$ArchivesOnly,
    [switch]$ActiveOnly,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if ($ArchivesOnly -and $ActiveOnly) { throw 'Choose archives or active caches, not both' }
$purgeRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd('\')
$purgeAuditRoot = [IO.Path]::GetFullPath($AuditRoot).TrimEnd('\')
if (-not (Test-Path -LiteralPath $purgeRoot -PathType Container)) {
    throw "Named cache root does not exist: $purgeRoot"
}
if (((Get-Item -LiteralPath $purgeRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Linked cache root is not purged: $purgeRoot"
}

function Assert-PurgePath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if (-not $absolute.StartsWith($purgeRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Purge path outside named data root: $absolute"
    }
    $ancestor = $absolute
    while ($ancestor.Length -ge $purgeRoot.Length) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Linked cache path is not purged: $ancestor"
            }
        }
        if ($ancestor -eq $purgeRoot) { break }
        $ancestor = Split-Path -Parent $ancestor
    }
    return $absolute
}

function Get-PlainFiles([string]$Directory) {
    $files = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Directory)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $current -Force)) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Linked descendant is not purged: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            else { $files.Add($item) }
        }
    }
    return @($files.ToArray())
}

function Get-RecordFiles([string]$Directory) {
    $files = @(Get-ChildItem -LiteralPath $Directory -File -Filter '*.json.gz' -Force)
    foreach ($file in $files) {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Linked record is not purged: $($file.FullName)"
        }
        $absolute = [IO.Path]::GetFullPath($file.FullName)
        if (-not $absolute.StartsWith($Directory + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Record outside checked cache directory: $absolute"
        }
    }
    return $files
}

$targets = @()
function Add-PurgeTarget([string]$Path, [string]$Kind, [bool]$Archived) {
    $source = Assert-PurgePath $Path
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { return }
    $files = if ($Kind -eq 'directory') { @(Get-PlainFiles $source) } else { @(Get-RecordFiles $source) }
    $bytes = ($files | Measure-Object -Property Length -Sum).Sum
    $script:targets += [ordered]@{
        source = $source
        kind = $Kind
        archived = $Archived
        files = $files.Count
        bytes = [long]$bytes
    }
}

if (-not $ActiveOnly) {
    $archiveRoot = Assert-PurgePath (Join-Path $purgeRoot 'cache_archive')
    if (Test-Path -LiteralPath $archiveRoot -PathType Container) {
        foreach ($archive in @(Get-ChildItem -LiteralPath $archiveRoot -Directory -Filter 'segment_review_*' -Force)) {
            if ($archive.Name -notmatch '^segment_review_\d{8}_\d{6}$') { continue }
            $archivePath = Assert-PurgePath $archive.FullName
            foreach ($name in @('chart_cache', 'chart_cache_early_restart', 'prewarm_before_alignment')) {
                Add-PurgeTarget (Join-Path $archivePath $name) 'directory' $true
            }
            Add-PurgeTarget (Join-Path $archivePath 'screening\analysis_cache') 'records' $true
            Add-PurgeTarget (Join-Path $archivePath 'signal_monitor\runs\analysis_cache') 'records' $true
        }
    }
}
if (-not $ArchivesOnly) {
    Add-PurgeTarget (Join-Path $purgeRoot 'chart_cache') 'directory' $false
    Add-PurgeTarget (Join-Path $purgeRoot 'screening\analysis_cache') 'records' $false
    Add-PurgeTarget (Join-Path $purgeRoot 'signal_monitor\runs\analysis_cache') 'records' $false
}

$record = [ordered]@{
    created_at = (Get-Date).ToString('o')
    applied = [bool]$Apply
    data_root = $purgeRoot
    archives_only = [bool]$ArchivesOnly
    active_only = [bool]$ActiveOnly
    targets = $targets
    preserved = @('original market K-line history', 'immutable analysis_cache/inputs snapshots',
                  'archived klines and symbol-source data', 'screening run history and audit evidence',
                  'settings, watchlists and notification preferences')
}

if ($Apply) {
    foreach ($target in $targets) {
        $source = Assert-PurgePath $target.source
        if ($target.kind -eq 'directory') {
            $files = @(Get-PlainFiles $source)
            Remove-Item -LiteralPath $source -Recurse -Force
            if (-not $target.archived) {
                New-Item -ItemType Directory -Path $source -Force | Out-Null
            }
        } else {
            $files = @(Get-RecordFiles $source)
            for ($index = 0; $index -lt $files.Count; $index += 256) {
                $last = [Math]::Min($index + 255, $files.Count - 1)
                $paths = @($files[$index..$last] | ForEach-Object { $_.FullName })
                Remove-Item -LiteralPath $paths -Force
            }
        }
        if ($files.Count -ne $target.files) {
            throw "Cache changed during purge; replan before continuing: $source"
        }
    }
    if (-not $ActiveOnly -and (Test-Path -LiteralPath $archiveRoot -PathType Container)) {
        foreach ($archive in @(Get-ChildItem -LiteralPath $archiveRoot -Directory -Filter 'segment_review_*' -Force)) {
            if ($archive.Name -notmatch '^segment_review_\d{8}_\d{6}$') { continue }
            $archivePath = Assert-PurgePath $archive.FullName
            foreach ($relative in @('screening\analysis_cache', 'signal_monitor\runs\analysis_cache',
                                   'screening', 'signal_monitor\runs', 'signal_monitor')) {
                $candidate = Assert-PurgePath (Join-Path $archivePath $relative)
                if ((Test-Path -LiteralPath $candidate -PathType Container) -and
                    @(Get-ChildItem -LiteralPath $candidate -Force).Count -eq 0) {
                    Remove-Item -LiteralPath $candidate -Force
                }
            }
            if (@(Get-ChildItem -LiteralPath $archivePath -Force).Count -eq 0) {
                Remove-Item -LiteralPath $archivePath -Force
            }
        }
    }
}

New-Item -ItemType Directory -Path $purgeAuditRoot -Force | Out-Null
$name = if ($Apply) { 'cache_purge_receipt.json' } else { 'cache_purge_plan.json' }
[IO.File]::WriteAllText((Join-Path $purgeAuditRoot $name),
    ($record | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
$record | ConvertTo-Json -Depth 6
