[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "Medium")]
param([switch]$IncludeExecutable)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$Targets = @([IO.Path]::GetFullPath((Join-Path $ProjectRoot "build")))
if ($IncludeExecutable) {
    $Targets += [IO.Path]::GetFullPath((Join-Path $ProjectRoot "dist"))
}
foreach ($Target in $Targets) {
    if (-not $Target.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar)) {
        throw "Refusing to clean a path outside the project: $Target"
    }
    if ((Test-Path -LiteralPath $Target) -and $PSCmdlet.ShouldProcess($Target, "Remove generated output")) {
        Remove-Item -LiteralPath $Target -Recurse -Force
        Write-Host "Removed $Target"
    }
}
