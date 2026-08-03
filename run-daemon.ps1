[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Join-Path $RepoRoot "forge.toml"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Config) -or -not (Test-Path $Python)) { exit 2 }
$DataRootLine = Select-String -Path $Config -Pattern '^data_root\s*=\s*"(.+)"' | Select-Object -First 1
$DataRoot = if ($DataRootLine) { $DataRootLine.Matches[0].Groups[1].Value } else { Join-Path $RepoRoot ".local-data" }
$LogDir = Join-Path $DataRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
& $Python -u -m codex_forge --config $Config run --daemon *>> (Join-Path $LogDir "daemon-$stamp.log")
exit $LASTEXITCODE
