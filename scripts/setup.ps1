param([string]$TerraformPath)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $taskRoot '.env.example') -Destination (Join-Path $taskRoot '.env') -ErrorAction Stop
}
New-Item -ItemType Directory -Force (Join-Path $taskRoot '.tools') | Out-Null
New-Item -ItemType Directory -Force (Join-Path $taskRoot '.local/tmp') | Out-Null
$taskPreviousTemp = $env:TEMP
$taskPreviousTmp = $env:TMP
$taskPreviousPipCache = $env:PIP_CACHE_DIR
try {
$env:TEMP = Join-Path $taskRoot '.local/tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $taskRoot '.local/pip-cache'
python -m venv (Join-Path $taskRoot '.tools/venv')
if ($LASTEXITCODE) { throw 'Python venv creation failed.' }
& (Join-Path $taskRoot '.tools/venv/Scripts/python.exe') -m pip install -r (Join-Path $taskRoot 'requirements-local.txt')
if ($LASTEXITCODE) { throw 'Local dependency installation failed.' }
$taskTarget = Join-Path $taskRoot '.tools/terraform.exe'
if ($TerraformPath) {
    $taskSource = (Resolve-Path -LiteralPath $TerraformPath -ErrorAction Stop).Path
    if ([IO.Path]::GetFullPath($taskSource) -ne [IO.Path]::GetFullPath($taskTarget)) {
        Copy-Item -LiteralPath $taskSource -Destination $taskTarget
    }
} elseif (-not (Test-Path -LiteralPath $taskTarget)) {
    Copy-Item -LiteralPath (Get-Command terraform -ErrorAction Stop).Source -Destination $taskTarget
}
& $taskTarget version
if ($LASTEXITCODE) { throw 'Local Terraform executable check failed.' }
& (Join-Path $taskRoot '.tools/venv/Scripts/python.exe') (Join-Path $taskRoot 'scripts/terraform_plugins.py')
if ($LASTEXITCODE) { throw 'Terraform provider preparation failed; no cloud resources created.' }
Write-Host 'Local Python and Terraform tools prepared; no cloud resources created.'
} finally {
    $env:TEMP = $taskPreviousTemp
    $env:TMP = $taskPreviousTmp
    $env:PIP_CACHE_DIR = $taskPreviousPipCache
}
