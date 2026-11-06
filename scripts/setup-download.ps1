function Install-SetupExecutable {
    param([string]$Root, [ValidateSet('uv','terraform')][string]$Name, $Settings)
    $toolTemporary = Join-Path $Root ('.tools/' + $Name + '-download-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $toolTemporary | Out-Null
    try {
        $toolArchive = Join-Path $toolTemporary ($Name + '.zip')
        Write-Host ($Name + ': downloading pinned version ' + $Settings.version + '; HTTPS and SHA256 verification enabled')
        $toolCurl = Join-Path $env:SystemRoot 'System32/curl.exe'
        & $toolCurl --silent --show-error --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 120 --retry 2 --retry-delay 1 --output $toolArchive $Settings.url
        if ($LASTEXITCODE) { throw ('Pinned ' + $Name + ' download failed; check Internet access and retry setup.') }
        if ((Get-FileHash -LiteralPath $toolArchive -Algorithm SHA256).Hash -ne $Settings.sha256) { throw ($Name + ' archive SHA256 mismatch.') }
        Expand-Archive -LiteralPath $toolArchive -DestinationPath (Join-Path $toolTemporary 'unpacked')
        Copy-Item -LiteralPath (Join-Path $toolTemporary ('unpacked/' + $Name + '.exe')) -Destination (Join-Path $Root ('.tools/' + $Name + '.exe'))
    } finally {
        $toolBoundary = [IO.Path]::GetFullPath((Join-Path $Root '.tools'))
        if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($toolTemporary)) -ne $toolBoundary) { throw 'Unsafe tool temporary path' }
        Remove-Item -LiteralPath $toolTemporary -Recurse -Force
    }
}
