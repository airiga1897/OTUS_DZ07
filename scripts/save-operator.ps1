# Run interactively after dot-sourcing selectel-env.ps1. Values are never printed.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskLocal = Join-Path $taskRoot '.local'
New-Item -ItemType Directory -Force $taskLocal | Out-Null
$taskValues = @{
    auth_url = $env:OS_AUTH_URL
    project_id = $env:OS_PROJECT_ID
    region = $env:OS_REGION_NAME
    domain_name = $env:OS_USER_DOMAIN_NAME
    username = $env:OS_USERNAME
    password = $env:OS_PASSWORD
}
foreach ($taskValue in $taskValues.Values) {
    if (-not $taskValue) { throw 'First load the operator environment with scripts/env.ps1.' }
}
$taskPath = Join-Path $taskLocal 'operator.json'
# Restrict the directory before writing a plaintext local credential file.
$taskIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $taskLocal /inheritance:r /grant:r "${taskIdentity}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the local credential directory.' }
$taskValues | ConvertTo-Json | Set-Content -LiteralPath $taskPath -Encoding utf8
Write-Host 'Operator configuration saved locally in .local/operator.json; excluded from Git.'
