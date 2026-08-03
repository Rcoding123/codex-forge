[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [switch]$SkipOptionalTools
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Add-UserPath([string]$PathToAdd) {
    $resolved = [System.IO.Path]::GetFullPath($PathToAdd)
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { $userPath = "" }
    $parts = @($userPath -split ';' | Where-Object { $_ })
    if ($parts -notcontains $resolved) {
        $newPath = (($parts + $resolved) -join ';')
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    }
    if (($env:Path -split ';') -notcontains $resolved) {
        $env:Path = "$resolved;$env:Path"
    }
}

function Get-GitHubAsset(
    [string]$Repository,
    [string]$AssetPattern,
    [string]$Destination,
    [switch]$ExtractZip
) {
    $headers = @{"User-Agent"="CodexForge"; "Accept"="application/vnd.github+json"}
    $release = Invoke-RestMethod -Headers $headers -Uri "https://api.github.com/repos/$Repository/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -match $AssetPattern } | Select-Object -First 1
    if (-not $asset) { throw "No matching release asset for $Repository using pattern $AssetPattern" }

    $tempRoot = Join-Path $env:TEMP ("codex-forge-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    try {
        $download = Join-Path $tempRoot $asset.name
        Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $asset.browser_download_url -OutFile $download
        if ($ExtractZip) {
            Expand-Archive -Force -Path $download -DestinationPath $tempRoot
            $source = Get-ChildItem -Path $tempRoot -Recurse -File | Where-Object { $_.Name -eq (Split-Path -Leaf $Destination) } | Select-Object -First 1
            if (-not $source) { throw "Expected $(Split-Path -Leaf $Destination) was not found in $($asset.name)." }
            Copy-Item -Force $source.FullName $Destination
        } else {
            Copy-Item -Force $download $Destination
        }
    } finally {
        Remove-Item -Recurse -Force $tempRoot -ErrorAction SilentlyContinue
    }
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Add-UserPath $InstallDir

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing portable uv..."
    Get-GitHubAsset -Repository "astral-sh/uv" -AssetPattern '^uv-x86_64-pc-windows-msvc\.zip$' -Destination (Join-Path $InstallDir "uv.exe") -ExtractZip
}

if (-not $SkipOptionalTools) {
    if (-not (Get-Command rg -ErrorAction SilentlyContinue)) {
        Write-Host "Installing portable ripgrep..."
        Get-GitHubAsset -Repository "BurntSushi/ripgrep" -AssetPattern 'x86_64-pc-windows-msvc\.zip$' -Destination (Join-Path $InstallDir "rg.exe") -ExtractZip
    }
    if (-not (Get-Command fd -ErrorAction SilentlyContinue)) {
        Write-Host "Installing portable fd..."
        Get-GitHubAsset -Repository "sharkdp/fd" -AssetPattern 'x86_64-pc-windows-msvc\.zip$' -Destination (Join-Path $InstallDir "fd.exe") -ExtractZip
    }
    if (-not (Get-Command jq -ErrorAction SilentlyContinue)) {
        Write-Host "Installing portable jq..."
        Get-GitHubAsset -Repository "jqlang/jq" -AssetPattern '^jq-windows-amd64\.exe$' -Destination (Join-Path $InstallDir "jq.exe")
    }
}

Write-Host "Portable tooling ready at $InstallDir"
