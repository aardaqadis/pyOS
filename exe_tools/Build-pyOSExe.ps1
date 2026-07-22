[CmdletBinding()]
param(
    [string]$Version = "2.0.0.0",
    [string]$ProductVersion = "2.0",
    [string]$IconPath = "pyos2.0.png",
    [string]$CompanyName = "pyOS",
    [string]$FileDescription = "pyOS Desktop Environment",
    [string]$OutputName = "pyOS",
    [string]$FactoryNamespace = "pyOS-Release-2.0-Factory",
    [Alias("Python")]
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Resolve-PythonExecutable {
    param([Parameter(Mandatory)] [string]$Candidate)

    if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
        return (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
    }
    if (-not [IO.Path]::IsPathRooted($Candidate)) {
        $ProjectCandidate = Join-Path $ProjectRoot $Candidate
        if (Test-Path -LiteralPath $ProjectCandidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $ProjectCandidate -ErrorAction Stop).Path
        }
    }
    $Command = Get-Command -Name $Candidate -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $Command) {
        return $Command.Source
    }
    throw "Python executable '$Candidate' was not found."
}

$ProjectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonExecutable = Resolve-PythonExecutable -Candidate $PythonPath
} elseif (Test-Path -LiteralPath $ProjectPython -PathType Leaf) {
    $PythonExecutable = (Resolve-Path -LiteralPath $ProjectPython -ErrorAction Stop).Path
} else {
    $ActivePython = Get-Command -Name "python" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $ActivePython) {
        throw "No Python executable was found. Run pyOS Setup or pass -PythonPath explicitly."
    }
    $PythonExecutable = $ActivePython.Source
}
if ($Version -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    throw "Version must contain four numeric parts, for example 2.0.0.0."
}
if ([string]::IsNullOrWhiteSpace($ProductVersion)) {
    throw "ProductVersion cannot be empty."
}
if ([string]::IsNullOrWhiteSpace($CompanyName)) {
    throw "CompanyName cannot be empty."
}
if ([string]::IsNullOrWhiteSpace($FileDescription)) {
    throw "FileDescription cannot be empty."
}
if ($OutputName -notmatch '^[A-Za-z0-9._-]+$') {
    throw "OutputName contains unsupported characters."
}
if ($FactoryNamespace -notmatch '^[A-Za-z0-9._ -]+$') {
    throw "FactoryNamespace contains unsupported characters."
}

$IconCandidate = if ([IO.Path]::IsPathRooted($IconPath)) {
    $IconPath
} else {
    Join-Path $ProjectRoot $IconPath
}
$ResolvedIcon = (Resolve-Path -LiteralPath $IconCandidate -ErrorAction Stop).Path
$SpecFile = Join-Path $ProjectRoot "pyOS.spec"
$FactoryHookTemplate = Join-Path $PSScriptRoot "factory_runtime.py"
if (-not (Test-Path -LiteralPath $SpecFile -PathType Leaf)) {
    throw "PyInstaller specification was not found at $SpecFile."
}
if (-not (Test-Path -LiteralPath $FactoryHookTemplate -PathType Leaf)) {
    throw "Factory runtime-hook template was not found at $FactoryHookTemplate."
}

$ToolBuild = Join-Path $ProjectRoot "build\release-tools"
$WorkPath = Join-Path $ProjectRoot "build\pyinstaller"
$DistPath = Join-Path $ProjectRoot "dist"
New-Item -ItemType Directory -Path $ToolBuild -Force | Out-Null
New-Item -ItemType Directory -Path $DistPath -Force | Out-Null
$VersionFile = Join-Path $ToolBuild "version_info.txt"
$RuntimeHook = Join-Path $ToolBuild "factory_runtime.py"
$parts = $Version.Split('.') | ForEach-Object { [int]$_ }
$tuple = "($($parts -join ', '))"

# ConvertTo-Json produces valid quoted Python string literals for version-file
# metadata while preventing quotes or newlines in caller-provided labels from
# changing the generated resource program.
$CompanyLiteral = ConvertTo-Json -InputObject $CompanyName -Compress
$DescriptionLiteral = ConvertTo-Json -InputObject $FileDescription -Compress
$ProductVersionLiteral = ConvertTo-Json -InputObject $ProductVersion -Compress

@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=$tuple, prodvers=$tuple, mask=0x3f, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', $CompanyLiteral),
    StringStruct('FileDescription', $DescriptionLiteral),
    StringStruct('FileVersion', '$Version'),
    StringStruct('InternalName', '$OutputName'),
    StringStruct('OriginalFilename', '$OutputName.exe'),
    StringStruct('ProductName', 'pyOS'),
    StringStruct('ProductVersion', $ProductVersionLiteral)
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content -LiteralPath $VersionFile -Encoding UTF8

$HookSource = Get-Content -LiteralPath $FactoryHookTemplate -Raw
$DefaultNamespaceLine = 'FACTORY_NAMESPACE = "pyOS-Release-2.0-Factory"'
if (-not $HookSource.Contains($DefaultNamespaceLine)) {
    throw "Factory runtime-hook template does not contain its namespace marker."
}
$NamespaceLiteral = ConvertTo-Json -InputObject $FactoryNamespace -Compress
$HookSource.Replace(
    $DefaultNamespaceLine,
    "FACTORY_NAMESPACE = $NamespaceLiteral"
) | Set-Content -LiteralPath $RuntimeHook -Encoding UTF8

$BuildEnvironment = @{
    PYOS_BUILD_ICON = $ResolvedIcon
    PYOS_BUILD_OUTPUT_NAME = $OutputName
    PYOS_BUILD_RUNTIME_HOOK = $RuntimeHook
    PYOS_BUILD_VERSION_FILE = $VersionFile
}
$PreviousEnvironment = @{}
foreach ($Name in $BuildEnvironment.Keys) {
    $PreviousEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    [Environment]::SetEnvironmentVariable($Name, $BuildEnvironment[$Name], "Process")
}

$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath", $DistPath,
    "--workpath", $WorkPath,
    $SpecFile
)

Push-Location $ProjectRoot
try {
    & $PythonExecutable @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
    foreach ($Name in $BuildEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $PreviousEnvironment[$Name], "Process")
    }
}

$Executable = Join-Path $DistPath "$OutputName.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "PyInstaller completed without creating $Executable."
}
$Hash = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash
$HashFile = "$Executable.sha256"
"$Hash  $OutputName.exe" | Set-Content -LiteralPath $HashFile -Encoding ASCII
Write-Host "Python: $PythonExecutable"
Write-Host "Built: $Executable"
Write-Host "SHA-256: $Hash"
Write-Host "Hash manifest: $HashFile"
