<#
.SYNOPSIS
Locate an already-installed otelcol-contrib.exe.

.DESCRIPTION
Resolution order is explicit argument, then the OTELCOL_PATH environment variable, then PATH.

Invoke this script (`& ./Resolve-OtelCollector.ps1 -CollectorPath ...`); do not dot-source it. A
dot-source runs this param block in the *caller's* scope, so `. ./Resolve-OtelCollector.ps1` with no
arguments rebinds the caller's own $CollectorPath to an empty string and silently discards the path
the operator supplied.

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

function Test-PortableExecutable {
    <#
    .SYNOPSIS
    Return $true when the file carries a Windows PE header.

    .DESCRIPTION
    The extension alone proves nothing: any file can be renamed to .exe. A PE begins with the "MZ"
    DOS signature, and the 4-byte little-endian value at offset 0x3C points at the "PE\0\0" NT
    signature. Checking both keeps this helper from handing a script, a batch file, or a truncated
    download to the call operator.
    #>
    [CmdletBinding()]
    param([string]$Path)

    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        $dosHeader = [byte[]]::new(64)
        if ($stream.Read($dosHeader, 0, 64) -ne 64) { return $false }
        if ($dosHeader[0] -ne 0x4D -or $dosHeader[1] -ne 0x5A) { return $false }

        $ntOffset = [System.BitConverter]::ToInt32($dosHeader, 0x3C)
        if ($ntOffset -lt 64 -or $ntOffset -gt ($stream.Length - 4)) { return $false }

        $stream.Position = $ntOffset
        $ntSignature = [byte[]]::new(4)
        if ($stream.Read($ntSignature, 0, 4) -ne 4) { return $false }
        return $ntSignature[0] -eq 0x50 -and $ntSignature[1] -eq 0x45 -and $ntSignature[2] -eq 0 -and $ntSignature[3] -eq 0
    }
    finally {
        $stream.Dispose()
    }
}

function Assert-NativeExecutable {
    <#
    .SYNOPSIS
    Reject anything that is not a native .exe.

    .DESCRIPTION
    Both the -CollectorPath argument and OTELCOL_PATH are operator-supplied, and Get-Command
    resolves an Application through PATHEXT, which admits .bat, .cmd, and .com. Those are
    interpreted rather than executed directly, so a file named otelcol-contrib.cmd placed earlier
    on PATH would run as a shell script under the caller's identity. Requiring .exe keeps this
    helper to launching a real Collector binary, and the PE check keeps a renamed script from
    slipping past the extension test.
    #>
    [CmdletBinding()]
    param([string]$Path, [string]$Source)

    if ([System.IO.Path]::GetExtension($Path).ToLowerInvariant() -ne ".exe") {
        throw "$Source must point at a native .exe; refusing to run: $Path"
    }
    if (-not (Test-PortableExecutable -Path $Path)) {
        throw "$Source is not a native Windows executable despite its .exe name; refusing to run: $Path"
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

    $fromPath = @(Get-Command otelcol-contrib -CommandType Application -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Source } |
        Select-Object -Unique)
    if ($fromPath.Count -gt 1) {
        # Picking the first match would make the effective Collector depend on PATH order, which is
        # exactly the ambiguity an operator needs to resolve deliberately.
        throw "otelcol-contrib is ambiguous on PATH; pass -CollectorPath or set OTELCOL_PATH to choose one of:`n  $($fromPath -join "`n  ")"
    }
    if ($fromPath.Count -eq 1) {
        return Assert-NativeExecutable -Path $fromPath[0] -Source "otelcol-contrib on PATH"
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
