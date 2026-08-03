[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$codexHome = Join-Path $env:USERPROFILE ".codex"
$policyPath = Join-Path $codexHome "RTK.md"
New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
$policy = @"
# RTK command-output policy

Use ``rtk <command>`` for supported noisy commands such as Git, searches, tests, linters, and builds.
Use ``rtk proxy <command>`` when exact raw output is diagnostically necessary, and preserve raw logs on disk.
If RTK is unsupported or rejected by an execution policy (especially in a read-only review), run the exact
direct read-only command instead. Do not block a task solely because RTK cannot wrap a command.

On this VPS, use ``rtk proxy pytest ...``, not compressed ``rtk pytest ...``: RTK 0.44.2 incorrectly summarizes
quiet passing suites as "No tests collected." Reassess this exception only after an RTK upgrade and benchmark.
"@
[System.IO.File]::WriteAllText($policyPath, $policy, [System.Text.UTF8Encoding]::new($false))
Write-Host "Configured validated RTK policy at $policyPath"
