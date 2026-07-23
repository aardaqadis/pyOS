[CmdletBinding()]
param(
    [string]$Version = "2.0.0.0",
    [string]$ProductVersion = "2.0",
    [string]$IconPath = "pyos2.0.png",
    [string]$CompanyName = "pyOS",
    [string]$FileDescription = "pyOS Setup",
    [string]$OutputName = "pyOS-Setup",
    [string]$FactoryNamespace = "pyOS-Release-2.0-Factory",
    [Alias("Python")]
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeBuilder = Join-Path $PSScriptRoot "Build-pyOSExe.ps1"
$SetupSpec = Join-Path $ProjectRoot "pyOS-Setup.spec"

function Resolve-PythonExecutable {
    param([string]$Candidate)
    if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
        $command = Get-Command -Name $Candidate -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $command) { return $command.Source }
        throw "Python executable '$Candidate' was not found."
    }
    $projectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $projectPython).Path
    }
    $command = Get-Command -Name python -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) { throw "No Python executable was found. Pass -PythonPath explicitly." }
    return $command.Source
}

$PythonExecutable = Resolve-PythonExecutable $PythonPath
$common = @{
    Version = $Version
    ProductVersion = $ProductVersion
    IconPath = $IconPath
    CompanyName = $CompanyName
    FactoryNamespace = $FactoryNamespace
    PythonPath = $PythonExecutable
}

& $RuntimeBuilder @common -FileDescription "pyOS Desktop Environment" -OutputName "pyOS"
if ($LASTEXITCODE -ne 0) { throw "GUI runtime build failed with exit code $LASTEXITCODE." }

$previousEntry = $env:PYOS_BUILD_ENTRY
$previousConsole = $env:PYOS_BUILD_CONSOLE
try {
    $env:PYOS_BUILD_ENTRY = "pyOScli.py"
    $env:PYOS_BUILD_CONSOLE = "1"
    & $RuntimeBuilder @common -FileDescription "pyOS Command Center" -OutputName "pyOS-cli"
    if ($LASTEXITCODE -ne 0) { throw "CLI runtime build failed with exit code $LASTEXITCODE." }
} finally {
    $env:PYOS_BUILD_ENTRY = $previousEntry
    $env:PYOS_BUILD_CONSOLE = $previousConsole
}

$iconCandidate = if ([IO.Path]::IsPathRooted($IconPath)) { $IconPath } else { Join-Path $ProjectRoot $IconPath }
$resolvedIcon = (Resolve-Path -LiteralPath $iconCandidate).Path
$versionFile = Join-Path $ProjectRoot "build\release-tools\version_info.txt"
$workPath = Join-Path $ProjectRoot "build\setup-pyinstaller"
$distPath = Join-Path $ProjectRoot "dist"
$previousIcon = $env:PYOS_BUILD_ICON
$previousVersion = $env:PYOS_BUILD_VERSION_FILE
$previousOutput = $env:PYOS_SETUP_OUTPUT_NAME
try {
    $env:PYOS_BUILD_ICON = $resolvedIcon
    $env:PYOS_BUILD_VERSION_FILE = $versionFile
    $env:PYOS_SETUP_OUTPUT_NAME = $OutputName
    Push-Location $ProjectRoot
    try {
        & $PythonExecutable -m PyInstaller --noconfirm --clean --distpath $distPath --workpath $workPath $SetupSpec
        if ($LASTEXITCODE -ne 0) { throw "Setup executable build failed with exit code $LASTEXITCODE." }
    } finally { Pop-Location }
} finally {
    $env:PYOS_BUILD_ICON = $previousIcon
    $env:PYOS_BUILD_VERSION_FILE = $previousVersion
    $env:PYOS_SETUP_OUTPUT_NAME = $previousOutput
}

$setupExecutable = Join-Path $distPath "$OutputName.exe"
if (-not (Test-Path -LiteralPath $setupExecutable -PathType Leaf)) {
    throw "PyInstaller completed without creating $setupExecutable."
}
$hash = (Get-FileHash -LiteralPath $setupExecutable -Algorithm SHA256).Hash
"$hash  $OutputName.exe" | Set-Content -LiteralPath "$setupExecutable.sha256" -Encoding ASCII
foreach ($intermediate in @(
    (Join-Path $distPath "pyOS.exe"), (Join-Path $distPath "pyOS.exe.sha256"),
    (Join-Path $distPath "pyOS-cli.exe"), (Join-Path $distPath "pyOS-cli.exe.sha256")
)) {
    if (Test-Path -LiteralPath $intermediate -PathType Leaf) { Remove-Item -LiteralPath $intermediate -Force }
}
Write-Host "Built: $setupExecutable"
Write-Host "SHA-256: $hash"