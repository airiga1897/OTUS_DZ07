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
$taskPython = Join-Path $taskRoot '.tools/venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    py -3.12 -m venv (Join-Path $taskRoot '.tools/venv')
    if ($LASTEXITCODE) { throw 'Python venv creation failed.' }
}
& $taskPython -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'
if ($LASTEXITCODE) { throw 'Project virtual environment must use Python 3.12. Credentials were not changed.' }
& $taskPython -m pip install -r (Join-Path $taskRoot 'requirements-quality.txt')
if ($LASTEXITCODE) { throw 'Local dependency installation failed.' }
& $taskPython -c 'import _cffi_backend, paramiko, yaml, jinja2, boto3, coverage'
if ($LASTEXITCODE) { throw 'Virtual environment dependency imports failed; repair .tools/venv with its original Python version. No cloud resources created.' }
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
& $taskPython (Join-Path $taskRoot 'scripts/terraform_plugins.py')
if ($LASTEXITCODE) { throw 'Terraform provider preparation failed; no cloud resources created.' }
Write-Host 'Local Python and Terraform tools prepared; no cloud resources created.'
} finally {
    $env:TEMP = $taskPreviousTemp
    $env:TMP = $taskPreviousTmp
    $env:PIP_CACHE_DIR = $taskPreviousPipCache
}
