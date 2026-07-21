[CmdletBinding()]
param([string]$Path = "dist\pyOS.exe")

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Executable = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot $Path)).Path
Start-Process -FilePath $Executable
Write-Host "Started $Executable"
