$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project Python was not found at $Python. Run pyOS Setup first."
}

Set-Location -LiteralPath $ProjectRoot

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name pyOS `
    --icon "pyos1.2.png" `
    --add-data "pyOSgui.py;." `
    --add-data "pyOScli.py;." `
    --add-data "pyos_config.py;." `
    --add-data "pyos_auth.py;." `
    --add-data "pyos_updater.py;." `
    --add-data "setup.py;." `
    --add-data "README.md;." `
    --add-data "LICENSE.md;." `
    --add-data "pyos1.2.png;." `
    --hidden-import chess `
    --hidden-import vlc `
    --collect-all fido2 `
    --collect-all PIL `
    --collect-all mido `
    --collect-all pygame `
    --collect-all psutil `
    --collect-all tkinterweb `
    "pyOSgui.py"

if ($LASTEXITCODE -ne 0) {
    throw "pyOS executable build failed with exit code $LASTEXITCODE."
}

Write-Host "Built executable: $(Join-Path $ProjectRoot 'dist\pyOS.exe')"
