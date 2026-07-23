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
$BuildScript = Join-Path $PSScriptRoot "exe_tools\Build-pyOSSetup.ps1"

# This is the single public build entry point; implementation lives in exe_tools.
& $BuildScript @PSBoundParameters
