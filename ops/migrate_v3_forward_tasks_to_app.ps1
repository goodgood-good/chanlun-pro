#Requires -RunAsAdministrator
# One-way operational hand-off from every legacy runtime scheduled task to the
# scheduler and QMT controller owned by the continuously running app.py process.
#
# This script fails before mutation unless /readyz proves the app-runtime
# contracts and their LIVE_DISABLED safety fields.  It does not stop QMT or
# app.py; it only removes redundant Windows task definitions after proof.
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$WebPort = 9900
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReceiptDir = Join-Path $ProjectRoot '.cache\chanlun_v3_scheduler'
$ReceiptPath = Join-Path $ReceiptDir 'forward_app_migration.json'
$OwnerPath = Join-Path $ReceiptDir 'forward_execution_owner.json'
$QmtOwnerPath = Join-Path $ReceiptDir 'qmt_execution_owner.json'
$TaskNames = @(
    'Chanlun-V3-Forward-Capture',
    'Chanlun-V3-Forward-Evaluate',
    'Chanlun-QMT-DailyRestart'
)

function Get-JsonHttpDocument {
    param([Parameter(Mandatory = $true)][string]$Uri)

    # Invoke-RestMethod throws away the useful JSON body on HTTP 503 in
    # Windows PowerShell 5.1.  /readyz intentionally uses 503 while unrelated
    # cold-start components are warming, even when the app-owned forward
    # component is already configuration-ready.  Read the response body for
    # every HTTP status and let the component contract decide.
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

if (-not (Test-Path -LiteralPath $OwnerPath -PathType Leaf)) {
    throw 'app forward owner receipt is missing; start/restart app.py before migration'
}

$owner = Get-Content -LiteralPath $OwnerPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (
    $owner.schema -ne 'chanlun-v3-forward-execution-owner/v1' -or
    $owner.owner -ne 'APP_RUNTIME' -or
    $owner.real_account_accessed -ne $false -or
    $owner.real_order_transport_enabled -ne $false -or
    $owner.automated_order_authorized -ne $false -or
    $owner.live_status -ne 'LIVE_DISABLED'
) {
    throw 'app forward owner receipt is invalid or unsafe'
}
try {
    $ownerProcess = Get-Process -Id ([int]$owner.pid) -ErrorAction Stop
} catch {
    throw 'app forward owner process is not running'
}
if ($ownerProcess.Id -ne [int]$owner.pid) {
    throw 'app forward owner process identity is inconsistent'
}

if (-not (Test-Path -LiteralPath $QmtOwnerPath -PathType Leaf)) {
    throw 'app QMT owner receipt is missing; start/restart app.py before migration'
}
$qmtOwner = Get-Content -LiteralPath $QmtOwnerPath -Raw -Encoding UTF8 |
    ConvertFrom-Json
if (
    $qmtOwner.schema -ne 'chanlun-qmt-execution-owner/v1' -or
    $qmtOwner.contract_id -ne 'chanlun-qmt-runtime/app-runtime-contract/v1' -or
    $qmtOwner.owner -ne 'APP_RUNTIME' -or
    $qmtOwner.real_account_accessed -ne $false -or
    $qmtOwner.real_order_transport_enabled -ne $false -or
    $qmtOwner.automated_order_authorized -ne $false -or
    $qmtOwner.live_status -ne 'LIVE_DISABLED'
) {
    throw 'app QMT owner receipt is invalid or unsafe'
}
try {
    $qmtOwnerProcess = Get-Process -Id ([int]$qmtOwner.pid) -ErrorAction Stop
} catch {
    throw 'app QMT owner process is not running'
}
if (
    $qmtOwnerProcess.Id -ne [int]$qmtOwner.pid -or
    [int]$qmtOwner.pid -ne [int]$owner.pid
) {
    throw 'forward and QMT runtime ownership do not belong to one app.py process'
}

$readyUri = 'http://127.0.0.1:{0}/readyz?market=a' -f $WebPort
try {
    $health = Get-JsonHttpDocument -Uri $readyUri
} catch {
    throw "app readiness is unavailable at $readyUri"
}
$forward = $health.components.forward_scheduler
$qmtRuntime = $health.components.qmt_runtime
if (
    $null -eq $forward -or
    $forward.schema -ne 'chanlun-v3-forward-scheduler-readiness/v1' -or
    $forward.contract_id -ne 'chanlun-v3-forward-scheduler/app-runtime-contract/v1' -or
    $forward.execution_owner -ne 'APP_RUNTIME' -or
    $forward.ready -ne $true -or
    $forward.configuration_ready -ne $true -or
    $forward.real_account_accessed -ne $false -or
    $forward.real_order_transport_enabled -ne $false -or
    $forward.automated_order_authorized -ne $false -or
    $forward.live_status -ne 'LIVE_DISABLED'
) {
    throw 'app-owned forward scheduler is not configuration-ready; no task was changed'
}
if (
    $null -eq $qmtRuntime -or
    $qmtRuntime.schema -ne 'chanlun-qmt-runtime-readiness/v1' -or
    $qmtRuntime.contract_id -ne 'chanlun-qmt-runtime/app-runtime-contract/v1' -or
    $qmtRuntime.execution_owner -ne 'APP_RUNTIME' -or
    $qmtRuntime.ready -ne $true -or
    $qmtRuntime.configuration_ready -ne $true -or
    $qmtRuntime.real_account_accessed -ne $false -or
    $qmtRuntime.real_order_transport_enabled -ne $false -or
    $qmtRuntime.automated_order_authorized -ne $false -or
    $qmtRuntime.live_status -ne 'LIVE_DISABLED'
) {
    throw 'app-owned QMT runtime is not configuration-ready; no task was changed'
}

$removed = @()
foreach ($taskName in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
        $removed += $taskName
    }
}
foreach ($taskName in $TaskNames) {
    if ($null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        throw "legacy forward task still exists after migration: $taskName"
    }
}

$migratedAt = (Get-Date).ToString(
    'yyyy-MM-ddTHH:mm:ss.ffffffK',
    [Globalization.CultureInfo]::InvariantCulture
)
$receipt = [ordered]@{
    schema = 'chanlun-v3-forward-app-migration/v1'
    migrated_at = $migratedAt
    execution_owner = 'APP_RUNTIME'
    app_contract_id = [string]$forward.contract_id
    qmt_contract_id = [string]$qmtRuntime.contract_id
    app_pid = [int]$owner.pid
    removed_tasks = @($removed)
    absent_legacy_tasks = @($TaskNames)
    qmt_runtime_migrated = $true
    qmt_bootstrap_task_preserved = $false
    qmt_legacy_task_absent = $true
    real_account_accessed = $false
    real_order_transport_enabled = $false
    automated_order_authorized = $false
    live_status = 'LIVE_DISABLED'
}
$null = New-Item -ItemType Directory -Path $ReceiptDir -Force
$temporary = '{0}.{1}.tmp' -f $ReceiptPath, $PID
try {
    [IO.File]::WriteAllText(
        $temporary,
        ($receipt | ConvertTo-Json -Depth 6 -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $ReceiptPath -Force
} finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}

Write-Output 'Forward scheduling and QMT runtime ownership migrated to app.py.'
Write-Output ('Removed legacy tasks: {0}' -f (($removed | Sort-Object) -join ', '))
Write-Output 'No business or QMT runtime Windows task remains.'
Write-Output ('Migration receipt: {0}' -f $ReceiptPath)
Write-Output 'Status remains REVIEW_REQUIRED/LIVE_DISABLED; no account or order transport was accessed.'
