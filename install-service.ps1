[CmdletBinding()]
param([string]$TaskName = "CodexForge")
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Daemon = Join-Path $RepoRoot "run-daemon.ps1"
if (-not (Test-Path $Daemon)) { throw "Daemon launcher not found: $Daemon" }

$pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwshCommand) {
    $PowerShell = $pwshCommand.Source
} else {
    $windowsPowerShellCommand = Get-Command powershell -ErrorAction SilentlyContinue
    if (-not $windowsPowerShellCommand) {
        throw "Neither pwsh.exe nor powershell.exe is available."
    }
    $PowerShell = $windowsPowerShellCommand.Source
}

$UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Daemon`"" `
    -WorkingDirectory $RepoRoot
$Triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -AtLogOn -User $UserId
)
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Triggers `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Codex Forge autonomous controller" `
    -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Registered and started scheduled task '$TaskName' for startup and logon."
