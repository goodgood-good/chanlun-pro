param(
    [ValidateRange(1, 6)]
    [int]$Workers = 6,
    [string]$QmtDataDir = "",
    [ValidateRange(1, 5)]
    [int]$ExtractionAttempts = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$inputDirectory = Join-Path $repoRoot "audit\chanlun_trading_system_backtest\fixed_year_2025_2026"
$pitSnapshot = Join-Path $inputDirectory "pit_metadata.json"
$report = Join-Path $repoRoot "audit\chanlun_trading_system_backtest\certified_report.json"
$logsDirectory = Join-Path $repoRoot "ops\logs"
$lockPath = Join-Path $logsDirectory "historical_backtest.lock"

New-Item -ItemType Directory -Path $logsDirectory -Force | Out-Null
if (-not (Test-Path -LiteralPath $pitSnapshot -PathType Leaf)) {
    throw "PIT metadata snapshot is missing: $pitSnapshot"
}

if ([string]::IsNullOrWhiteSpace($QmtDataDir)) {
    $QmtDataDir = [Environment]::GetEnvironmentVariable("CHANLUN_QMT_LOCAL_DATA_DIR")
}
if ([string]::IsNullOrWhiteSpace($QmtDataDir) -or -not (Test-Path -LiteralPath $QmtDataDir -PathType Container)) {
    throw "A valid QMT data directory is required."
}
$env:CHANLUN_QMT_LOCAL_DATA_DIR = (Resolve-Path -LiteralPath $QmtDataDir).Path
$pythonPath = (Get-Command python -ErrorAction Stop).Source

try {
    $lockStream = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
} catch [System.IO.IOException] {
    throw "Another historical backtest pipeline is already running."
}

function Invoke-PythonStage {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Starting $Label"
    & $pythonPath $Script @Arguments
    $stageExitCode = $LASTEXITCODE
    if ($stageExitCode -ne 0) {
        throw "$Label failed with exit code $stageExitCode"
    }
    Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Completed $Label"
}

try {
    Push-Location -LiteralPath $repoRoot
    try {
        $extractionComplete = $false
        for ($attempt = 1; $attempt -le $ExtractionAttempts; $attempt++) {
            Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Starting causal fact extraction attempt $attempt/$ExtractionAttempts"
            & $pythonPath "tools\backtest_qmt_fixed_year.py" `
                "--workers" "$Workers" `
                "--pit-snapshot" $pitSnapshot `
                "--output-dir" $inputDirectory
            $extractExitCode = $LASTEXITCODE
            if ($extractExitCode -eq 0) {
                $extractionComplete = $true
                break
            }
            if ($extractExitCode -ne 2 -or $attempt -eq $ExtractionAttempts) {
                throw "Causal fact extraction failed with exit code $extractExitCode"
            }
            Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Retrying only failed or unfinished symbols"
        }
        if (-not $extractionComplete) {
            throw "Causal fact extraction did not complete"
        }

        Invoke-PythonStage `
            -Label "prefix causality audit" `
            -Script "tools\audit_qmt_prefix_invariance.py" `
            -Arguments @("--input-dir", $inputDirectory, "--workers", "$Workers")

        Invoke-PythonStage `
            -Label "certified portfolio finalization" `
            -Script "tools\finalize_qmt_pit_fixed_year.py" `
            -Arguments @("--input-dir", $inputDirectory, "--report", $report)
    } finally {
        Pop-Location
    }
} finally {
    $lockStream.Dispose()
}

Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Historical backtest pipeline completed"
