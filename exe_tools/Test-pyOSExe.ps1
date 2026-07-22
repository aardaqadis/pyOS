[CmdletBinding()]
param(
    [string]$Path = "dist\pyOS.exe",
    [switch]$Release,
    [Alias("Python")]
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Candidate = if ([IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $ProjectRoot $Path }
$Executable = (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
$Bytes = [IO.File]::ReadAllBytes($Executable)
if ($Bytes.Length -lt 1024 -or [Text.Encoding]::ASCII.GetString($Bytes, 0, 2) -ne "MZ") {
    throw "$Executable is not a valid DOS/Windows executable."
}
$PeOffset = [BitConverter]::ToInt32($Bytes, 0x3c)
if ($PeOffset -lt 0 -or $PeOffset + 4 -gt $Bytes.Length -or
        [Text.Encoding]::ASCII.GetString($Bytes, $PeOffset, 4) -ne "PE`0`0") {
    throw "$Executable does not contain a valid PE header."
}

$ProjectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
    if (Test-Path -LiteralPath $PythonPath -PathType Leaf) {
        $Python = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
    } else {
        $PythonCommand = Get-Command -Name $PythonPath -CommandType Application `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $PythonCommand) {
            throw "Python executable '$PythonPath' was not found."
        }
        $Python = $PythonCommand.Source
    }
} elseif (Test-Path -LiteralPath $ProjectPython -PathType Leaf) {
    $Python = (Resolve-Path -LiteralPath $ProjectPython -ErrorAction Stop).Path
} else {
    $PythonCommand = Get-Command -Name "python" -CommandType Application `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $PythonCommand) {
        throw "No Python executable was found. Run pyOS Setup or pass -PythonPath explicitly."
    }
    $Python = $PythonCommand.Source
}
$Inventory = @(& $Python -m PyInstaller.utils.cliutils.archive_viewer -l $Executable 2>$null)
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller could not inspect the executable archive (exit code $LASTEXITCODE)."
}
$Sensitive = @($Inventory | Select-String -Pattern @(
    'credentials\.json', 'remembered_session', 'gui_settings\.json',
    'cli_settings\.json', 'virtual_drives\.json', 'profiles[/\\]', 'dev\.py', 'workspace\.xml'
))
$FactoryHookFound = @(
    $Inventory | Select-String -Pattern "(^|[', ])factory_runtime(?:\.py)?([', ]|$)"
).Count -gt 0
$ParamikoReleaseVersionFound = @(
    $Inventory | Select-String -Pattern "(^|[/\\', ])paramiko-5\.0\.0\.dist-info(?:[/\\', ]|$)"
).Count -gt 0

$File = Get-Item -LiteralPath $Executable
$FileVersion = [string]$File.VersionInfo.FileVersion
$ProductVersion = [string]$File.VersionInfo.ProductVersion
$VersionMetadataPresent = (
    -not [string]::IsNullOrWhiteSpace($FileVersion) -and
    -not [string]::IsNullOrWhiteSpace($ProductVersion)
)
$SignatureStatus = "Unavailable"
if (Get-Command Get-AuthenticodeSignature -ErrorAction SilentlyContinue) {
    $SignatureStatus = [string](Get-AuthenticodeSignature -LiteralPath $Executable).Status
}
$SignatureValid = $SignatureStatus -eq "Valid"
$ReleaseReady = (
    $Sensitive.Count -eq 0 -and
    $ParamikoReleaseVersionFound -and
    $FactoryHookFound -and
    $VersionMetadataPresent -and
    $SignatureValid
)

[pscustomobject]@{
    Path = $Executable
    SizeMB = [math]::Round($File.Length / 1MB, 2)
    SHA256 = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash
    Version = $FileVersion
    ProductVersion = $ProductVersion
    VersionMetadataPresent = $VersionMetadataPresent
    Signature = $SignatureStatus
    SignatureValid = $SignatureValid
    FactoryHookFound = $FactoryHookFound
    ValidPE = $true
    SensitiveFilesFound = $Sensitive.Count
    PrivacyCheckPassed = ($Sensitive.Count -eq 0)
    ParamikoReleaseVersionFound = $ParamikoReleaseVersionFound
    ReleaseMode = [bool]$Release
    ReleaseReady = $ReleaseReady
}

if ($Sensitive.Count -gt 0) {
    throw "Sensitive runtime files were found in the executable inventory."
}
if ($Release) {
    $Failures = @()
    if (-not $VersionMetadataPresent) { $Failures += "Windows version metadata is missing" }
    if (-not $SignatureValid) { $Failures += "Authenticode signature is not valid ($SignatureStatus)" }
    if (-not $FactoryHookFound) { $Failures += "factory-isolation runtime hook is missing" }
    if (-not $ParamikoReleaseVersionFound) {
        $Failures += "fixed Paramiko 5.0.0 package metadata is missing"
    }
    if ($Failures.Count -gt 0) {
        throw "Release verification failed: $($Failures -join '; ')."
    }
}
