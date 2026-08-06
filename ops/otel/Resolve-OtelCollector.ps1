<#
.SYNOPSIS
Locate an already-installed otelcol-contrib.exe.

.DESCRIPTION
Resolution order is explicit argument, then the OTELCOL_PATH environment variable, then PATH.

This script never downloads, installs, or updates a Collector. Obtaining the binary is a deliberate
operator step: see docs/TELEMETRY.md. When no Collector is found the script fails with the three
places it looked, so the fix is obvious without a network call.

.EXAMPLE
./Resolve-OtelCollector.ps1
.EXAMPLE
./Resolve-OtelCollector.ps1 -CollectorPath 'C:\Tools\otelcol-contrib\otelcol-contrib.exe'
#>
[CmdletBinding()]
param(
    [string]$CollectorPath
)

$ErrorActionPreference = "Stop"

function Assert-NativeExecutable {
    <#
    .SYNOPSIS
    Reject anything that is not a native .exe.

    .DESCRIPTION
    Both the -CollectorPath argument and OTELCOL_PATH are operator-supplied, and Get-Command
    resolves an Application through PATHEXT, which admits .bat, .cmd, and .com. Those are
    interpreted rather than executed directly, so a file named otelcol-contrib.cmd placed earlier
    on PATH would run as a shell script under the caller's identity. Requiring .exe keeps this
    helper to launching a real Collector binary.
    #>
    [CmdletBinding()]
    param([string]$Path, [string]$Source)

    if ([System.IO.Path]::GetExtension($Path).ToLowerInvariant() -ne ".exe") {
        throw "$Source must point at a native .exe; refusing to run: $Path"
    }
    return $Path
}

function Resolve-OtelCollectorPath {
    [CmdletBinding()]
    param([string]$CollectorPath)

    if ($CollectorPath) {
        if (-not (Test-Path -LiteralPath $CollectorPath -PathType Leaf)) {
            throw "Collector not found at the path supplied with -CollectorPath: $CollectorPath"
        }
        return Assert-NativeExecutable -Path (Resolve-Path -LiteralPath $CollectorPath).Path -Source "-CollectorPath"
    }

    $fromEnvironment = $env:OTELCOL_PATH
    if ($fromEnvironment) {
        if (-not (Test-Path -LiteralPath $fromEnvironment -PathType Leaf)) {
            throw "OTELCOL_PATH is set but does not point at a file: $fromEnvironment"
        }
        return Assert-NativeExecutable -Path (Resolve-Path -LiteralPath $fromEnvironment).Path -Source "OTELCOL_PATH"
    }

    $fromPath = Get-Command otelcol-contrib -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($fromPath) {
        return Assert-NativeExecutable -Path $fromPath.Source -Source "otelcol-contrib on PATH"
    }

    throw @"
otelcol-contrib.exe was not found. Codex Forge does not download it.

Looked in:
  1. the -CollectorPath argument
  2. the OTELCOL_PATH environment variable
  3. PATH

Install the OpenTelemetry Collector Contrib distribution yourself, then re-run with -CollectorPath
or set OTELCOL_PATH. Forge runs normally without it; telemetry simply has nowhere to go.
"@
}

if ($MyInvocation.InvocationName -ne '.') {
    $resolved = Resolve-OtelCollectorPath -CollectorPath $CollectorPath
    Write-Output $resolved
}
