[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Warning "Codex is not on PATH; Token Optimizer marketplace was not added."
    exit 1
}

codex plugin marketplace add alexgreensh/token-optimizer
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not add the Token Optimizer marketplace. The Forge controller remains operational without it."
    exit 0
}

# Newer Codex releases may support direct CLI installation. Try it without becoming interactive.
codex plugin install token-optimizer@alexgreensh-token-optimizer
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Marketplace added, but this Codex build requires one TUI install: run codex, open /plugins, and install Token Optimizer."
}
