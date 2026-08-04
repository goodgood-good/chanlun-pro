[CmdletBinding()]
param(
    # Windows PowerShell 5.1 does not reliably populate $PSScriptRoot while a
    # parameter default expression is being bound for ``powershell -File``.
    # Resolve the implicit root after binding, when the automatic variable is
    # authoritative.
    [string]$ProjectRoot = '',
    [string]$HealthUri = 'http://127.0.0.1:9900/readyz?market=a',
    [string]$ExpectedRevision = '',
    [string]$ExpectedSourceRevision = '',
    [int]$ExpectedProcessId = 0,
    [switch]$SkipProcessCheck,
    [switch]$SkipFreshnessCheck,
    [switch]$SkipSourceCheck
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
. (Join-Path $PSScriptRoot 'deploy_common.ps1')
$ok = $true

try {
    $resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    Push-Location $resolvedRoot
    try {
        if (-not $SkipSourceCheck) {
            $currentSourceRevision = Get-ApplicationSourceRevision -Root $resolvedRoot
            Write-Output "INFO: current source revision=$currentSourceRevision"
            if ([string]::IsNullOrWhiteSpace($ExpectedSourceRevision)) {
                if (-not [string]::IsNullOrWhiteSpace($ExpectedRevision)) {
                    Write-Output 'FAIL: ExpectedSourceRevision is required when ExpectedRevision is supplied'
                    $ok = $false
                }
                $ExpectedSourceRevision = $currentSourceRevision
            } elseif ($currentSourceRevision -ne $ExpectedSourceRevision) {
                Write-Output "FAIL: current source revision '$currentSourceRevision' does not match expected '$ExpectedSourceRevision'"
                $ok = $false
            }
            if ([string]::IsNullOrWhiteSpace($ExpectedRevision)) {
                $ExpectedRevision = $ExpectedSourceRevision
            } elseif (
                $ExpectedRevision -ne $ExpectedSourceRevision -and
                -not $ExpectedRevision.StartsWith("$ExpectedSourceRevision.run.", [StringComparison]::Ordinal)
            ) {
                Write-Output "FAIL: expected deployment revision '$ExpectedRevision' is not derived from source '$ExpectedSourceRevision'"
                $ok = $false
            }
        } elseif ([string]::IsNullOrWhiteSpace($ExpectedRevision)) {
            throw 'ExpectedRevision is required when SkipSourceCheck is used'
        }

        $process = $null
        $portOwnerIds = @()
        if (-not $SkipProcessCheck) {
            $healthAddress = [Uri]$HealthUri
            if ($healthAddress.Port -lt 1 -or $healthAddress.Port -gt 65535) {
                throw "HealthUri has an invalid port: $HealthUri"
            }
            $portOwnerIds = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { [int]$_.LocalPort -eq $healthAddress.Port } |
                Select-Object -ExpandProperty OwningProcess -Unique)
            if ($portOwnerIds.Count -eq 0) {
                Write-Output "FAIL: no process owns readiness port $($healthAddress.Port)"
                $ok = $false
            }

            $processId = 0
            if ($ExpectedProcessId -gt 0) {
                $processId = $ExpectedProcessId
                if ($portOwnerIds -notcontains $ExpectedProcessId) {
                    Write-Output "FAIL: expected web process PID '$ExpectedProcessId' does not own readiness port $($healthAddress.Port)"
                    $ok = $false
                }
            } elseif ($portOwnerIds.Count -eq 1) {
                $processId = [int]$portOwnerIds[0]
            } elseif ($portOwnerIds.Count -gt 1) {
                Write-Output "FAIL: readiness port has multiple owning processes: $($portOwnerIds -join ',')"
                $ok = $false
            }

            if ($processId -gt 0) {
                $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
                if ($null -eq $process) {
                    Write-Output "FAIL: web process PID=$processId was not found"
                    $ok = $false
                } else {
                    $appScript = Join-Path $resolvedRoot 'web\chanlun_chart\app.py'
                    $appPattern = '(?i)(?:^|[\s"])' + [regex]::Escape($appScript) + '(?:[\s"]|$)'
                    if (-not $process.CommandLine -or $process.CommandLine -notmatch $appPattern) {
                        Write-Output "FAIL: readiness port owner PID=$processId is not the expected web process ($appScript)"
                        $ok = $false
                    } else {
                        Write-Output "OK: exact web process PID=$processId owns readiness port $($healthAddress.Port)"
                    }
                    if (-not $SkipFreshnessCheck) {
                        try {
                            $startedAt = ConvertTo-ProcessStartDate $process.CreationDate
                            $restartGate = (Get-Date).Date.AddHours(8.5)
                            if ((Get-Date) -gt $restartGate -and $startedAt -lt $restartGate) {
                                Write-Output "FAIL: web process started before today's 08:30 gate ($startedAt)"
                                $ok = $false
                            }
                        } catch {
                            Write-Output "FAIL: unable to verify web process start time ($($_.Exception.Message))"
                            $ok = $false
                        }
                    }
                }
            }
        }

        try {
            $health = Invoke-RestMethod -Uri $HealthUri -Method Get -TimeoutSec 10
            if ($health.status -ne 'ready') {
                Write-Output "FAIL: readiness status is '$($health.status)'"
                $ok = $false
            } elseif ($health.revision -ne $ExpectedRevision) {
                Write-Output "FAIL: running revision '$($health.revision)' does not match expected '$ExpectedRevision'"
                $ok = $false
            } elseif (-not $SkipProcessCheck -and [string]$health.pid -ne [string]$process.ProcessId) {
                Write-Output "FAIL: readiness PID '$($health.pid)' does not match web process '$($process.ProcessId)'"
                $ok = $false
            } elseif (-not $SkipProcessCheck -and $portOwnerIds -notcontains [int]$health.pid) {
                Write-Output "FAIL: readiness PID '$($health.pid)' does not own the readiness port"
                $ok = $false
            } else {
                Write-Output "OK: readiness endpoint revision=$($health.revision) pid=$($health.pid)"
            }
        } catch {
            Write-Output "FAIL: readiness endpoint unavailable ($($_.Exception.Message))"
            $ok = $false
        }
    } finally {
        Pop-Location
    }
} catch {
    Write-Output "FAIL: deployment verifier error ($($_.Exception.Message))"
    $ok = $false
}

if ($ok) {
    Write-Output 'DEPLOY-OK'
    exit 0
}
Write-Output 'DEPLOY-CHECK-FAILED'
exit 1
