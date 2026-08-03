[CmdletBinding()]
param([string]$TaskName = "CodexForge")
schtasks.exe /Delete /TN $TaskName /F
