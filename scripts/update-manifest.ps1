<#
.SYNOPSIS
    Regenerates MANIFEST.sha256 deterministically.

.DESCRIPTION
    Writes the release manifest verified by tests/test_manifest.py. The file set, the record
    grammar and the manifest's own exclusion are the verifier's law; this script reproduces it
    rather than defining a second one, and tests/test_update_manifest.py proves the two agree.

    The manifest covers tracked files only, so a new file must be staged before regeneration.
    An already-current manifest is left untouched, so regeneration is a true no-op.
#>
[CmdletBinding()]
param(
    # Repository to regenerate. Defaults to this script's repository; tests point it at a fixture
    # so regeneration never mutates a developer's working-tree manifest.
    [string]$RepositoryRoot
)

$ErrorActionPreference = "Stop"

if (-not $RepositoryRoot) {
    $RepositoryRoot = Split-Path -Parent $PSScriptRoot
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).ProviderPath

$manifestName = "MANIFEST.sha256"

# Exactly the verifier's file set: tracked files only. An untracked file is scratch state and must
# never reach the manifest. -z emits raw NUL-separated paths, so Git never quotes an unusual name.
$previousEncoding = [Console]::OutputEncoding
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $listing = & git -C $RepositoryRoot ls-files --cached -z
    if ($LASTEXITCODE -ne 0) {
        throw "git ls-files failed in $RepositoryRoot (exit $LASTEXITCODE)."
    }
}
finally {
    [Console]::OutputEncoding = $previousEncoding
}

# The manifest never hashes itself.
$paths = [string[]]@(($listing -join "`n") -split "`0" | Where-Object { $_ -and $_ -ne $manifestName })

# Ordinal, not culture-sensitive: ordering must not depend on the operator's locale.
[Array]::Sort($paths, [System.StringComparer]::Ordinal)

# .NET directly rather than Get-FileHash: the cmdlet depends on module autoloading, which a
# polluted PSModulePath can break under Windows PowerShell.
$sha256 = [System.Security.Cryptography.SHA256]::Create()
try {
    $builder = New-Object System.Text.StringBuilder
    foreach ($path in $paths) {
        $full = [System.IO.Path]::Combine($RepositoryRoot, $path)
        $stream = [System.IO.File]::OpenRead($full)
        try {
            $digest = [System.BitConverter]::ToString($sha256.ComputeHash($stream))
        }
        finally {
            $stream.Dispose()
        }
        [void]$builder.Append($digest.Replace("-", "").ToLowerInvariant()).Append("  ./").Append($path).Append("`n")
    }

    # Explicit LF bytes, no BOM: PowerShell redirection would translate line endings and Windows
    # PowerShell would emit UTF-16, either of which breaks the repository's eol=lf policy.
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    $bytes = $utf8NoBom.GetBytes($builder.ToString())
    $manifestPath = [System.IO.Path]::Combine($RepositoryRoot, $manifestName)

    # Byte-compare before writing: an unchanged manifest keeps its timestamp, so regeneration is a
    # genuine no-op rather than a rewrite that merely happens to produce the same bytes.
    $current = $null
    if ([System.IO.File]::Exists($manifestPath)) {
        $current = [System.BitConverter]::ToString($sha256.ComputeHash([System.IO.File]::ReadAllBytes($manifestPath)))
    }
    if ($null -ne $current -and $current -eq [System.BitConverter]::ToString($sha256.ComputeHash($bytes))) {
        Write-Host "$manifestName already current ($($paths.Count) entries)."
    }
    else {
        [System.IO.File]::WriteAllBytes($manifestPath, $bytes)
        Write-Host "Wrote $manifestName ($($paths.Count) entries)."
    }
}
finally {
    $sha256.Dispose()
}
