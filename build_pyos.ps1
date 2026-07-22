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
$BuildScript = Join-Path $PSScriptRoot "exe_tools\Build-pyOSExe.ps1"

# Keep this legacy entry point as a parameter-forwarding compatibility wrapper.
# All packaging behavior belongs to Build-pyOSExe.ps1 and pyOS.spec.
& $BuildScript @PSBoundParameters
