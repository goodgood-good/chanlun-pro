param(
    [ValidateRange(1, 16)]
    [int]$Workers = 16,
    [string]$QmtDataDir = "",
    [ValidateRange(1, 5)]
    [int]$ExtractionAttempts = 3,
    [ValidateSet("smoke2", "validation12", "diagnostic32")]
    [string]$Profile = "smoke2",
    [ValidateSet("Extract", "Prefix", "Finalize", "All")]
    [string]$Stage = "Extract",
    [switch]$GeneratePIT,
    [string]$MembershipCheckpointDir = "",
    [string]$MembershipIndex = "",
    [switch]$ConfirmLargeScope,
    [switch]$FullMarket,
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$WarmupStart = "2025-11-01",
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$RequestedStart = "2026-01-25",
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$EffectiveStart = "2026-01-26",
    [ValidatePattern("^\d{4}-\d{2}-\d{2}$")]
    [string]$RequestedEnd = "2026-07-24"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$backtestAuditRoot = Join-Path $repoRoot "audit\chanlun_trading_system_backtest"
$fullInputDirectory = Join-Path $backtestAuditRoot "full_market_explicit"
$pitReferenceDirectory = Join-Path $backtestAuditRoot "pit_reference"
$profileDefinitions = @{
    smoke2 = @{
        ExpectedCount = 2
        MaxSectorCount = 2
        MaxSectorClosure = 900
        CodesPath = "config\research_backtest_smoke_2.txt"
        OutputPath = "audit\chanlun_trading_system_backtest\research_sample_smoke_2"
    }
    validation12 = @{
        ExpectedCount = 12
        MaxSectorCount = 3
        MaxSectorClosure = 200
        CodesPath = "config\research_backtest_validation_12.txt"
        OutputPath = "audit\chanlun_trading_system_backtest\research_sample_validation_12"
    }
    diagnostic32 = @{
        ExpectedCount = 32
        MaxSectorCount = 8
        MaxSectorClosure = 2200
        CodesPath = "config\research_backtest_diagnostic_32.txt"
        OutputPath = "audit\chanlun_trading_system_backtest\research_sample_diagnostic_32"
    }
}
$profileDefinition = $profileDefinitions[$Profile]
$researchInputDirectory = Join-Path $repoRoot $profileDefinition.OutputPath
$researchCodesPath = Join-Path $repoRoot $profileDefinition.CodesPath
$inputDirectory = if ($FullMarket) { $fullInputDirectory } else { $researchInputDirectory }
$pitSnapshot = Join-Path $inputDirectory "pit_metadata.json"
$report = if ($FullMarket) {
    Join-Path $repoRoot "audit\chanlun_trading_system_backtest\certified_report.json"
} else {
    Join-Path $researchInputDirectory "report.json"
}
$logsDirectory = Join-Path $repoRoot "ops\logs"
$lockPath = Join-Path $logsDirectory "historical_backtest.lock"

New-Item -ItemType Directory -Path $logsDirectory -Force | Out-Null
if ($FullMarket -and $PSBoundParameters.ContainsKey("Profile")) {
    throw "-FullMarket cannot be combined with an explicit -Profile."
}
if ($FullMarket -and -not $ConfirmLargeScope) {
    throw "-FullMarket also requires -ConfirmLargeScope."
}
$parsedWindow = @(
    [DateTime]::ParseExact($WarmupStart, "yyyy-MM-dd", $null),
    [DateTime]::ParseExact($RequestedStart, "yyyy-MM-dd", $null),
    [DateTime]::ParseExact($EffectiveStart, "yyyy-MM-dd", $null),
    [DateTime]::ParseExact($RequestedEnd, "yyyy-MM-dd", $null)
)
if (
    $parsedWindow[0] -gt $parsedWindow[1] -or
    $parsedWindow[1] -gt $parsedWindow[2] -or
    $parsedWindow[2] -gt $parsedWindow[3]
) {
    throw "Expected WarmupStart <= RequestedStart <= EffectiveStart <= RequestedEnd."
}

$researchCodes = @()
if (-not $FullMarket) {
    if (-not (Test-Path -LiteralPath $researchCodesPath -PathType Leaf)) {
        throw "Research sample is missing: $researchCodesPath"
    }
    $researchCodes = @(
        Get-Content -LiteralPath $researchCodesPath -Encoding UTF8 |
            ForEach-Object { $_.Trim().ToUpperInvariant() } |
            Where-Object { $_ -and -not $_.StartsWith("#") }
    )
    $expectedCount = [int]$profileDefinition.ExpectedCount
    $uniqueCount = ($researchCodes | Sort-Object -Unique).Count
    $invalidCodes = @(
        $researchCodes | Where-Object { $_ -notmatch "^(SH|SZ|BJ)\.\d{6}$" }
    )
    if (
        $researchCodes.Count -ne $expectedCount -or
        $uniqueCount -ne $expectedCount -or
        $invalidCodes.Count -gt 0
    ) {
        throw "Research profile '$Profile' must contain exactly $expectedCount unique normalized symbols."
    }
    if ($researchCodes.Count -gt 20 -and -not $ConfirmLargeScope) {
        throw "Research profile '$Profile' contains $($researchCodes.Count) symbols; re-run with -ConfirmLargeScope."
    }
}

if ($GeneratePIT -and $Stage -notin @("Extract", "All")) {
    throw "-GeneratePIT is only valid before an Extract or All stage."
}
if (
    $Stage -in @("Extract", "All") -and
    -not $GeneratePIT -and
    -not (Test-Path -LiteralPath $pitSnapshot -PathType Leaf)
) {
    throw "Profile-scoped PIT metadata is missing: $pitSnapshot. Re-run with -GeneratePIT after verifying the checkpoint directory."
}
if ($GeneratePIT) {
    $defaultCheckpointDir = Join-Path $pitReferenceDirectory "cninfo_memberships"
    $defaultMembershipIndex = Join-Path $pitReferenceDirectory "membership_index.json"
    if ($FullMarket) {
        $hasCheckpointDir = -not [string]::IsNullOrWhiteSpace($MembershipCheckpointDir)
        $hasMembershipIndex = -not [string]::IsNullOrWhiteSpace($MembershipIndex)
        if ($hasCheckpointDir -ne $hasMembershipIndex) {
            throw "Full-market checkpoint reuse requires both MembershipCheckpointDir and MembershipIndex."
        }
        if (
            -not $hasCheckpointDir -and
            (Test-Path -LiteralPath $defaultCheckpointDir -PathType Container) -and
            (Test-Path -LiteralPath $defaultMembershipIndex -PathType Leaf)
        ) {
            $MembershipCheckpointDir = $defaultCheckpointDir
            $MembershipIndex = $defaultMembershipIndex
        }
    } else {
        if ([string]::IsNullOrWhiteSpace($MembershipCheckpointDir)) {
            $MembershipCheckpointDir = $defaultCheckpointDir
        }
        if ([string]::IsNullOrWhiteSpace($MembershipIndex)) {
            $MembershipIndex = $defaultMembershipIndex
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($MembershipCheckpointDir)) {
        if (-not (Test-Path -LiteralPath $MembershipCheckpointDir -PathType Container)) {
            throw "A complete CNInfo membership checkpoint directory is required: $MembershipCheckpointDir"
        }
        if (-not (Test-Path -LiteralPath $MembershipIndex -PathType Leaf)) {
            throw "An immutable full-capture membership index is required: $MembershipIndex"
        }
        $MembershipCheckpointDir = (
            Resolve-Path -LiteralPath $MembershipCheckpointDir
        ).Path
        $MembershipIndex = (Resolve-Path -LiteralPath $MembershipIndex).Path
    } elseif (-not $FullMarket) {
        throw "Scoped PIT capture requires immutable membership checkpoints."
    }
}

if ($Stage -in @("Extract", "Finalize", "All")) {
    if ([string]::IsNullOrWhiteSpace($QmtDataDir)) {
        $QmtDataDir = [Environment]::GetEnvironmentVariable(
            "CHANLUN_QMT_LOCAL_DATA_DIR"
        )
    }
    if (
        [string]::IsNullOrWhiteSpace($QmtDataDir) -or
        -not (Test-Path -LiteralPath $QmtDataDir -PathType Container)
    ) {
        throw "A valid QMT data directory is required for extraction/finalization."
    }
    $env:CHANLUN_QMT_LOCAL_DATA_DIR = (
        Resolve-Path -LiteralPath $QmtDataDir
    ).Path
}
$pythonPath = (Get-Command python -ErrorAction Stop).Source

if ($FullMarket) {
    $validationDirectory = Join-Path `
        $repoRoot `
        "audit\chanlun_trading_system_backtest\research_sample_validation_12"
    & $pythonPath `
        (Join-Path $repoRoot "tools\verify_qmt_validation_gate.py") `
        "--directory" $validationDirectory `
        "--expected-symbol-count" "12"
    if ($LASTEXITCODE -ne 0) {
        throw "Full-market replay requires a current passed validation12 gate."
    }
}

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
        $scopeLabel = if ($FullMarket) {
            "FULL_MARKET_EXPLICIT"
        } else {
            "RESEARCH_PROFILE_$($Profile.ToUpperInvariant())_$($researchCodes.Count)"
        }
        Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Replay scope: $scopeLabel"
        Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Pipeline stage: $Stage"

        if ($GeneratePIT) {
            $pitArguments = @(
                "--output", $pitSnapshot,
                "--workers", "$([Math]::Min($Workers, 12))"
            )
            if ($FullMarket) {
                $pitArguments += @("--full-market", "--confirm-large-scope")
                if (-not [string]::IsNullOrWhiteSpace($MembershipCheckpointDir)) {
                    $pitArguments += @(
                        "--membership-checkpoint-dir", $MembershipCheckpointDir,
                        "--membership-index", $MembershipIndex
                    )
                }
            } else {
                $pitArguments += @(
                    "--codes-file", $researchCodesPath,
                    "--membership-checkpoint-dir", $MembershipCheckpointDir,
                    "--membership-index", $MembershipIndex
                )
                if ($ConfirmLargeScope) {
                    $pitArguments += "--confirm-large-scope"
                }
            }
            Invoke-PythonStage `
                -Label "profile-scoped PIT metadata capture" `
                -Script "tools\snapshot_qmt_pit_metadata.py" `
                -Arguments $pitArguments
        }

        if ($Stage -in @("Extract", "All")) {
            $extractionComplete = $false
            for ($attempt = 1; $attempt -le $ExtractionAttempts; $attempt++) {
                Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Starting causal fact extraction attempt $attempt/$ExtractionAttempts"
                $extractionArguments = @(
                    "--workers", "$Workers",
                    "--pit-snapshot", $pitSnapshot,
                    "--output-dir", $inputDirectory,
                    "--warmup-start", $WarmupStart,
                    "--start", $RequestedStart,
                    "--effective-start", $EffectiveStart,
                    "--end", $RequestedEnd
                )
                if (-not $FullMarket) {
                    $extractionArguments += @("--codes", ($researchCodes -join ","))
                    if ($researchCodes.Count -gt 20) {
                        $extractionArguments += "--confirm-large-scope"
                    }
                } else {
                    $extractionArguments += @(
                        "--full-market",
                        "--confirm-large-scope"
                    )
                }
                & $pythonPath "tools\backtest_qmt_fixed_year.py" @extractionArguments
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
        }

        if ($Stage -in @("Prefix", "All")) {
            Invoke-PythonStage `
                -Label "prefix causality audit" `
                -Script "tools\audit_qmt_prefix_invariance.py" `
                -Arguments @(
                    "--input-dir", $inputDirectory,
                    "--workers", "$([Math]::Min($Workers, 12))"
                )
        }

        if ($Stage -in @("Finalize", "All")) {
            $finalizationArguments = @(
                "--input-dir", $inputDirectory,
                "--report", $report,
                "--reuse-sector-cache",
                "--sector-workers", "$([Math]::Min($Workers, 3))"
            )
            if (-not $FullMarket) {
                $finalizationArguments += @(
                    "--max-sector-count", "$($profileDefinition.MaxSectorCount)",
                    "--max-sector-closure", "$($profileDefinition.MaxSectorClosure)"
                )
            } else {
                $finalizationArguments += "--confirm-large-sector-scope"
            }
            Invoke-PythonStage `
                -Label "certified portfolio finalization" `
                -Script "tools\finalize_qmt_pit_fixed_year.py" `
                -Arguments $finalizationArguments
        }
    } finally {
        Pop-Location
    }
} finally {
    $lockStream.Dispose()
}

Write-Output "[$([DateTimeOffset]::Now.ToString('o'))] Historical backtest stage completed: $Stage"
