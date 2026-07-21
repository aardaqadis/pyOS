[CmdletBinding()]
param([string]$Path = "dist\pyOS.exe")

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Executable = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot $Path)).Path
$Bytes = [IO.File]::ReadAllBytes($Executable)
if ($Bytes.Length -lt 1024 -or [Text.Encoding]::ASCII.GetString($Bytes, 0, 2) -ne "MZ") {
    throw "$Executable is not a valid DOS/Windows executable."
}
$PeOffset = [BitConverter]::ToInt32($Bytes, 0x3c)
if ([Text.Encoding]::ASCII.GetString($Bytes, $PeOffset, 4) -ne "PE`0`0") {
    throw "$Executable does not contain a valid PE header."
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Inventory = & $Python -m PyInstaller.utils.cliutils.archive_viewer -l $Executable 2>$null
$Sensitive = $Inventory | Select-String -Pattern @(
    'credentials\.json', 'remembered_session', 'gui_settings\.json',
    'cli_settings\.json', 'virtual_drives\.json', 'profiles[/\\]', 'dev\.py', 'workspace\.xml'
)
$Signature = Get-AuthenticodeSignature -LiteralPath $Executable
$File = Get-Item -LiteralPath $Executable
[pscustomobject]@{
    Path = $Executable
    SizeMB = [math]::Round($File.Length / 1MB, 2)
    SHA256 = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash
    Version = $File.VersionInfo.FileVersion
    ProductVersion = $File.VersionInfo.ProductVersion
    Signature = $Signature.Status
    ValidPE = $true
    SensitiveFilesFound = @($Sensitive).Count
    PrivacyCheckPassed = (@($Sensitive).Count -eq 0)
}
if ($Sensitive) {
    throw "Sensitive runtime files were found in the executable inventory."
}
