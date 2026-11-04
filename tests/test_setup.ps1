# Exercise the real preparation script with isolated files and fake native commands.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$testRoot = [IO.Path]::GetFullPath((Join-Path $repo ('.tools/setup-test-' + [guid]::NewGuid().ToString('N'))))
$expectedParent = [IO.Path]::GetFullPath((Join-Path $repo '.tools'))
if ([IO.Path]::GetDirectoryName($testRoot) -ne $expectedParent) { throw 'Unsafe setup test path' }
$testPython = Join-Path $testRoot '.tools/venv/Scripts/python.exe'
$testTerraform = Join-Path $testRoot '.tools/terraform.exe'
$calls = New-Object 'System.Collections.Generic.List[string]'
$originalEnv = @{}
foreach ($name in 'TEMP','TMP','PIP_CACHE_DIR') { $originalEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$global:otusDz07SetupTestContext = @{ Calls=$calls; Python=$testPython; BrokenImports=$false; WrongVersion=$false; FailedCreation=$false; Environment=$originalEnv }
function Invoke-TestCreation {
    if (($args -join ' ') -notlike '-3.12 -m venv *') { throw 'Fresh environment must use py -3.12' }
    $global:otusDz07SetupTestContext.Calls.Add('create')
    if ($global:otusDz07SetupTestContext.FailedCreation) { $global:LASTEXITCODE = 1; return }
    New-Item -ItemType Directory -Force (Split-Path -Parent $global:otusDz07SetupTestContext.Python) | Out-Null
    Set-Content -LiteralPath $global:otusDz07SetupTestContext.Python -Value 'synthetic interpreter'
    $global:LASTEXITCODE = 0
}
function Invoke-TestVenv {
    if ($args[0] -eq '-m') {
        if ($args[1] -ne 'pip' -or $args[2] -ne 'install' -or $args[3] -ne '-r' -or
            [IO.Path]::GetFileName($args[4]) -ne 'requirements-quality.txt') { throw 'Wrong dependency installation' }
        $global:otusDz07SetupTestContext.Calls.Add('pip')
    }
    elseif ($args[0] -eq '-c' -and $args[1] -like '*sys.version_info*') { $global:otusDz07SetupTestContext.Calls.Add('version') }
    elseif ($args[0] -eq '-c') { $global:otusDz07SetupTestContext.Calls.Add('imports') }
    else { $global:otusDz07SetupTestContext.Calls.Add('providers') }
    $global:LASTEXITCODE = 0
    if ($args[0] -eq '-c' -and $args[1] -like '*sys.version_info*') {
        if ($global:otusDz07SetupTestContext.WrongVersion) { $global:LASTEXITCODE = 1 }
    } elseif ($args[0] -eq '-c' -and $global:otusDz07SetupTestContext.BrokenImports) { $global:LASTEXITCODE = 1 }
}
function Invoke-TestTerraform { $global:otusDz07SetupTestContext.Calls.Add('terraform'); $global:LASTEXITCODE = 0 }
function Assert-TestEnvironment {
    foreach ($name in $global:otusDz07SetupTestContext.Environment.Keys) {
        if ([Environment]::GetEnvironmentVariable($name,'Process') -ne $global:otusDz07SetupTestContext.Environment[$name]) {
            throw ('Environment was not restored: ' + $name)
        }
    }
}
try {
    New-Item -ItemType Directory -Force "$testRoot/scripts", (Split-Path -Parent $testPython) | Out-Null
    Copy-Item -LiteralPath "$repo/scripts/setup.ps1" -Destination "$testRoot/scripts/setup.ps1"
    Set-Content -LiteralPath "$testRoot/.env.example" -Value 'EXAMPLE=true'
    Set-Content -LiteralPath "$testRoot/.env" -Value 'PRESERVE=true'
    Set-Content -LiteralPath $testPython -Value 'existing interpreter'
    Set-Content -LiteralPath $testTerraform -Value 'synthetic terraform'
    Set-Alias -Name py -Value Invoke-TestCreation -Scope Script
    Set-Alias -Name $testPython -Value Invoke-TestVenv -Scope Script
    Set-Alias -Name $testTerraform -Value Invoke-TestTerraform -Scope Script

    # A changed python in PATH must never recreate the existing environment.
    & "$testRoot/scripts/setup.ps1"
    & "$testRoot/scripts/setup.ps1"
    if (($calls -join ',') -ne 'version,pip,imports,terraform,providers,version,pip,imports,terraform,providers') { throw 'Existing venv was not reused' }
    if ((Get-Content -LiteralPath $testPython -Raw).Trim() -ne 'existing interpreter') { throw 'Interpreter was overwritten' }
    if ((Get-Content -LiteralPath "$testRoot/.env" -Raw).Trim() -ne 'PRESERVE=true') { throw 'Existing configuration was overwritten' }
    Assert-TestEnvironment

    $calls.Clear()
    Remove-Item -LiteralPath $testPython
    & "$testRoot/scripts/setup.ps1"
    if (($calls -join ',') -ne 'create,version,pip,imports,terraform,providers') { throw 'Fresh venv preparation failed' }
    Assert-TestEnvironment

    $calls.Clear(); $global:otusDz07SetupTestContext.BrokenImports = $true; $caught = $false
    try { & "$testRoot/scripts/setup.ps1" } catch { $caught = $_.Exception.Message -like 'Virtual environment dependency imports failed*' }
    if (-not $caught -or ($calls -join ',') -ne 'version,pip,imports') { throw 'Broken dependencies reached provider preparation' }
    Assert-TestEnvironment

    $calls.Clear(); $global:otusDz07SetupTestContext.BrokenImports = $false; $global:otusDz07SetupTestContext.WrongVersion = $true; $caught = $false
    try { & "$testRoot/scripts/setup.ps1" } catch { $caught = $_.Exception.Message -like 'Project virtual environment must use Python 3.12*' }
    if (-not $caught -or ($calls -join ',') -ne 'version') { throw 'Wrong Python version reached dependency installation' }
    Assert-TestEnvironment
    $global:otusDz07SetupTestContext.WrongVersion = $false

    $calls.Clear(); $global:otusDz07SetupTestContext.BrokenImports = $false; $global:otusDz07SetupTestContext.FailedCreation = $true; $caught = $false
    Remove-Item -LiteralPath $testPython
    try { & "$testRoot/scripts/setup.ps1" } catch { $caught = $_.Exception.Message -eq 'Python venv creation failed.' }
    if (-not $caught -or ($calls -join ',') -ne 'create') { throw 'Failed creation continued preparation' }
    Assert-TestEnvironment
    Write-Host 'Setup reuse, fresh creation, native import failure and creation failure verified without network or cloud.'
} finally {
    foreach ($name in $originalEnv.Keys) { [Environment]::SetEnvironmentVariable($name,$originalEnv[$name],'Process') }
    foreach ($name in @('py',$testPython,$testTerraform)) { Remove-Item -LiteralPath ('Alias:' + $name) -ErrorAction SilentlyContinue }
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($testRoot)) -ne $expectedParent) { throw 'Unsafe setup cleanup' }
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
    Remove-Variable -Name otusDz07SetupTestContext -Scope Global
}
