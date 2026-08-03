[CmdletBinding()]
param([string]$TaskName = "CodexForge")
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Join-Path $RepoRoot "forge.toml"
$EscapedConfig = [regex]::Escape($Config)

function Get-ForgeDaemonProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match '(?i)python(?:\.exe)?"?\s+.*-m\s+codex_forge\s+--config\s+' -and
        $_.CommandLine -match $EscapedConfig -and
        $_.CommandLine -match '(?i)\srun\s+--daemon(?:\s|$)'
    })
}

function Get-DescendantProcessIds {
    param([int[]]$RootIds)
    $all = @(Get-CimInstance Win32_Process)
    $selected = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($id in $RootIds) { [void]$selected.Add($id) }
    do {
        $added = $false
        foreach ($process in $all) {
            if ($selected.Contains([int]$process.ParentProcessId) -and $selected.Add([int]$process.ProcessId)) {
                $added = $true
            }
        }
    } while ($added)
    @($selected)
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName
}

for ($index = 0; $index -lt 20; $index++) {
    if ((Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') { break }
    Start-Sleep -Milliseconds 250
}

$daemons = @(Get-ForgeDaemonProcesses)
if ($daemons.Count -gt 0) {
    $processIds = Get-DescendantProcessIds -RootIds @($daemons.ProcessId)
    foreach ($processId in ($processIds | Sort-Object -Descending)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Milliseconds 500
if (@(Get-ForgeDaemonProcesses).Count -ne 0) {
    throw "A verified Codex Forge daemon process remained after the controlled stop."
}

Start-ScheduledTask -TaskName $TaskName
for ($index = 0; $index -lt 40; $index++) {
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') { break }
    Start-Sleep -Milliseconds 250
}
if ((Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') {
    throw "Scheduled task '$TaskName' did not reach Running state."
}

Write-Host "Restarted '$TaskName' with one verified Forge daemon process tree."
