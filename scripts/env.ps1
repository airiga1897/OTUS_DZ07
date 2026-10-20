# Dot-source in PowerShell. The shared schema defines all credential fields.
param([switch]$Clear)
$taskPolicy = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../config/selectel.json') -Raw | ConvertFrom-Json
if ($Clear) {
    foreach ($taskField in $taskPolicy.credentials) {
        [Environment]::SetEnvironmentVariable($taskField.environment, $null, 'Process')
    }
    Remove-Item Env:OS_AUTH_URL, Env:OS_REGION_NAME, Env:OTUS_SELECTEL_AUTH -ErrorAction SilentlyContinue
    Write-Host 'Реквизиты удалены из окружения текущей сессии.'
    return
}
$taskInput = @{}
try {
    foreach ($taskField in $taskPolicy.credentials) {
        if ($taskField.secret) {
            $taskSecure = Read-Host $taskField.prompt -AsSecureString
            try {
                if ($taskSecure.Length -eq 0) { throw 'Пустой ввод реквизитов.' }
                $taskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecure)
                try { $taskValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPointer) }
                finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer) }
            } finally { $taskSecure.Dispose() }
        } else {
            $taskValue = Read-Host $taskField.prompt
            if ([string]::IsNullOrWhiteSpace($taskValue)) { throw 'Пустой ввод реквизитов.' }
        }
        $taskInput[$taskField.environment] = $taskValue
    }
    foreach ($taskName in $taskInput.Keys) {
        [Environment]::SetEnvironmentVariable($taskName, $taskInput[$taskName], 'Process')
    }
    $env:OS_AUTH_URL = $taskPolicy.auth_url
    $env:OS_REGION_NAME = $taskPolicy.region
    Remove-Item Env:OTUS_SELECTEL_AUTH -ErrorAction SilentlyContinue
    Write-Host 'Реквизиты загружены только в окружение текущей сессии.'
} finally {
    $taskInput.Clear()
    Remove-Variable taskValue, taskInput, taskSecure, taskPointer -ErrorAction SilentlyContinue
}
