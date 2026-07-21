[CmdletBinding()]
param(
    [string]$Version = "2.0.0.0",
    [string]$ProductVersion = "2.0",
    [string]$IconPath = "pyos2.0.png",
    [string]$CompanyName = "pyOS",
    [string]$FileDescription = "pyOS Desktop Environment",
    [string]$OutputName = "pyOS",
    [string]$FactoryNamespace = "pyOS-Release-2.0-Factory"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python was not found at $Python. Run pyOS Setup first."
}
if ($Version -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    throw "Version must contain four numeric parts, for example 2.0.0.0."
}
if ($OutputName -notmatch '^[A-Za-z0-9._-]+$') {
    throw "OutputName contains unsupported characters."
}
if ($FactoryNamespace -notmatch '^[A-Za-z0-9._ -]+$') {
    throw "FactoryNamespace contains unsupported characters."
}

$ResolvedIcon = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot $IconPath)).Path
$ToolBuild = Join-Path $ProjectRoot "build\release-tools"
New-Item -ItemType Directory -Path $ToolBuild -Force | Out-Null
$VersionFile = Join-Path $ToolBuild "version_info.txt"
$RuntimeHook = Join-Path $ToolBuild "factory_runtime.py"
$parts = $Version.Split('.') | ForEach-Object { [int]$_ }
$tuple = "($($parts -join ', '))"

@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=$tuple, prodvers=$tuple, mask=0x3f, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', '$CompanyName'),
    StringStruct('FileDescription', '$FileDescription'),
    StringStruct('FileVersion', '$Version'),
    StringStruct('InternalName', '$OutputName'),
    StringStruct('OriginalFilename', '$OutputName.exe'),
    StringStruct('ProductName', 'pyOS'),
    StringStruct('ProductVersion', '$ProductVersion')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content -LiteralPath $VersionFile -Encoding UTF8

@"
import json
import os
import sys
from pathlib import Path

local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
root = local / "$FactoryNamespace"
data = root / "data"
config_path = root / "install.json"
os.environ["PYOS_CONFIG_FILE"] = str(config_path)
if not config_path.exists():
    root.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "install_dir": str(Path(sys.executable).resolve().parent),
        "data_dir": str(data),
        "downloads_dir": str(Path.home() / "Downloads"),
        "drive_b_dir": str(data / "Drive_B"),
        "enabled_apps": None,
        "configured": True
    }, indent=2), encoding="utf-8")
    temporary.replace(config_path)
"@ | Set-Content -LiteralPath $RuntimeHook -Encoding UTF8

$arguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
    "--specpath", $ToolBuild,
    "--name", $OutputName, "--icon", $ResolvedIcon,
    "--version-file", $VersionFile, "--runtime-hook", $RuntimeHook,
    "--add-data", "$(Join-Path $ProjectRoot 'pyOSgui.py');.",
    "--add-data", "$(Join-Path $ProjectRoot 'pyOScli.py');.",
    "--add-data", "$(Join-Path $ProjectRoot 'pyos_config.py');.",
    "--add-data", "$(Join-Path $ProjectRoot 'pyos_auth.py');.",
    "--add-data", "$(Join-Path $ProjectRoot 'pyos_updater.py');.",
    "--add-data", "$(Join-Path $ProjectRoot 'setup.py');.",
    "--add-data", "$(Join-Path $ProjectRoot 'README.md');.",
    "--add-data", "$(Join-Path $ProjectRoot 'LICENSE.md');.",
    "--add-data", "$ResolvedIcon;.", "--hidden-import", "chess", "--hidden-import", "vlc",
    "--collect-all", "fido2", "--collect-all", "PIL", "--collect-all", "mido",
    "--collect-all", "pygame", "--collect-all", "psutil", "--collect-all", "tkinterweb",
    (Join-Path $ProjectRoot "pyOSgui.py")
)

Push-Location $ProjectRoot
try {
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
}

$Executable = Join-Path $ProjectRoot "dist\$OutputName.exe"
$Hash = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash
Write-Host "Built: $Executable"
Write-Host "SHA-256: $Hash"
