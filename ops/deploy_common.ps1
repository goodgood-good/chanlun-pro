function ConvertTo-ProcessStartDate {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Value)

    if ($Value -is [datetime]) {
        return [datetime]$Value
    }
    if ($Value -is [datetimeoffset]) {
        return ([datetimeoffset]$Value).LocalDateTime
    }

    $text = [string]$Value
    try {
        return [Management.ManagementDateTimeConverter]::ToDateTime($text)
    } catch {
        $parsed = [datetime]::MinValue
        if ([datetime]::TryParse($text, [ref]$parsed)) {
            return $parsed
        }
        throw "Unsupported process creation date: $text"
    }
}

function Get-ApplicationSourceRevision {
    param([Parameter(Mandatory = $true)][string]$Root)

    $headOutput = @(& git -C $Root rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $headOutput.Count -eq 0) {
        throw 'unable to resolve deployment git revision'
    }
    $head = ([string]$headOutput[-1]).Trim()
    if ([string]::IsNullOrWhiteSpace($head)) {
        throw 'deployment git revision is empty'
    }

    # Keep this list byte-for-byte equivalent to
    # tools/run_v3_forward_paper.py::FORWARD_PIPELINE_TOOL_PATHS.  These
    # subprocesses execute decision/PIT code from disk and therefore belong to
    # the deployed application identity; unrelated maintenance tools do not.
    $forwardPipelineTools = @(
        'tools/backtest_v3_sector_first_full_market.py',
        'tools/build_v3_recent_year_current_sector_triggers.py',
        'tools/extract_v3_sector_first_direct_facts.py',
        'tools/prescreen_v3_sector_first_research_candidates.py',
        'tools/run_v3_forward_paper.py',
        'tools/snapshot_qmt_gics3_sector_ledger.py',
        'tools/snapshot_qmt_pit_metadata.py'
    )
    $sourcePaths = @('src', 'web/chanlun_chart', 'ops', 'windows_run.bat') + $forwardPipelineTools
    $paths = @(& git -C $Root -c core.quotePath=false ls-files --cached --others --exclude-standard -- @sourcePaths 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'unable to enumerate application source files'
    }
    $runtimeConfig = 'src/chanlun/config.py'
    if ((Test-Path -LiteralPath (Join-Path $Root $runtimeConfig) -PathType Leaf) -and $paths -notcontains $runtimeConfig) {
        $paths += $runtimeConfig
    }
    $paths = [string[]]@($paths | Sort-Object -Unique)
    # Sort-Object is culture-aware (for example v3_* sorts before v31_*),
    # while the Python forward runner uses ordinal ordering.  The explicit
    # comparer gives deployment, verification and forward evidence one
    # cross-runtime source identity.
    [Array]::Sort($paths, [StringComparer]::Ordinal)
    $existing = @($paths | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) -PathType Leaf })
    $hashes = @()
    if ($existing.Count -gt 0) {
        $hashes = @($existing | & git -C $Root hash-object --no-filters --stdin-paths 2>$null)
        if ($LASTEXITCODE -ne 0 -or $hashes.Count -ne $existing.Count) {
            throw 'unable to hash application source files'
        }
    }
    $hashByPath = @{}
    for ($i = 0; $i -lt $existing.Count; $i++) {
        $hashByPath[$existing[$i]] = ([string]$hashes[$i]).Trim()
    }
    $manifest = New-Object System.Collections.Generic.List[string]
    $manifest.Add("HEAD`t$head")
    foreach ($path in $paths) {
        $hash = if ($hashByPath.ContainsKey($path)) { $hashByPath[$path] } else { 'deleted' }
        # Double quotes are intentional: a single-quoted PowerShell string
        # would retain `t literally instead of emitting a TAB.
        $manifest.Add(("{0}`t{1}" -f $path, $hash))
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($manifest -join "`n"))
        $digest = -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
    } finally {
        $sha.Dispose()
    }
    return ('{0}.tree.{1}' -f $head, $digest.Substring(0, 24))
}
