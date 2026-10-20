# Optional development mode. Reviewer lifecycle does not use this script.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskLocal = Join-Path $taskRoot '.local'
New-Item -ItemType Directory -Force $taskLocal | Out-Null
# Restrict the directory before writing a plaintext local credential file.
$taskIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskOwner = [System.Security.Principal.NTAccount]::new($taskIdentity)
$taskAcl = [System.Security.AccessControl.DirectorySecurity]::new()
$taskAcl.SetOwner($taskOwner)
$taskAcl.SetAccessRuleProtection($true, $false)
$taskAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
    $taskOwner, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $taskLocal -AclObject $taskAcl
$taskFile = Join-Path $taskLocal 'operator.json'
if (Test-Path -LiteralPath $taskFile) {
    $taskFileAcl = [System.Security.AccessControl.FileSecurity]::new()
    $taskFileAcl.SetOwner($taskOwner)
    $taskFileAcl.SetAccessRuleProtection($true, $false)
    $taskFileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($taskOwner, 'FullControl', 'Allow'))
    Set-Acl -LiteralPath $taskFile -AclObject $taskFileAcl
}
& (Join-Path $taskRoot '.tools/venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'credentials.py') save-development
if ($LASTEXITCODE -ne 0) { throw 'Не удалось сохранить реквизиты разработки.' }
