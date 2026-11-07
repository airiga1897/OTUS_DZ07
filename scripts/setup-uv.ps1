function Get-SetupUv {
    param([string]$Root, $Settings)
    $uvOfficialUrl = 'https://github.com/astral-sh/uv/releases/download/' + $Settings.version + '/uv-x86_64-pc-windows-msvc.zip'
    if ($Settings.version -notmatch '^\d+\.\d+\.\d+$' -or $Settings.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or $Settings.url -cne $uvOfficialUrl) {
        throw 'Invalid pinned uv configuration.'
    }
    $uvTarget = Join-Path $Root '.tools/uv.exe'
    if (-not (Test-Path -LiteralPath $uvTarget -PathType Leaf)) {
        Install-SetupExecutable -Root $Root -Name uv -Settings $Settings
    }
    $uvVersion = & $uvTarget --version
    if ($LASTEXITCODE -or $uvVersion -notlike ('uv ' + $Settings.version + ' *')) { throw 'Local uv version differs from config/setup-tools.json.' }
    return $uvTarget
}
