[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 9900,
    [ValidateRange(2, 300)]
    [int]$PollSeconds = 10,
    [ValidateRange(1, 30)]
    [int]$FailureThreshold = 3,
    [ValidateRange(1, 60)]
    [int]$ReadinessFailureThreshold = 6,
    [ValidateRange(5, 120)]
    [int]$LivenessTimeoutSeconds = 15,
    [ValidateRange(5, 120)]
    [int]$ReadinessTimeoutSeconds = 15,
    [ValidateRange(1, 120)]
    [int]$StartupReadinessFailureThreshold = 24,
    [ValidateRange(10, 3600)]
    [int]$RestartCooldownSeconds = 60,
    [ValidateSet('a')]
    [string]$ReadinessMarket = 'a',


    [switch]$EnableFullSymbolCatalog,


    [switch]$Once
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$restartScript = Join-Path $ProjectRoot 'ops\restart_web.ps1'
$stateRoot = Join-Path $ProjectRoot '.cache\chanlun_web_watchdog'
$logRoot = Join-Path $ProjectRoot 'ops\logs'
$heartbeatPath = Join-Path $stateRoot 'heartbeat.json'
$lockPath = Join-Path $stateRoot 'watchdog.lock'
$deploymentScopePath = Join-Path $stateRoot 'deployment_scope.json'
$liveUri = 'http://127.0.0.1:{0}/livez' -f $WebPort
$healthUri = 'http://127.0.0.1:{0}/readyz?market={1}' -f `
    $WebPort, [Uri]::EscapeDataString($ReadinessMarket)

foreach ($directory in @($stateRoot, $logRoot)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}
if (-not (Test-Path -LiteralPath $restartScript -PathType Leaf)) {
    throw "restart script is unavailable: $restartScript"
}

function Write-WatchdogLog([string]$Message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    $path = Join-Path $logRoot ('web_watchdog_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
    Add-Content -LiteralPath $path -Value $line -Encoding UTF8
}

$scopeSwitchNames = @('EnableFullSymbolCatalog')
$explicitScope = @(
    $scopeSwitchNames | Where-Object { $PSBoundParameters.ContainsKey($_) }
).Count -gt 0
$scopeSource = if ($explicitScope) { 'explicit' } else { 'default_bounded' }
if (-not $explicitScope -and (Test-Path -LiteralPath $deploymentScopePath -PathType Leaf)) {
    try {
        $deploymentScope = Get-Content `
            -LiteralPath $deploymentScopePath `
            -Raw `
            -Encoding UTF8 | ConvertFrom-Json
        if ($deploymentScope.schema -ne 'chanlun-web-watchdog-deployment-scope-v1') {
            throw 'schema mismatch'
        }
        $persistedRoot = [IO.Path]::GetFullPath(
            [string]$deploymentScope.project_root
        )
        if (-not [string]::Equals(
            $persistedRoot.TrimEnd('\'),
            $ProjectRoot.TrimEnd('\'),
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'project root mismatch'
        }
        if ([int]$deploymentScope.web_port -ne $WebPort) {
            throw 'web port mismatch'
        }
        $scopeProperties = @([pscustomobject]@{ Parameter = 'EnableFullSymbolCatalog'; Property = 'enable_full_symbol_catalog' })
        foreach ($scopeProperty in $scopeProperties) {
            if (
                $deploymentScope.PSObject.Properties.Name -notcontains `
                    $scopeProperty.Property
            ) {
                throw ('missing property: {0}' -f $scopeProperty.Property)
            }
            $value = $deploymentScope.($scopeProperty.Property)
            if ($value -isnot [bool]) {
                throw ('property is not boolean: {0}' -f $scopeProperty.Property)
            }
            Set-Variable -Name $scopeProperty.Parameter -Value ([bool]$value)
        }

        $scopeSource = 'persisted'
    } catch {
        Write-WatchdogLog (
            'ERROR: persisted deployment scope is invalid: {0}' -f `
                $_.Exception.Message
        )
        throw
    }
}

function Write-WatchdogHeartbeat {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][int]$ConsecutiveFailures,
        [AllowNull()][string]$Detail,
        [bool]$RecoveryRecommended = $false,
        [AllowNull()]$AppPid
    )
    $payload = [ordered]@{
        schema = 'chanlun-web-watchdog-heartbeat'
        observed_at = (Get-Date).ToString('o')
        watchdog_pid = $PID
        web_port = $WebPort
        live_uri = $liveUri
        health_uri = $healthUri
        status = $Status
        consecutive_failures = $ConsecutiveFailures
        recovery_recommended = $RecoveryRecommended
        app_pid = $AppPid





        scope_source = $scopeSource
        deployment_scope = [ordered]@{


            enable_full_symbol_catalog = [bool]$EnableFullSymbolCatalog

        }
        detail = $Detail
    }
    $temporary = '{0}.{1}.tmp' -f $heartbeatPath, $PID
    [IO.File]::WriteAllText(
        $temporary,
        (($payload | ConvertTo-Json -Depth 4 -Compress) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
    Move-Item -LiteralPath $temporary -Destination $heartbeatPath -Force
}

function Get-JsonHttpResponse {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    # Windows PowerShell's Invoke-RestMethod throws before returning the JSON
    # body for HTTP 503.  /readyz deliberately uses 503 for structured
    # readiness states (metadata warm-up or missing quotes); discarding
    # that body turns every normal warm-up into an indistinguishable transport
    # failure and creates a destructive restart loop.
    $request = [Net.HttpWebRequest]::Create($Uri)
    $request.Method = 'GET'
    $request.Timeout = $TimeoutSeconds * 1000
    $request.ReadWriteTimeout = $TimeoutSeconds * 1000
    $response = $null
    try {
        try {
            $response = $request.GetResponse()
        } catch [Net.WebException] {
            if ($null -eq $_.Exception.Response) {
                throw
            }
            $response = $_.Exception.Response
        }
        $stream = $response.GetResponseStream()
        if ($null -eq $stream) {
            throw 'HTTP response has no body'
        }
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
        try {
            $body = $reader.ReadToEnd()
        } finally {
            $reader.Dispose()
        }
        if ([string]::IsNullOrWhiteSpace($body)) {
            throw 'HTTP response body is empty'
        }
        return ($body | ConvertFrom-Json)
    } finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
    }
}

function Test-WebHealth {
    try {
        $live = Invoke-RestMethod -Uri $liveUri -Method Get -TimeoutSec $LivenessTimeoutSeconds
        if ($live.status -ne 'alive') { throw 'live endpoint returned a non-alive status' }
    } catch {
        return [pscustomobject]@{Healthy=$false; FailureClass='liveness_failed'; RecoveryRecommended=$true; Detail=$_.Exception.Message; AppPid=$null}
    }
    try {
        $health = Get-JsonHttpResponse -Uri $healthUri -TimeoutSeconds $ReadinessTimeoutSeconds
    } catch {
        return [pscustomobject]@{Healthy=$false; FailureClass='readiness_failed'; RecoveryRecommended=$true; Detail=$_.Exception.Message; AppPid=$null}
    }
    $ready = $health.status -eq 'ready'
    # Data-provider availability is not repaired by repeatedly restarting the Web app.
    $runtimeFailed = ($health.components.runtime.required -eq $true -and $health.components.runtime.ready -ne $true) -or ($health.components.scheduler.required -eq $true -and $health.components.scheduler.ready -ne $true)
    return [pscustomobject]@{
        Healthy = $ready
        FailureClass = $(if ($ready) {'healthy'} elseif ($runtimeFailed) {'readiness_failed'} else {'operational_degraded'})
        RecoveryRecommended = $runtimeFailed
        Detail = ('pid={0}; revision={1}; reasons={2}' -f $health.pid, $health.revision, ($health.reasons -join ','))
        AppPid = $health.pid
    }
}

function Invoke-WebRecovery {
    Write-WatchdogLog ('recovery requested after health failure: {0}' -f $healthUri)
    # Do not capture restart output here. A long-running Web descendant can keep
    # the capture pipe open after the restart script exits and deadlock recovery.
    # The deployment has its own log; wait only for the direct child exit code.
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        ('"{0}"' -f $restartScript),
        '-SkipWatchdog',
        '-WebReadinessTimeoutSeconds',
        '1800'
    )

    if ($EnableFullSymbolCatalog) {
        $arguments += '-EnableFullSymbolCatalog'
    }

    $restartProcess = Start-Process `
        -FilePath 'powershell.exe' `
        -ArgumentList $arguments `
        -WindowStyle Hidden `
        -PassThru
    $restartProcess.WaitForExit()
    $exitCode = $restartProcess.ExitCode
    if ($exitCode -ne 0) {
        Write-WatchdogLog ('recovery failed with exit code {0}' -f $exitCode)
        return $false
    }
    Write-WatchdogLog 'recovery completed successfully'
    return $true
}

$lockStream = $null
try {
    $lockStream = [IO.File]::Open(
        $lockPath,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
} catch {
    # The file handle provides cross-session exclusion for this project watcher.
    exit 0
}
if ($null -eq $lockStream) {
    exit 0
}
$lockPayload = [Text.Encoding]::UTF8.GetBytes(
    ('pid={0}; port={1}; started_at={2}' -f $PID, $WebPort, (Get-Date).ToString('o'))
)
$lockStream.SetLength(0)
$lockStream.Write($lockPayload, 0, $lockPayload.Length)
$lockStream.Flush()

try {
    Write-WatchdogLog ('watchdog started; pid={0}; catalog={1}' -f $PID, [bool]$EnableFullSymbolCatalog)
    $consecutiveFailures = 0
    $lastFailureClass = $null
    $lastObservedAppPid = $null
    $lastRestartAt = [datetime]::MinValue
    while ($true) {
        $probe = Test-WebHealth
        $observedAppPid = [string]$probe.AppPid
        if (
            -not [string]::IsNullOrWhiteSpace($observedAppPid) -and
            -not [string]::IsNullOrWhiteSpace([string]$lastObservedAppPid) -and
            $observedAppPid -ne [string]$lastObservedAppPid
        ) {
            # A replacement process must receive its own complete startup budget;
            # failures accumulated by the prior PID cannot be inherited.
            Write-WatchdogLog (
                'application PID changed {0} -> {1}; reset failure budget' -f `
                    $lastObservedAppPid, $observedAppPid
            )
            $consecutiveFailures = 0
            $lastFailureClass = $null
        }
        if (-not [string]::IsNullOrWhiteSpace($observedAppPid)) {
            $lastObservedAppPid = $observedAppPid
        }
        if ($probe.Healthy) {
            $consecutiveFailures = 0
            $lastFailureClass = $null
            Write-WatchdogHeartbeat `
                -Status 'healthy' `
                -ConsecutiveFailures 0 `
                -Detail $probe.Detail `
                -AppPid $probe.AppPid





        } else {
            if ($lastFailureClass -ne $probe.FailureClass) {
                $consecutiveFailures = 0
                $lastFailureClass = $probe.FailureClass
            }
            $consecutiveFailures += 1
            Write-WatchdogHeartbeat `
                -Status $probe.FailureClass `
                -ConsecutiveFailures $consecutiveFailures `
                -Detail $probe.Detail `
                -RecoveryRecommended $probe.RecoveryRecommended `
                -AppPid $probe.AppPid





            $threshold = switch ($probe.FailureClass) {
                'liveness_failed' { $FailureThreshold; break }
                'startup_readiness_failed' {
                    $StartupReadinessFailureThreshold
                    break
                }
                default { $ReadinessFailureThreshold; break }
            }
            if (
                $probe.RecoveryRecommended -and
                $consecutiveFailures -ge $threshold
            ) {
                $cooldownElapsed = ((Get-Date) - $lastRestartAt).TotalSeconds
                if ($cooldownElapsed -ge $RestartCooldownSeconds) {
                    $lastRestartAt = Get-Date
                    Write-WatchdogHeartbeat `
                        -Status 'recovering' `
                        -ConsecutiveFailures $consecutiveFailures `
                        -Detail $probe.Detail `
                        -RecoveryRecommended $true `
                        -AppPid $probe.AppPid





                    $recovered = Invoke-WebRecovery
                    $consecutiveFailures = 0
                    $recoveryStatus = if ($recovered) {
                        'recovered'
                    } else {
                        'recovery_failed'
                    }
                    Write-WatchdogHeartbeat `
                        -Status $recoveryStatus `
                        -ConsecutiveFailures 0 `
                        -Detail $probe.Detail `
                        -RecoveryRecommended (-not $recovered) `
                        -AppPid $probe.AppPid





                }
            }
        }
        if ($Once) {
            if ($probe.Healthy) {
                exit 0
            }
            exit 1
        }
        Start-Sleep -Seconds $PollSeconds
    }
} finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
