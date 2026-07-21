[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param()

$ErrorActionPreference = "Stop"
$ExpectedName = "pyOS-Release-2.0-Factory"
$Target = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA $ExpectedName))
if ((Split-Path $Target -Leaf) -ne $ExpectedName) {
    throw "Refusing to reset unexpected path: $Target"
}
if (-not (Test-Path -LiteralPath $Target)) {
    Write-Host "The factory profile is already clean: $Target"
    return
}
if ($PSCmdlet.ShouldProcess($Target, "Permanently remove the isolated pyOS factory profile")) {
    Remove-Item -LiteralPath $Target -Recurse -Force
    Write-Host "Factory profile reset. The next launch will request a new account."
}
