# Read-only audit of the app.py-owned strict strategy forward scheduler.
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$WebPort = 9900
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$OwnerPath = Join-Path $ProjectRoot '.cache\chanlun_scheduler\forward_execution_owner.json'
$QmtOwnerPath = Join-Path $ProjectRoot '.cache\chanlun_scheduler\qmt_execution_owner.json'
$Reasons = [Collections.Generic.List[string]]::new()

function Add-Reason([string]$Reason) {
    if (-not $Reasons.Contains($Reason)) { $Reasons.Add($Reason) }
}

function Get-JsonHttpDocument {
    param([Parameter(Mandatory = $true)][string]$Uri)

    # Preserve /readyz's JSON diagnostics even when unrelated cold-start
    # components correctly make the aggregate endpoint return HTTP 503.
    $request = [Net.HttpWebRequest]::Create($Uri)
    $request.Method = 'GET'
    $request.Timeout = 20000
    $request.ReadWriteTimeout = 20000
    $response = $null
    try {
        $response = $request.GetResponse()
    } catch [Net.WebException] {
        if ($null -eq $_.Exception.Response) { throw }
        $response = $_.Exception.Response
    }
    try {
        $stream = $response.GetResponseStream()
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
        try {
            $body = $reader.ReadToEnd()
        } finally {
            $reader.Dispose()
        }
    } finally {
        $response.Dispose()
    }
    if ([string]::IsNullOrWhiteSpace($body)) {
        throw 'app readiness returned an empty response'
    }
    return ($body | ConvertFrom-Json)
}

$forward = $null
$qmtRuntime = $null
$healthStatus = $null
try {
    $health = Get-JsonHttpDocument `
        -Uri ('http://127.0.0.1:{0}/readyz?market=a' -f $WebPort)
    $healthStatus = [string]$health.status
    $forward = $health.components.forward_scheduler
    $qmtRuntime = $health.components.qmt_runtime
} catch {
    Add-Reason 'APP_READINESS_UNAVAILABLE'
}
if ($null -eq $qmtRuntime) {
    Add-Reason 'APP_QMT_COMPONENT_UNAVAILABLE'
} else {
    if ($qmtRuntime.schema -ne 'chanlun-qmt-runtime-readiness') {
        Add-Reason 'APP_QMT_SCHEMA_MISMATCH'
    }
    if ($qmtRuntime.contract_id -ne 'chanlun-qmt-runtime/app-runtime-contract') {
        Add-Reason 'APP_QMT_CONTRACT_MISMATCH'
    }
    if ($qmtRuntime.execution_owner -ne 'APP_RUNTIME') {
        Add-Reason 'APP_QMT_OWNER_MISMATCH'
    }
    if ($qmtRuntime.ready -ne $true -or $qmtRuntime.configuration_ready -ne $true) {
        Add-Reason 'APP_QMT_CONFIGURATION_NOT_READY'
    }
    if (
        $qmtRuntime.real_account_accessed -ne $false -or
        $qmtRuntime.real_order_transport_enabled -ne $false -or
        $qmtRuntime.automated_order_authorized -ne $false -or
        $qmtRuntime.live_status -ne 'LIVE_DISABLED'
    ) {
        Add-Reason 'APP_QMT_SAFETY_MISMATCH'
    }
}
if ($null -eq $forward) {
    Add-Reason 'APP_FORWARD_COMPONENT_UNAVAILABLE'
} else {
    if ($forward.schema -ne 'chanlun-forward-scheduler-readiness') {
        Add-Reason 'APP_FORWARD_SCHEMA_MISMATCH'
    }
    if ($forward.contract_id -ne 'chanlun-forward-scheduler/app-runtime-contract') {
        Add-Reason 'APP_FORWARD_CONTRACT_MISMATCH'
    }
    if ($forward.execution_owner -ne 'APP_RUNTIME') {
        Add-Reason 'APP_FORWARD_OWNER_MISMATCH'
    }
    if ($forward.ready -ne $true -or $forward.configuration_ready -ne $true) {
        Add-Reason 'APP_FORWARD_CONFIGURATION_NOT_READY'
    }
    if (
        $forward.real_account_accessed -ne $false -or
        $forward.real_order_transport_enabled -ne $false -or
        $forward.automated_order_authorized -ne $false -or
        $forward.live_status -ne 'LIVE_DISABLED'
    ) {
        Add-Reason 'APP_FORWARD_SAFETY_MISMATCH'
    }
}

$owner = $null
if (-not (Test-Path -LiteralPath $OwnerPath -PathType Leaf)) {
    Add-Reason 'APP_FORWARD_OWNER_RECEIPT_MISSING'
} else {
    try {
        $owner = Get-Content -LiteralPath $OwnerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $heartbeat = [DateTimeOffset]::Parse(
            [string]$owner.heartbeat_at,
            [Globalization.CultureInfo]::InvariantCulture
        )
        if (([DateTimeOffset]::Now - $heartbeat).Duration().TotalMinutes -gt 15) {
            Add-Reason 'APP_FORWARD_OWNER_HEARTBEAT_STALE'
        }
        if (
            $owner.schema -ne 'chanlun-forward-execution-owner' -or
            $owner.owner -ne 'APP_RUNTIME' -or
            $owner.live_status -ne 'LIVE_DISABLED'
        ) {
            Add-Reason 'APP_FORWARD_OWNER_RECEIPT_INVALID'
        }
        $null = Get-Process -Id ([int]$owner.pid) -ErrorAction Stop
    } catch {
        Add-Reason 'APP_FORWARD_OWNER_PROCESS_UNAVAILABLE'
    }
}

$qmtOwner = $null
if (-not (Test-Path -LiteralPath $QmtOwnerPath -PathType Leaf)) {
    Add-Reason 'APP_QMT_OWNER_RECEIPT_MISSING'
} else {
    try {
        $qmtOwner = Get-Content -LiteralPath $QmtOwnerPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        $qmtHeartbeat = [DateTimeOffset]::Parse(
            [string]$qmtOwner.heartbeat_at,
            [Globalization.CultureInfo]::InvariantCulture
        )
        if (([DateTimeOffset]::Now - $qmtHeartbeat).Duration().TotalMinutes -gt 15) {
            Add-Reason 'APP_QMT_OWNER_HEARTBEAT_STALE'
        }
        if (
            $qmtOwner.schema -ne 'chanlun-qmt-execution-owner' -or
            $qmtOwner.contract_id -ne 'chanlun-qmt-runtime/app-runtime-contract' -or
            $qmtOwner.owner -ne 'APP_RUNTIME' -or
            $qmtOwner.live_status -ne 'LIVE_DISABLED'
        ) {
            Add-Reason 'APP_QMT_OWNER_RECEIPT_INVALID'
        }
        $null = Get-Process -Id ([int]$qmtOwner.pid) -ErrorAction Stop
        if ($null -ne $owner -and [int]$qmtOwner.pid -ne [int]$owner.pid) {
            Add-Reason 'APP_RUNTIME_OWNER_PID_MISMATCH'
        }
    } catch {
        Add-Reason 'APP_QMT_OWNER_PROCESS_UNAVAILABLE'
    }
}

$ready = $Reasons.Count -eq 0
$result = [ordered]@{
    schema = 'chanlun-app-forward-scheduler-readiness'
    observed_at = (Get-Date).ToString(
        'yyyy-MM-ddTHH:mm:ss.ffffffK',
        [Globalization.CultureInfo]::InvariantCulture
    )
    ready = $ready
    status = if ($ready) { 'ready' } else { 'not_ready' }
    reason_code = if ($ready) { 'READY' } else { $Reasons[0] }
    reason_codes = @($Reasons)
    execution_owner = if ($null -eq $forward) { $null } else { [string]$forward.execution_owner }
    app_health_status = $healthStatus
    app_forward = $forward
    app_qmt_runtime = $qmtRuntime
    owner_receipt = if ($null -eq $owner) { $null } else { $OwnerPath }
    owner_pid = if ($null -eq $owner) { $null } else { [int]$owner.pid }
    qmt_owner_receipt = if ($null -eq $qmtOwner) { $null } else { $QmtOwnerPath }
    qmt_owner_pid = if ($null -eq $qmtOwner) { $null } else { [int]$qmtOwner.pid }
    real_account_accessed = $false
    real_order_transport_enabled = $false
    automated_order_authorized = $false
    live_status = 'LIVE_DISABLED'
}
$result | ConvertTo-Json -Depth 12 -Compress
if (-not $ready) { exit 3 }
