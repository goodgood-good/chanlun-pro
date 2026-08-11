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

function Get-ApplicationFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    # Use framework primitives instead of Get-FileHash.  The latter belongs to
    # Microsoft.PowerShell.Utility and is absent from some minimal Windows CI
    # hosts even when powershell.exe itself is available.
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::ReadWrite
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') })
    } finally {
        $sha.Dispose()
        $stream.Dispose()
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
    # tools/run_forward_paper.py::FORWARD_PIPELINE_TOOL_PATHS.  These
    # subprocesses execute decision/PIT code from disk and therefore belong to
    # the deployed application identity; unrelated maintenance tools do not.
    $forwardPipelineTools = @(
        'tools/audit_qmt_warmup_convergence.py',
        'tools/run_forward_paper.py',
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
    # Sort-Object is culture-aware (for example * sorts before *),
    # while the Python forward runner uses ordinal ordering.  The explicit
    # comparer gives deployment, verification and forward evidence one
    # cross-runtime source identity.
    [Array]::Sort($paths, [StringComparer]::Ordinal)
    $existing = @($paths | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) -PathType Leaf })
    # Do not pipe path names into a native executable. Windows PowerShell 5.1
    # may prefix that native stdin stream with a BOM; Git then interprets the
    # BOM as part of the first path and fails only on some runner code pages.
    # Direct SHA-256 content hashes are stable across PowerShell/Python and
    # still bind the exact bytes used by the running application.
    $hashByPath = @{}
    foreach ($path in $existing) {
        try {
            $hashByPath[$path] = Get-ApplicationFileSha256 -Path (Join-Path $Root $path)
        } catch {
            throw "unable to hash application source file '$path': $($_.Exception.Message)"
        }
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
