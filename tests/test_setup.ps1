# Actual setup/bootstrap with isolated files and mock commands; no network or cloud.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$testRoot = [IO.Path]::GetFullPath((Join-Path $repo ('.tools/setup-test-' + [guid]::NewGuid().ToString('N'))))
$expectedParent = [IO.Path]::GetFullPath((Join-Path $repo '.tools'))
if ([IO.Path]::GetDirectoryName($testRoot) -ne $expectedParent) { throw 'Unsafe setup test path' }
$testPython = Join-Path $testRoot '.tools/venv/Scripts/python.exe'
$testTerraform = Join-Path $testRoot '.tools/terraform.exe'
$testUv = Join-Path $testRoot '.tools/uv.exe'
$testCurl = Join-Path $env:SystemRoot 'System32/curl.exe'
$calls = New-Object 'System.Collections.Generic.List[string]'
$originalEnv = @{}
foreach ($name in 'TEMP','TMP','PIP_CACHE_DIR','UV_CACHE_DIR','UV_PYTHON_INSTALL_DIR','UV_NO_CONFIG','UV_SYSTEM_CERTS','UV_LINK_MODE','UV_PYTHON_DOWNLOADS','UV_HTTP_TIMEOUT','UV_HTTP_RETRIES','UV_CONCURRENT_DOWNLOADS') {
    $originalEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$global:otusDz07SetupTestContext = @{ Calls=$calls; Python=$testPython; RealPython=(Join-Path $repo '.tools/venv/Scripts/python.exe'); Failed=''; WrongUv=$false; WrongPython=$false; Environment=$originalEnv; Root=$testRoot }
function Invoke-TestUv {
    $context = $global:otusDz07SetupTestContext
    if ($env:UV_HTTP_TIMEOUT -ne '120' -or $env:UV_HTTP_RETRIES -ne '3' -or $env:UV_CONCURRENT_DOWNLOADS -ne '4') { throw 'Project download policy was not applied' }
    $step = switch ($args[0]) { '--version' { 'uv-version' }; 'python' { 'python-download' }; 'venv' { 'create' }; 'pip' { if ($args[1] -eq 'install') { 'dependencies' } else { 'check' } }; default { throw 'Unexpected uv command' } }
    $context.Calls.Add($step)
    $global:LASTEXITCODE = 0
    if ($context.Failed -eq $step) { $global:LASTEXITCODE = 1; return }
    switch ($step) {
        'uv-version' { if ($context.WrongUv) { 'uv 0.0.0 (test)' } else { 'uv 0.13.7 (test)' } }
        'python-download' { if (($args -join ' ') -ne 'python install 3.12 --no-bin --no-registry') { throw 'Managed Python must not modify system registration or PATH' } }
        'create' {
            if (($args -join ' ') -notlike 'venv --managed-python --python 3.12 --seed *') { throw 'Wrong venv source' }
            New-Item -ItemType Directory -Force (Split-Path -Parent $context.Python) | Out-Null
            Set-Content -LiteralPath $context.Python -Value 'synthetic interpreter'
        }
        'dependencies' { if ($args[2] -ne '--python' -or $args[3] -ne $context.Python -or [IO.Path]::GetFileName($args[5]) -ne 'requirements-quality.txt') { throw 'Wrong dependency target' } }
        'check' { if ($args[2] -ne '--python' -or $args[3] -ne $context.Python) { throw 'Wrong consistency target' } }
    }
}
function Invoke-TestPython {
    $context = $global:otusDz07SetupTestContext
    $step = if ($args[0] -ne '-c') { 'providers' } elseif ($args[1] -like '*sys.version_info*') { 'version' } else { 'imports' }
    $context.Calls.Add($step)
    $global:LASTEXITCODE = 0
    if ($context.Failed -eq $step -or ($step -eq 'version' -and $context.WrongPython)) { $global:LASTEXITCODE = 1; return }
    if ($step -eq 'version') { & $context.RealPython @args }
}
function Invoke-TestTerraform { $global:otusDz07SetupTestContext.Calls.Add('terraform'); $global:LASTEXITCODE = [int]($global:otusDz07SetupTestContext.Failed -eq 'terraform') }
function Invoke-TestDownload {
    $context = $global:otusDz07SetupTestContext
    $context.Calls.Add('download')
    $expected = '--silent --show-error --fail --location --proto =https --proto-redir =https --connect-timeout 20 --max-time 120 --retry 2 --retry-delay 1 --output * https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-pc-windows-msvc.zip'
    if (($args -join ' ') -notlike $expected) { throw 'Unexpected or unbounded download' }
    $global:LASTEXITCODE = 0
    if ($context.Failed -eq 'download') { $global:LASTEXITCODE = 1; return }
    Copy-Item -LiteralPath (Join-Path $context.Root 'test-uv.zip') -Destination $args[$args.Count - 2]
}
function Assert-TestEnvironment {
    foreach ($name in $global:otusDz07SetupTestContext.Environment.Keys) {
        if ([Environment]::GetEnvironmentVariable($name,'Process') -ne $global:otusDz07SetupTestContext.Environment[$name]) { throw ('Environment was not restored: ' + $name) }
    }
}
function Invoke-ExpectedFailure {
    param([string]$Step, [string]$Message)
    $calls.Clear(); $global:otusDz07SetupTestContext.Failed = $Step; $caught = $false
    try { & "$testRoot/scripts/setup.ps1" } catch { $caught = $_.Exception.Message -like $Message }
    if (-not $caught) { throw ('Failure not reported: ' + $Step) }
    if ($Step -ne 'download' -and $calls[$calls.Count - 1] -ne $Step) { throw ('Preparation continued after failure: ' + $Step) }
    Assert-TestEnvironment
    $global:otusDz07SetupTestContext.Failed = ''
}
try {
    New-Item -ItemType Directory -Force "$testRoot/scripts", "$testRoot/config", "$testRoot/.local", (Split-Path -Parent $testPython), "$testRoot/archive" | Out-Null
    Copy-Item -LiteralPath "$repo/scripts/setup.ps1", "$repo/scripts/setup-uv.ps1" -Destination "$testRoot/scripts"
    Copy-Item -LiteralPath "$repo/config/setup-tools.json" -Destination "$testRoot/config/setup-tools.json"
    Set-Content -LiteralPath "$testRoot/.env.example" -Value 'EXAMPLE=true'
    Set-Content -LiteralPath "$testRoot/.env" -Value 'PRESERVE=true'
    Set-Content -LiteralPath "$testRoot/.local/operator.json" -Value '{"synthetic":"preserve"}'
    $credentialsHash = (Get-FileHash "$testRoot/.local/operator.json").Hash
    Set-Content -LiteralPath $testPython -Value 'existing interpreter'
    Set-Content -LiteralPath $testTerraform -Value 'synthetic terraform'
    Set-Content -LiteralPath $testUv -Value 'synthetic uv'
    Set-Content -LiteralPath "$testRoot/archive/uv.exe" -Value 'synthetic uv'
    Compress-Archive -LiteralPath "$testRoot/archive/uv.exe" -DestinationPath "$testRoot/test-uv.zip"
    $settings = Get-Content "$testRoot/config/setup-tools.json" -Raw | ConvertFrom-Json
    $settings.uv.sha256 = (Get-FileHash "$testRoot/test-uv.zip" -Algorithm SHA256).Hash
    $settings | ConvertTo-Json | Set-Content "$testRoot/config/setup-tools.json"
    Set-Alias -Name $testPython -Value Invoke-TestPython -Scope Script
    Set-Alias -Name $testTerraform -Value Invoke-TestTerraform -Scope Script
    Set-Alias -Name $testUv -Value Invoke-TestUv -Scope Script
    Set-Alias -Name $testCurl -Value Invoke-TestDownload -Scope Script
    & "$testRoot/scripts/setup.ps1"
    & "$testRoot/scripts/setup.ps1"
    $normal = 'uv-version,version,dependencies,check,imports,terraform,providers'
    if (($calls -join ',') -ne "$normal,$normal") { throw 'Existing environment was not reused' }
    if ((Get-Content $testPython -Raw).Trim() -ne 'existing interpreter') { throw 'Existing Python was overwritten' }
    Assert-TestEnvironment
    $calls.Clear(); Remove-Item -LiteralPath "$testRoot/.tools/venv" -Recurse -Force; Remove-Item -LiteralPath $testUv
    & "$testRoot/scripts/setup.ps1"
    if (($calls -join ',') -ne 'download,uv-version,python-download,create,version,dependencies,check,imports,terraform,providers') { throw 'Fresh preparation did not download its own tools' }
    Assert-TestEnvironment
    foreach ($failure in @(@('dependencies','Local dependency installation failed.'), @('check','Virtual environment dependency consistency check failed.'), @('imports','Virtual environment dependency imports failed*'), @('terraform','Local Terraform executable check failed.'), @('providers','Terraform provider preparation failed*'))) {
        Invoke-ExpectedFailure $failure[0] $failure[1]
    }
    $calls.Clear(); $global:otusDz07SetupTestContext.WrongPython = $true
    try { & "$testRoot/scripts/setup.ps1"; throw 'Wrong Python accepted' } catch { if ($_.Exception.Message -notlike 'Project virtual environment must use Python 3.12*') { throw } }
    $global:otusDz07SetupTestContext.WrongPython = $false; Assert-TestEnvironment
    $global:otusDz07SetupTestContext.WrongUv = $true
    try { & "$testRoot/scripts/setup.ps1"; throw 'Wrong uv accepted' } catch { if ($_.Exception.Message -ne 'Local uv version differs from config/setup-tools.json.') { throw } }
    $global:otusDz07SetupTestContext.WrongUv = $false; Assert-TestEnvironment
    Remove-Item -LiteralPath "$testRoot/.tools/venv" -Recurse -Force
    Invoke-ExpectedFailure 'python-download' 'Managed Python download failed*'
    Invoke-ExpectedFailure 'create' 'Python venv creation failed.'
    Remove-Item -LiteralPath $testUv
    Invoke-ExpectedFailure 'download' 'Pinned uv download failed*'
    $settings.uv.sha256 = '0' * 64
    $settings | ConvertTo-Json | Set-Content "$testRoot/config/setup-tools.json"
    try { & "$testRoot/scripts/setup.ps1"; throw 'Bad archive accepted' } catch { if ($_.Exception.Message -ne 'uv archive SHA256 mismatch.') { throw } }
    if (Test-Path $testUv) { throw 'Unverified uv was installed' }
    if (@(Get-ChildItem "$testRoot/.tools" -Filter 'uv-download-*').Count) { throw 'Temporary download was not cleaned' }
    if ((Get-FileHash "$testRoot/.local/operator.json").Hash -ne $credentialsHash) { throw 'Credentials changed' }
    if ((Get-Content "$testRoot/.env" -Raw).Trim() -ne 'PRESERVE=true') { throw 'Configuration changed' }
    Assert-TestEnvironment
    Write-Host 'uv bootstrap, SHA256 rejection, fresh Python, reuse, failures, environment and credential preservation verified offline.'
} finally {
    foreach ($name in $originalEnv.Keys) {
        if ($null -eq $originalEnv[$name]) { Remove-Item -LiteralPath ('Env:' + $name) -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable($name,$originalEnv[$name],'Process') }
    }
    foreach ($name in @($testPython,$testTerraform,$testUv,$testCurl)) { Remove-Item -LiteralPath ('Alias:' + $name) -ErrorAction SilentlyContinue }
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($testRoot)) -ne $expectedParent) { throw 'Unsafe setup cleanup' }
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
    Remove-Variable -Name otusDz07SetupTestContext -Scope Global
}
