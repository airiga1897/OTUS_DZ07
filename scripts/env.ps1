# Загружать через dot sourcing. Пароль остаётся в окружении текущей сессии.
param(
    [string]$AccountNumber,
    [string]$UserName = 'otus-dz04-terraform',
    [string]$ProjectId = 'fdce7ecd9cf742a6a4eda91e927a09fb'
)
if (-not $AccountNumber) { $AccountNumber = Read-Host 'Номер аккаунта Selectel' }
if (-not $AccountNumber -or -not $UserName -or -not $ProjectId) { throw 'Параметры оператора должны быть заполнены.' }
$taskPassword = Read-Host "Пароль сервисного пользователя $UserName" -AsSecureString
if ($taskPassword.Length -eq 0) { $taskPassword.Dispose(); throw 'Пароль пуст.' }
$taskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskPassword)
try { $env:OS_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPointer) }
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer)
    $taskPassword.Dispose()
}
$env:OS_AUTH_URL = 'https://cloud.api.selcloud.ru/identity/v3'
$env:OS_PROJECT_ID = $ProjectId
$env:OS_REGION_NAME = 'ru-9'
$env:OS_USER_DOMAIN_NAME = $AccountNumber
$env:OS_USERNAME = $UserName
Remove-Item Env:OTUS_SELECTEL_AUTH -ErrorAction SilentlyContinue
Write-Host 'Оператор Selectel загружен в окружение. Пароль не записан в файл.'
