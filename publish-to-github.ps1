[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Repository,
    [string]$Owner
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
        throw "The release repository must already be initialized, committed, and tagged."
    }
    $dirty = @(git status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect Git status." }
    if ($dirty.Count -ne 0) { throw "Refusing to publish a dirty working tree." }
    git rev-parse --verify "refs/tags/v0.2.0" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Required release tag v0.2.0 is missing." }
    git remote get-url origin *> $null
    if ($LASTEXITCODE -eq 0) { throw "Remote 'origin' already exists; refusing to replace it." }
    $fullName = if ($Owner) { "$Owner/$Repository" } else { $Repository }
    gh repo create $fullName --private --source $RepoRoot --remote origin --push
    if ($LASTEXITCODE -ne 0) { throw "GitHub repository creation or branch push failed." }
    git push origin "refs/tags/v0.2.0"
    if ($LASTEXITCODE -ne 0) { throw "Release tag push failed." }
    Write-Host "Published private repository $fullName. The VPS can now git clone/pull it."
} finally {
    Pop-Location
}
