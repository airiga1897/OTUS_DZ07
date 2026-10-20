# Shared PowerShell credential operations. Field definitions exist only in JSON.
function Get-OtusSelectelPolicy {
    Get-Content -LiteralPath (Join-Path $PSScriptRoot '../config/selectel.json') -Raw | ConvertFrom-Json
}

function Assert-OtusSelectelValues {
    param([hashtable]$Values)
    $policy = Get-OtusSelectelPolicy
    foreach ($field in $policy.credentials) {
        if ($Values[$field.key] -isnot [string] -or [string]::IsNullOrWhiteSpace($Values[$field.key])) {
            throw 'Реквизиты не заполнены; выполните scripts/env.ps1.'
        }
    }
    if (($Values.auth_url -and $Values.auth_url -ne $policy.auth_url) -or
        ($Values.region -and $Values.region -ne $policy.region)) {
        throw 'Источник реквизитов не соответствует настройкам среды.'
    }
}

function Get-OtusSelectelSession {
    $policy = Get-OtusSelectelPolicy
    $values = @{ auth_url = $policy.auth_url; region = $policy.region }
    foreach ($field in $policy.credentials) {
        $values[$field.key] = [Environment]::GetEnvironmentVariable($field.environment, 'Process')
    }
    Assert-OtusSelectelValues $values
    return $values
}

function Set-OtusSelectelSession {
    param([hashtable]$Values)
    Assert-OtusSelectelValues $Values
    $policy = Get-OtusSelectelPolicy
    foreach ($field in $policy.credentials) {
        [Environment]::SetEnvironmentVariable($field.environment, $Values[$field.key], 'Process')
    }
    $env:OS_AUTH_URL = $policy.auth_url
    $env:OS_REGION_NAME = $policy.region
}

function Clear-OtusSelectelSession {
    foreach ($field in (Get-OtusSelectelPolicy).credentials) {
        [Environment]::SetEnvironmentVariable($field.environment, $null, 'Process')
    }
    Remove-Item Env:OS_AUTH_URL, Env:OS_REGION_NAME -ErrorAction SilentlyContinue
}

function Read-OtusSelectelPrompt {
    $values = @{}
    foreach ($field in (Get-OtusSelectelPolicy).credentials) {
        if ($field.secret) {
            $secure = Read-Host $field.prompt -AsSecureString
            try {
                if ($secure.Length -eq 0) { throw 'Пустой ввод реквизитов.' }
                $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
                try { $values[$field.key] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
                finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
            } finally { $secure.Dispose() }
        } else {
            $values[$field.key] = Read-Host $field.prompt
        }
    }
    Assert-OtusSelectelValues $values
    return $values
}

function Read-OtusSelectelFile {
    param([string]$Path)
    try { $data = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'Не удалось прочитать явно выбранный локальный файл реквизитов.' }
    $values = @{ auth_url = $data.auth_url; region = $data.region }
    foreach ($field in (Get-OtusSelectelPolicy).credentials) {
        $values[$field.key] = $data.($field.key)
    }
    Assert-OtusSelectelValues $values
    return $values
}

function Save-OtusSelectelFile {
    param([string]$Directory)
    $values = Get-OtusSelectelSession
    try {
        New-Item -ItemType Directory -Force $Directory | Out-Null
        $owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $acl.SetOwner($owner)
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $owner, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
        Set-Acl -LiteralPath $Directory -AclObject $acl -ErrorAction Stop
        $path = Join-Path $Directory 'operator.json'
        if (Test-Path -LiteralPath $path) {
            $fileAcl = [System.Security.AccessControl.FileSecurity]::new()
            $fileAcl.SetOwner($owner)
            $fileAcl.SetAccessRuleProtection($true, $false)
            $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($owner, 'FullControl', 'Allow'))
            Set-Acl -LiteralPath $path -AclObject $fileAcl -ErrorAction Stop
        }
        $values | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding utf8 -ErrorAction Stop
    } finally { $values.Clear() }
}
