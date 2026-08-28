[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [Parameter(Mandatory = $true)]
    [string]$QmtDataDir,
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$TargetSession = (Get-Date -Format "yyyy-MM-dd"),
    [ValidateRange(1, 168)]
    [int]$MaxWaitHours = 36,
    [ValidateRange(15, 300)]
    [int]$PollSeconds = 60,
    [ValidateRange(1, 16)]
    [int]$Workers = 16,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 9900,
    [switch]$RestartWebAfterCompletion
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$QmtDataDir = (Resolve-Path -LiteralPath $QmtDataDir).Path
$backtestScript = Join-Path $ProjectRoot "ops\run_historical_backtest.ps1"
$restartScript = Join-Path $ProjectRoot "ops\restart_web.ps1"
if (-not (Test-Path -LiteralPath $backtestScript -PathType Leaf)) {
    throw "Historical backtest launcher is missing: $backtestScript"
}
if (-not (Test-Path -LiteralPath $QmtDataDir -PathType Container)) {
    throw "QMT data directory is unavailable: $QmtDataDir"
}
if (
    $RestartWebAfterCompletion -and
    -not (Test-Path -LiteralPath $restartScript -PathType Leaf)
) {
    throw "Web restart launcher is missing: $restartScript"
}

$targetDate = [DateTime]::ParseExact($TargetSession, "yyyy-MM-dd", $null).Date
$deadline = [DateTimeOffset]::Now.AddHours($MaxWaitHours)
$readyUri = "http://127.0.0.1:$WebPort/readyz"

Write-Output (
    "[{0}] Waiting for zero-failure post-close full coverage for {1}" -f
        [DateTimeOffset]::Now.ToString("o"),
        $TargetSession
)
while ([DateTimeOffset]::Now -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri $readyUri -TimeoutSec 15
        $screening = $health.components.trading_screening
        $marketDataAsOf = $null
        $marketDataText = [string]$screening.daily_preselection_market_data_as_of
        if (-not [string]::IsNullOrWhiteSpace($marketDataText)) {
            $marketDataAsOf = [DateTimeOffset]::Parse($marketDataText)
        }
        $postCloseTarget = (
            $null -ne $marketDataAsOf -and
            $marketDataAsOf.Date -eq $targetDate -and
            $marketDataAsOf.TimeOfDay -ge [TimeSpan]::FromHours(15)
        )
        $coverageComplete = (
            [string]$screening.full_coverage_state -eq "complete" -and
            $screening.coverage_cycle_complete -eq $true -and
            [int]$screening.coverage_cycle_failed_symbol_count -eq 0
        )
        Write-Output (
            "[{0}] coverage={1} complete={2} market_data={3} failed={4}" -f
                [DateTimeOffset]::Now.ToString("o"),
                $screening.full_coverage_state,
                $screening.coverage_cycle_complete,
                $marketDataAsOf,
                $screening.coverage_cycle_failed_symbol_count
        )
        if ($postCloseTarget -and $coverageComplete) {
            break
        }
    } catch {
        Write-Output (
            "[{0}] readiness unavailable: {1}" -f
                [DateTimeOffset]::Now.ToString("o"),
                $_.Exception.Message
        )
    }
    Start-Sleep -Seconds $PollSeconds
}
if ([DateTimeOffset]::Now -ge $deadline) {
    throw "Timed out waiting for the post-close full-coverage snapshot."
}

Set-Location -LiteralPath $ProjectRoot
Write-Output (
    "[{0}] Starting explicit full-market six-month causal backtest" -f
        [DateTimeOffset]::Now.ToString("o")
)
& powershell.exe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $backtestScript `
    -Workers $Workers `
    -QmtDataDir $QmtDataDir `
    -Stage All `
    -GeneratePIT `
    -FullMarket `
    -ConfirmLargeScope
if ($LASTEXITCODE -ne 0) {
    throw "Full-market historical backtest failed with exit code $LASTEXITCODE"
}

if ($RestartWebAfterCompletion) {
    Write-Output (
        "[{0}] Backtest complete; restarting Web to publish the new verdict" -f
            [DateTimeOffset]::Now.ToString("o")
    )
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $restartScript `
        -EnableLargeScreeningScope `
        -EnableLargeHoldingMonitorScope `
        -EnableFullSymbolCatalog `
        -EnableFullCoverage `
        -ForceFullCoverageUntilComplete `
        -WebReadinessTimeoutSeconds 1800
    if ($LASTEXITCODE -ne 0) {
        throw "Web restart after backtest failed with exit code $LASTEXITCODE"
    }
}

Write-Output (
    "[{0}] Full-market backtest orchestration complete" -f
        [DateTimeOffset]::Now.ToString("o")
)
