# Compatibility wrapper. New automation should call ops/verify_deploy.ps1.
[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$HealthUri = 'http://127.0.0.1:9900/readyz?market=a',
    [string]$ExpectedRevision = '',
    [switch]$SkipProcessCheck,
    [switch]$SkipFreshnessCheck
)

$params = @{
    ProjectRoot = $ProjectRoot
    HealthUri = $HealthUri
    ExpectedRevision = $ExpectedRevision
    SkipProcessCheck = $SkipProcessCheck
    SkipFreshnessCheck = $SkipFreshnessCheck
}
& (Join-Path $PSScriptRoot 'verify_deploy.ps1') @params
exit $LASTEXITCODE
