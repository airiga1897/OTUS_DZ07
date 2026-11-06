param([string]$TerraformPath)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'setup-uv.ps1')
$taskTools = Get-Content -LiteralPath (Join-Path $taskRoot 'config/setup-tools.json') -Raw | ConvertFrom-Json
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $taskRoot '.env.example') -Destination (Join-Path $taskRoot '.env') -ErrorAction Stop
}
New-Item -ItemType Directory -Force (Join-Path $taskRoot '.tools') | Out-Null
New-Item -ItemType Directory -Force (Join-Path $taskRoot '.local/tmp') | Out-Null
$taskPreviousEnvironment = @{}
foreach ($taskName in 'TEMP','TMP','PIP_CACHE_DIR','UV_CACHE_DIR','UV_PYTHON_INSTALL_DIR','UV_NO_CONFIG','UV_SYSTEM_CERTS','UV_LINK_MODE','UV_PYTHON_DOWNLOADS','UV_HTTP_TIMEOUT','UV_HTTP_RETRIES','UV_CONCURRENT_DOWNLOADS') {
    $taskPreviousEnvironment[$taskName] = [Environment]::GetEnvironmentVariable($taskName, 'Process')
}
try {
$env:TEMP = Join-Path $taskRoot '.local/tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $taskRoot '.local/pip-cache'
$env:UV_CACHE_DIR = Join-Path $taskRoot '.tools/uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $taskRoot '.tools/python'
$env:UV_NO_CONFIG = '1'
$env:UV_SYSTEM_CERTS = '1'
$env:UV_LINK_MODE = 'copy'
$env:UV_PYTHON_DOWNLOADS = 'automatic'
$env:UV_HTTP_TIMEOUT = [string]$taskTools.network.http_timeout_seconds
$env:UV_HTTP_RETRIES = [string]$taskTools.network.http_retries
$env:UV_CONCURRENT_DOWNLOADS = [string]$taskTools.network.concurrent_downloads
$taskUv = Get-SetupUv -Root $taskRoot -Settings $taskTools.uv
$taskPython = Join-Path $taskRoot '.tools/venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    $taskVenv = Join-Path $taskRoot '.tools/venv'
    if ((Test-Path -LiteralPath $taskVenv) -and @(Get-ChildItem -LiteralPath $taskVenv -Force).Count) {
        throw 'Incomplete virtual environment preserved; inspect .tools/venv before retrying setup.'
    }
    Write-Host ('Python: downloading managed ' + $taskTools.python + ' into this clone; system Python and py launcher are not required')
    & $taskUv python install $taskTools.python --no-bin --no-registry
    if ($LASTEXITCODE) { throw 'Managed Python download failed; existing credentials were not changed.' }
    Write-Host 'Python: creating project virtual environment'
    & $taskUv venv --managed-python --python $taskTools.python --seed (Join-Path $taskRoot '.tools/venv')
    if ($LASTEXITCODE) { throw 'Python venv creation failed.' }
}
& $taskPython -c 'import sys; sys.exit(0 if sys.version_info[:2] == tuple(map(int, sys.argv[1].split(chr(46)))) else 1)' $taskTools.python
if ($LASTEXITCODE) { throw 'Project virtual environment must use Python 3.12. Credentials were not changed.' }
Write-Host 'Dependencies: installing pinned requirements into project virtual environment with uv'
& $taskUv pip install --python $taskPython -r (Join-Path $taskRoot 'requirements-quality.txt')
if ($LASTEXITCODE) { throw 'Local dependency installation failed.' }
& $taskUv pip check --python $taskPython
if ($LASTEXITCODE) { throw 'Virtual environment dependency consistency check failed.' }
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
    foreach ($taskName in $taskPreviousEnvironment.Keys) {
        if ($null -eq $taskPreviousEnvironment[$taskName]) {
            Remove-Item -LiteralPath ('Env:' + $taskName) -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($taskName, $taskPreviousEnvironment[$taskName], 'Process')
        }
    }
}
