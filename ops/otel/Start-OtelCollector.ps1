<#
.SYNOPSIS
Run a locally installed OpenTelemetry Collector against the repository configuration.

.DESCRIPTION
Starts otelcol-contrib.exe in the foreground with ops/otel/collector-config.yaml, which listens on
127.0.0.1 and exports only to the debug exporter. Stop it with Ctrl+C.

This script deliberately does not:
  * download or install a Collector binary,
  * register a Windows service or a scheduled task,
  * modify forge.toml or touch Forge state.

With -ValidateOnly the Collector's own `validate` subcommand checks the configuration and exits.
That requires the binary; the repository's pytest suite checks the same file structurally without
one.

.EXAMPLE
./Start-OtelCollector.ps1
.EXAMPLE
./Start-OtelCollector.ps1 -CollectorPath 'C:\Tools\otelcol-contrib\otelcol-contrib.exe' -ValidateOnly
#>
[CmdletBinding()]
param(
    [string]$CollectorPath,
    [string]$ConfigPath,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptRoot "Resolve-OtelCollector.ps1")

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ScriptRoot "collector-config.yaml"
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Collector configuration not found: $ConfigPath"
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path

$collector = Resolve-OtelCollectorPath -CollectorPath $CollectorPath

if ($ValidateOnly) {
    Write-Host "Validating $ConfigPath"
    & $collector validate --config $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        # Older Collector builds predate `validate`. Report that rather than implying the
        # configuration itself was rejected.
        Write-Warning "The installed Collector returned $LASTEXITCODE. Builds without a 'validate' subcommand cannot check the configuration this way."
    }
    exit $LASTEXITCODE
}

Write-Host "Starting Collector: $collector"
Write-Host "Configuration:      $ConfigPath"
Write-Host "Listening on 127.0.0.1:4317 (gRPC) and 127.0.0.1:4318 (HTTP). Spans print below. Ctrl+C to stop."
& $collector --config $ConfigPath
exit $LASTEXITCODE
