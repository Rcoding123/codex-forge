[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$DataRoot
)

$ErrorActionPreference = "Stop"
$codexHome = Join-Path $env:USERPROFILE ".codex"
New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot "codex-logs") | Out-Null
$configPath = Join-Path $codexHome "config.toml"
if (Test-Path $configPath) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item $configPath "$configPath.backup-$stamp"
}
$logPath = (Join-Path $DataRoot "codex-logs").Replace('\','/')
$codexConfig = @"
# Managed by Codex Forge. Existing configuration was backed up before replacement.
approval_policy = "never"
sandbox_mode = "danger-full-access"
model = "gpt-5.6-terra"
model_reasoning_effort = "medium"
log_dir = "$logPath"

[features]
hooks = true
goals = true
memories = true
multi_agent = false
"@
[System.IO.File]::WriteAllText($configPath, $codexConfig, [System.Text.UTF8Encoding]::new($false))
Write-Host "Configured zero-prompt Codex at $configPath"
