[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Repository,
    [string]$Owner,
    [switch]$Public
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI is missing. Install gh or winget first."
    }
    winget install --id GitHub.cli --exact --silent --accept-package-agreements --accept-source-agreements
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

Push-Location $RepoRoot
try {
    gh auth status | Out-Null
    if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
        git init -b main
        git add -A
        git commit -m "Initial Codex Forge release"
    }
    $visibility = if ($Public) { "--public" } else { "--private" }
    $fullName = if ($Owner) { "$Owner/$Repository" } else { $Repository }
    gh repo create $fullName $visibility --source $RepoRoot --remote origin --push
    Write-Host "Published $fullName. The VPS can now git clone/pull this repository."
} finally {
    Pop-Location
}
