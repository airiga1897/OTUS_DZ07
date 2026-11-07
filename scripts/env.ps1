[CmdletBinding(DefaultParameterSetName='Prompt')]
param(
    [Parameter(ParameterSetName='Save')][switch]$SaveLocal,
    [Parameter(ParameterSetName='Save')][switch]$UseSession,
    [Parameter(ParameterSetName='Load')][Alias('Dev','FromFile')][switch]$Development,
    [Parameter(ParameterSetName='Clear')][switch]$Clear,
    [Parameter(ParameterSetName='Prompt')][Parameter(ParameterSetName='Save')][Parameter(ParameterSetName='Load')][switch]$Artifacts,
    [Parameter(ParameterSetName='Prompt')][Parameter(ParameterSetName='Save')][string]$AccountNumber,
    [Parameter(ParameterSetName='Prompt')][Parameter(ParameterSetName='Save')][string]$UserName,
    [Parameter(ParameterSetName='Prompt')][Parameter(ParameterSetName='Save')][string]$ProjectId
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'operator.ps1')
if ($Artifacts) {
    $artifactSchema = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../config/artifact-auth.json') -Raw | ConvertFrom-Json
    $artifactValues = @{}
    try {
        if ($Development) {
            $artifactPath = Join-Path $PSScriptRoot '../.local/artifact-credentials.json'
            if ((Get-Item -LiteralPath (Split-Path $artifactPath)).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Credential directory links forbidden.' }
            Assert-OtusPrivateFile $artifactPath
            $artifactFile = Get-Content -LiteralPath $artifactPath -Raw | ConvertFrom-Json
        }
        foreach ($property in $artifactSchema.fields.PSObject.Properties) {
            if ($Development) { $artifactValues[$property.Name] = $artifactFile.($property.Name) }
            elseif ($UseSession) { $artifactValues[$property.Name] = [Environment]::GetEnvironmentVariable($property.Value.env, 'Process') }
            else {
                $secure = Read-Host $property.Value.prompt -AsSecureString
                try {
                    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
                    try { $artifactValues[$property.Name] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
                    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
                } finally { $secure.Dispose() }
            }
            if ($artifactValues[$property.Name] -isnot [string] -or [string]::IsNullOrWhiteSpace($artifactValues[$property.Name])) { throw 'S3 keys must not be empty.' }
        }
        foreach ($property in $artifactSchema.fields.PSObject.Properties) { Set-Item ('Env:' + $property.Value.env) $artifactValues[$property.Name] }
        if ($SaveLocal) { Save-OtusPrivateValues $artifactValues 'artifact-credentials.json' }
        Write-Host 'S3 keys loaded; values hidden. Local persistence only with -SaveLocal.'
    } finally {
        $artifactValues.Clear()
        Remove-Variable artifactFile,artifactValues,secure,pointer -ErrorAction SilentlyContinue
    }
    return
}
$taskSchema = Get-OtusAuthSchema
Remove-Item Env:OTUS_SELECTEL_AUTH -ErrorAction SilentlyContinue
if ($Clear) {
    $artifactSchema = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../config/artifact-auth.json') -Raw | ConvertFrom-Json
    foreach ($property in $artifactSchema.fields.PSObject.Properties) { Remove-Item ('Env:' + $property.Value.env) -ErrorAction SilentlyContinue }
    foreach ($property in $taskSchema.fields.PSObject.Properties) { Remove-Item ('Env:' + $property.Value.env) -ErrorAction SilentlyContinue }
    Remove-Item Env:OS_AUTH_URL,Env:OS_REGION_NAME -ErrorAction SilentlyContinue
    return
}
$taskValues = @{}
try {
    if ($UseSession) {
        if (-not $SaveLocal) { throw '-UseSession requires -SaveLocal.' }
        $taskValues = Get-OtusSession
    } elseif ($Development) {
        $taskPath = Join-Path $PSScriptRoot '../.local/operator.json'
        if ((Get-Item -LiteralPath (Split-Path $taskPath)).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Ссылка вместо .local запрещена.' }
        Assert-OtusPrivateFile $taskPath
        $taskFile = Get-Content -LiteralPath $taskPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($property in $taskSchema.fields.PSObject.Properties) { $taskValues[$property.Name] = $taskFile.($property.Name) }
    } else {
        foreach ($property in $taskSchema.fields.PSObject.Properties) {
            $field = $property.Value
            if ($field.secret) {
                $secure = Read-Host $field.prompt -AsSecureString
                try {
                    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
                    try { $taskValues[$property.Name] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
                    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
                } finally { $secure.Dispose() }
            } else {
                $value = switch ($property.Name) { 'domain_name' {$AccountNumber} 'username' {$UserName} 'project_id' {$ProjectId} }
                if (-not $value) { $value = Read-Host $field.prompt }
                $taskValues[$property.Name] = $value
            }
        }
    }
    Assert-OtusValues $taskValues
    foreach ($property in $taskSchema.fields.PSObject.Properties) { Set-Item ('Env:' + $property.Value.env) $taskValues[$property.Name] }
    Remove-Item Env:OS_REGION_NAME -ErrorAction SilentlyContinue
    $env:OS_AUTH_URL = $taskSchema.auth_url
    if ($SaveLocal) { Save-OtusSession }
    Write-Host 'Реквизиты загружены в текущую сессию; значения скрыты.'
} finally {
    $taskValues.Clear()
    Remove-Variable taskFile,taskValues,value,secure,pointer -ErrorAction SilentlyContinue
}
