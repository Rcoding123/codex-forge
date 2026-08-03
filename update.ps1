[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $RepoRoot
try {
    git pull --ff-only
    & (Join-Path $RepoRoot "bootstrap.ps1") -SkipScheduledTask
} finally {
    Pop-Location
}
