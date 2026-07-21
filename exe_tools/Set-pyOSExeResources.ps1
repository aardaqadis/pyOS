[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$IconPath,
    [string]$Version = "2.0.0.0",
    [string]$ProductVersion = "2.0",
    [string]$CompanyName = "pyOS",
    [string]$FileDescription = "pyOS Desktop Environment"
)

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "Build-pyOSExe.ps1") `
    -IconPath $IconPath `
    -Version $Version `
    -ProductVersion $ProductVersion `
    -CompanyName $CompanyName `
    -FileDescription $FileDescription
