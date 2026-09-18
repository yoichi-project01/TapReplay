[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DistDir,
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$OutputZip
)

# Called from build.bat after a successful build, to package
# dist\TapReplay\ into a single distributable ZIP. Prints "OK:<path>" on
# success or "FAIL:<message>" on failure and exits 0/1 accordingly, the
# same convention create_shortcut.ps1 uses - build.bat parses that prefix.
#
# Only an explicit whitelist of files is copied into the ZIP staging
# folder (never the whole dist\TapReplay\ as-is). This is deliberate:
# dist\TapReplay\ can accumulate developer-only state from test runs
# (recipes\, settings.ini, playback logs) that must never ship to users.
# Picking the guide text file by extension (*.txt, minus the licenses
# file) instead of hardcoding its Japanese filename also sidesteps any
# console/script code-page mismatch when this runs from build.bat.
try {
    if (-not (Test-Path -LiteralPath "$DistDir\TapReplay.exe")) {
        throw "$DistDir\TapReplay.exe not found"
    }
    if (-not (Test-Path -LiteralPath "$DistDir\_internal")) {
        throw "$DistDir\_internal not found"
    }

    $stageRoot = Join-Path $env:TEMP "TapReplay_zip_stage"
    $stageApp = Join-Path $stageRoot "TapReplay"
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $stageApp -Force | Out-Null

    Copy-Item -LiteralPath "$DistDir\TapReplay.exe" -Destination $stageApp
    Copy-Item -LiteralPath "$DistDir\_internal" -Destination $stageApp -Recurse

    if (Test-Path -LiteralPath "$DistDir\THIRD_PARTY_LICENSES.txt") {
        Copy-Item -LiteralPath "$DistDir\THIRD_PARTY_LICENSES.txt" -Destination $stageApp
    }
    if (Test-Path -LiteralPath "$RepoRoot\LICENSE") {
        Copy-Item -LiteralPath "$RepoRoot\LICENSE" -Destination $stageApp
    }

    # Guide text file(s) at the repo root, e.g. the Japanese-named
    # "read this first" file for end users. Matched by extension so this
    # script never needs to spell out a non-ASCII filename. requirements.txt
    # is excluded alongside THIRD_PARTY_LICENSES.txt - it also matches
    # "*.txt" and was confirmed (via a real build.bat run) to otherwise
    # ship a developer-only pip dependency list to end users.
    Get-ChildItem -LiteralPath $RepoRoot -Filter "*.txt" -File |
        Where-Object { $_.Name -ne "THIRD_PARTY_LICENSES.txt" -and $_.Name -ne "requirements.txt" } |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $stageApp }

    if (Test-Path -LiteralPath $OutputZip) {
        Remove-Item -LiteralPath $OutputZip -Force
    }
    $outDir = Split-Path -Parent $OutputZip
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }

    # Passing the TapReplay folder itself (not "TapReplay\*") keeps it as
    # the single top-level entry in the archive, so extracting the ZIP
    # produces a TapReplay\ folder instead of scattering files.
    Compress-Archive -Path $stageApp -DestinationPath $OutputZip -Force

    Remove-Item -LiteralPath $stageRoot -Recurse -Force

    Write-Output "OK:$OutputZip"
    exit 0
} catch {
    Write-Output "FAIL:$($_.Exception.Message)"
    exit 1
}
