[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$AdapterArgs)
$ErrorActionPreference = 'Stop'
$adapterPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $adapterPython)) { throw 'Run bootstrap.ps1 first.' }
& $adapterPython -m codex_forge.adapter @AdapterArgs
exit $LASTEXITCODE
