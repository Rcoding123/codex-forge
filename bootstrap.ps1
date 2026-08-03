[CmdletBinding()]
param(
    [string]$ForgeRoot = "",
    [string]$DataRoot = "",
    [switch]$SkipToolInstall,
    [switch]$SkipScheduledTask,
    [switch]$SkipTokenOptimizer,
    [switch]$SkipCodexLogin
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Resolve-StorageRoots {
    $systemRoot = "$($env:SystemDrive)\"
    $otherDrives = @(Get-PSDrive -PSProvider FileSystem | Where-Object {
        $_.Root -and $_.Root -ne $systemRoot
    } | Sort-Object Name)

    if (-not $script:ForgeRoot) {
        if ($otherDrives.Count -ge 2) {
            $script:ForgeRoot = Join-Path $otherDrives[0].Root "CodexForge"
        } else {
            $script:ForgeRoot = Join-Path $systemRoot "CodexForgeWork"
        }
    }
    if (-not $script:DataRoot) {
        if ($otherDrives.Count -ge 2) {
            $script:DataRoot = Join-Path $otherDrives[1].Root "CodexData"
        } elseif ($otherDrives.Count -eq 1) {
            $script:DataRoot = Join-Path $otherDrives[0].Root "CodexData"
        } else {
            $script:DataRoot = Join-Path $systemRoot "CodexData"
        }
    }
}

function Install-WingetPackage([string]$Id) {
    winget install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget failed to install $Id (exit $LASTEXITCODE)." }
}

function Install-NodeDirect {
    Write-Host "Installing Node.js LTS from the official signed MSI..."
    $releases = Invoke-RestMethod "https://nodejs.org/dist/index.json"
    $release = $releases | Where-Object { $_.lts -and $_.files -contains "win-x64-msi" } | Select-Object -First 1
    if (-not $release) { throw "Could not locate a Node.js LTS Windows x64 MSI." }
    $version = $release.version
    $msi = Join-Path $env:TEMP "node-$version-x64.msi"
    Invoke-WebRequest -UseBasicParsing -Uri "https://nodejs.org/dist/$version/node-$version-x64.msi" -OutFile $msi
    $signature = Get-AuthenticodeSignature $msi
    if ($signature.Status -ne "Valid") { throw "Node.js MSI signature is not valid: $($signature.Status)" }
    $process = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Node.js MSI failed with exit $($process.ExitCode)." }
    Refresh-Path
}

function Install-GitDirect {
    Write-Host "Installing Git for Windows from its signed release installer..."
    $headers = @{"User-Agent"="CodexForge"; "Accept"="application/vnd.github+json"}
    $release = Invoke-RestMethod -Headers $headers -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -match '64-bit\.exe$' -and $_.name -notmatch 'PortableGit' } | Select-Object -First 1
    if (-not $asset) { throw "Could not locate the Git for Windows x64 installer." }
    $installer = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $asset.browser_download_url -OutFile $installer
    $signature = Get-AuthenticodeSignature $installer
    if ($signature.Status -ne "Valid") { throw "Git installer signature is not valid: $($signature.Status)" }
    $process = Start-Process $installer -ArgumentList "/VERYSILENT /NORESTART /NOCANCEL /SP-" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Git installer failed with exit $($process.ExitCode)." }
    Refresh-Path
}

Resolve-StorageRoots
Write-Host "== Codex Forge bootstrap ==" -ForegroundColor Cyan
Write-Host "Work root: $ForgeRoot"
Write-Host "Data root: $DataRoot"

New-Item -ItemType Directory -Force -Path $ForgeRoot, $DataRoot | Out-Null
$directories = @(
    "$ForgeRoot\repositories", "$ForgeRoot\worktrees", "$ForgeRoot\tools", "$ForgeRoot\build-cache",
    "$DataRoot\inbox", "$DataRoot\evidence", "$DataRoot\knowledge", "$DataRoot\logs", "$DataRoot\codex-logs"
)
New-Item -ItemType Directory -Force -Path $directories | Out-Null
$portableTools = Join-Path $ForgeRoot "tools\bin"

if (-not $SkipToolInstall) {
    $hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        if ($hasWinget) { Install-WingetPackage "Git.Git" } else { Install-GitDirect }
        Refresh-Path
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        if ($hasWinget) { Install-WingetPackage "OpenJS.NodeJS.LTS" } else { Install-NodeDirect }
        Refresh-Path
    }
    if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
        Write-Host "Installing OpenAI Codex CLI..."
        & npm.cmd install --global @openai/codex@latest
        if ($LASTEXITCODE -ne 0) { throw "Codex CLI installation failed." }
        Refresh-Path
    }

    & (Join-Path $RepoRoot "scripts\install-portable-tools.ps1") -InstallDir $portableTools
    Refresh-Path

    if (-not (Get-Command ast-grep -ErrorAction SilentlyContinue) -and -not (Get-Command sg -ErrorAction SilentlyContinue)) {
        Write-Host "Installing ast-grep..."
        & npm.cmd install --global @ast-grep/cli
        if ($LASTEXITCODE -ne 0) { throw "ast-grep installation failed." }
        Refresh-Path
    }
    if (-not (Get-Command rtk -ErrorAction SilentlyContinue)) {
        & (Join-Path $RepoRoot "scripts\install-rtk.ps1") -InstallDir $portableTools
        Refresh-Path
    } else {
        rtk init -g --codex
    }
}

$venv = Join-Path $RepoRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { $pythonCommand = Get-Command py -ErrorAction SilentlyContinue }
    if ($pythonCommand) {
        & $pythonCommand.Source -m venv $venv
    } else {
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            & (Join-Path $RepoRoot "scripts\install-portable-tools.ps1") -InstallDir $portableTools -SkipOptionalTools
            Refresh-Path
        }
        Write-Host "Installing a private Python 3.12 runtime through uv..."
        & uv python install 3.12
        if ($LASTEXITCODE -ne 0) { throw "uv could not install Python 3.12." }
        & uv venv --python 3.12 --seed $venv
        if ($LASTEXITCODE -ne 0) { throw "uv could not create the Forge virtual environment." }
    }
}
if (-not (Test-Path $venvPython)) { throw "Forge Python environment was not created at $venvPython." }

Write-Host "Installing Forge Python build and runtime dependencies..."
& $venvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Failed to install pip, setuptools, and wheel into the Forge environment." }

& $venvPython -m pip install -e "${RepoRoot}[dev]"
if ($LASTEXITCODE -ne 0) { throw "Failed to install Codex Forge and its development dependencies." }

$configPath = Join-Path $RepoRoot "forge.toml"
$forgeRootToml = $ForgeRoot.Replace('\','/')
$dataRootToml = $DataRoot.Replace('\','/')
$forgeConfig = @"
[paths]
workspace_root = "$forgeRootToml/worktrees"
data_root = "$dataRootToml"
repository_root = "$forgeRootToml/repositories"
inbox_root = "$dataRootToml/inbox"
database_path = "$dataRootToml/forge.db"
evidence_root = "$dataRootToml/evidence"
knowledge_root = "$dataRootToml/knowledge"

[codex]
executable = "codex"
sandbox = "danger-full-access"
approval_policy = "never"
extra_args = []
disabled_features = ["apps", "browser_use", "browser_use_external", "goals", "image_generation", "memories", "multi_agent", "plugins", "skill_search"]

[models]
implementation = "gpt-5.6-terra"
review = "gpt-5.6-terra"
escalation = "gpt-5.6-sol"

[reasoning]
implementation = "medium"
review = "medium"
escalation = "high"

[workers]
max_parallel = 2
poll_seconds = 15

[limits]
task_timeout_minutes = 180
max_repair_cycles = 2
max_identical_failures = 2
max_task_tokens = 2000000
max_output_chars = 12000
"@
[System.IO.File]::WriteAllText($configPath, $forgeConfig, [System.Text.UTF8Encoding]::new($false))

& (Join-Path $RepoRoot "scripts\configure-codex.ps1") -DataRoot $DataRoot
if (Get-Command rtk -ErrorAction SilentlyContinue) { rtk init -g --codex }
& (Join-Path $RepoRoot "scripts\configure-rtk-policy.ps1")

if (-not $SkipCodexLogin) {
    codex login status *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Codex authentication is the only interactive setup step." -ForegroundColor Yellow
        codex login
        if ($LASTEXITCODE -ne 0) { throw "Codex authentication did not complete successfully." }
    }
}

if (-not $SkipTokenOptimizer -and -not $SkipCodexLogin) {
    & (Join-Path $RepoRoot "scripts\install-token-optimizer.ps1")
}

& $venvPython -m codex_forge --config $configPath init
if ($LASTEXITCODE -ne 0) { throw "Codex Forge initialization failed." }

& $venvPython -m pytest "$RepoRoot\tests"
if ($LASTEXITCODE -ne 0) { throw "Codex Forge tests failed." }

& $venvPython -m ruff check "$RepoRoot"
if ($LASTEXITCODE -ne 0) { throw "Codex Forge lint validation failed." }

if (-not $SkipScheduledTask) {
    & (Join-Path $RepoRoot "install-service.ps1")
}

Write-Host ""
Write-Host "Bootstrap complete." -ForegroundColor Green
Write-Host "Health check: .\forge.ps1 doctor"
if ($SkipScheduledTask) {
    Write-Host "Start now: .\forge.ps1 run --daemon"
} else {
    Write-Host "The CodexForge daemon has been started and is registered for startup and logon."
}
