[CmdletBinding()]
param(
    [string]$InstallDir = "$env:USERPROFILE\.local\bin"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

try {
    $release = Invoke-RestMethod -Headers @{"User-Agent"="CodexForge"} -Uri "https://api.github.com/repos/rtk-ai/rtk/releases/latest"
    $asset = $release.assets | Where-Object {
        $_.name -match '(?i)(windows.*(x86_64|amd64)|(x86_64|amd64).*windows).*\.zip$'
    } | Select-Object -First 1
    if (-not $asset) {
        throw "No Windows x64 RTK release asset found."
    }
    $temp = Join-Path $env:TEMP ("rtk-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $temp | Out-Null
    $zip = Join-Path $temp $asset.name
    Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $zip
    Expand-Archive -Force -Path $zip -DestinationPath $temp
    $binary = Get-ChildItem -Path $temp -Recurse -Filter rtk.exe | Select-Object -First 1
    if (-not $binary) { throw "rtk.exe was not present in the downloaded archive." }
    Copy-Item -Force $binary.FullName (Join-Path $InstallDir "rtk.exe")
    Remove-Item -Recurse -Force $temp
} catch {
    Write-Warning "Release install failed: $($_.Exception.Message). Falling back to Cargo."
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install --id Rustlang.Rustup --exact --silent --accept-package-agreements --accept-source-agreements
            $env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
        } else {
            throw "Cargo is unavailable and winget is not installed. Install RTK manually."
        }
    }
    cargo install --git https://github.com/rtk-ai/rtk --locked
    $cargoRtk = Join-Path $env:USERPROFILE ".cargo\bin\rtk.exe"
    if (-not (Test-Path $cargoRtk)) { throw "Cargo completed but rtk.exe was not found." }
    Copy-Item -Force $cargoRtk (Join-Path $InstallDir "rtk.exe")
}

$currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $currentUserPath) { $currentUserPath = "" }
if (($currentUserPath -split ';') -notcontains $InstallDir) {
    [Environment]::SetEnvironmentVariable("Path", (($currentUserPath.TrimEnd(';') + ";" + $InstallDir).Trim(';')), "User")
}
$env:Path = "$InstallDir;$env:Path"

& (Join-Path $InstallDir "rtk.exe") init -g --codex
& (Join-Path $InstallDir "rtk.exe") --version
