function Get-SetupTerraform {
    param([string]$Root, $Settings, [string]$SourcePath)
    $terraformTarget = Join-Path $Root '.tools/terraform.exe'
    if ($SourcePath) {
        $terraformSource = (Resolve-Path -LiteralPath $SourcePath -ErrorAction Stop).Path
        if ([IO.Path]::GetFullPath($terraformSource) -ne [IO.Path]::GetFullPath($terraformTarget)) {
            Copy-Item -LiteralPath $terraformSource -Destination $terraformTarget
        }
    } elseif (-not (Test-Path -LiteralPath $terraformTarget -PathType Leaf)) {
        $terraformOfficialUrl = 'https://releases.hashicorp.com/terraform/' + $Settings.version + '/terraform_' + $Settings.version + '_windows_amd64.zip'
        if ($Settings.version -notmatch '^\d+\.\d+\.\d+$' -or $Settings.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or $Settings.url -cne $terraformOfficialUrl) {
            throw 'Invalid pinned Terraform configuration.'
        }
        Install-SetupExecutable -Root $Root -Name terraform -Settings $Settings
    }
    return $terraformTarget
}
