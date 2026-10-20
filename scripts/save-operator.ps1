# Explicit development persistence. Not required for deploy, verify or destroy.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'operator.ps1')
Save-OtusSelectelFile -Directory (Join-Path $PSScriptRoot '../.local')
Write-Host 'Реквизиты разработки сохранены локально с доступом текущего пользователя; удалите файл после очистки ресурсов.'
