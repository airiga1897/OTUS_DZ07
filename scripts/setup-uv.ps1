function Get-SetupUv {
    param([string]$Root, $Settings)
    $uvOfficialUrl = 'https://github.com/astral-sh/uv/releases/download/' + $Settings.version + '/uv-x86_64-pc-windows-msvc.zip'
    if ($Settings.version -notmatch '^\d+\.\d+\.\d+$' -or $Settings.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or $Settings.url -cne $uvOfficialUrl) {
        throw 'Invalid pinned uv configuration.'
    }
    $uvTarget = Join-Path $Root '.tools/uv.exe'
    if (-not (Test-Path -LiteralPath $uvTarget -PathType Leaf)) {
        $uvTemporary = Join-Path $Root ('.tools/uv-download-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $uvTemporary | Out-Null
        try {
            $uvArchive = Join-Path $uvTemporary 'uv.zip'
            Write-Host ('uv: downloading pinned version ' + $Settings.version + '; HTTPS and SHA256 verification enabled')
            $uvCurl = Join-Path $env:SystemRoot 'System32/curl.exe'
            & $uvCurl --silent --show-error --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 120 --retry 2 --retry-delay 1 --output $uvArchive $Settings.url
            if ($LASTEXITCODE) { throw 'Pinned uv download failed; check Internet access and retry setup.' }
            if ((Get-FileHash -LiteralPath $uvArchive -Algorithm SHA256).Hash -ne $Settings.sha256) { throw 'uv archive SHA256 mismatch.' }
            Expand-Archive -LiteralPath $uvArchive -DestinationPath (Join-Path $uvTemporary 'unpacked')
            Copy-Item -LiteralPath (Join-Path $uvTemporary 'unpacked/uv.exe') -Destination $uvTarget
        } finally {
            $uvBoundary = [IO.Path]::GetFullPath((Join-Path $Root '.tools'))
            if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($uvTemporary)) -ne $uvBoundary) { throw 'Unsafe uv temporary path' }
            Remove-Item -LiteralPath $uvTemporary -Recurse -Force
        }
    }
    $uvVersion = & $uvTarget --version
    if ($LASTEXITCODE -or $uvVersion -notlike ('uv ' + $Settings.version + ' *')) { throw 'Local uv version differs from config/setup-tools.json.' }
    return $uvTarget
}
