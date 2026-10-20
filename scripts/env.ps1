# Dot-source in the current PowerShell session. No Python credential file mode.
param([switch]$Clear, [switch]$FromFile, [string]$Path)
. (Join-Path $PSScriptRoot 'operator.ps1')
if ($Clear -and ($FromFile -or $Path)) { throw 'Выберите один режим ввода.' }
if ($Clear) {
    Clear-OtusSelectelSession
    Write-Host 'Реквизиты удалены из окружения текущей сессии.'
    return
}
if ($Path -and -not $FromFile) { throw 'Путь допустим только с явным -FromFile.' }
$taskValues = $null
try {
    if ($FromFile) {
        if (-not $Path) { $Path = Join-Path $PSScriptRoot '../.local/operator.json' }
        $taskValues = Read-OtusSelectelFile -Path $Path
    } else { $taskValues = Read-OtusSelectelPrompt }
    Set-OtusSelectelSession -Values $taskValues
    Write-Host 'Реквизиты загружены только в окружение текущей сессии.'
} finally {
    if ($null -ne $taskValues) { $taskValues.Clear() }
    Remove-Variable taskValues -ErrorAction SilentlyContinue
}
