[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$ForgeArgs
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Config = Join-Path $RepoRoot "forge.toml"
if (-not (Test-Path $Python)) { throw "Forge is not bootstrapped. Run .\bootstrap.ps1 first." }
& $Python -m codex_forge --config $Config @ForgeArgs
exit $LASTEXITCODE
