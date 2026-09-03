# ===================================================================
#  watch_build.ps1
#  Watches core.py / gui.py and rebuilds TapReplay.exe automatically
#  whenever one of them changes. Press Ctrl+C to stop.
#
#  ASCII-only on purpose, for consistency with build.bat / watch_build.bat.
# ===================================================================

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Invoke-Build {
    $ts = Get-Date -Format 'HH:mm:ss'
    Write-Host "`n[$ts] Change detected. Building..." -ForegroundColor Cyan

    # PyInstaller rebuilds dist\TapReplay from scratch, so back up the
    # recorded recipes (recipes\) and restore them after the build.
    $recipesDir = Join-Path $root 'dist\TapReplay\recipes'
    $backupDir = Join-Path $env:TEMP 'TapReplay_recipes_backup'
    if (Test-Path $recipesDir) {
        if (Test-Path $backupDir) { Remove-Item $backupDir -Recurse -Force }
        Move-Item $recipesDir $backupDir
    }

    python -m PyInstaller --noconfirm TapReplay.spec

    if (Test-Path $backupDir) {
        if (Test-Path $recipesDir) { Remove-Item $recipesDir -Recurse -Force }
        Move-Item $backupDir $recipesDir
    }

    $ts = Get-Date -Format 'HH:mm:ss'
    if ($LASTEXITCODE -eq 0) {
        # THIRD_PARTY_LICENSES.txt (license notices for bundled third-party
        # binaries) is copied here explicitly: with PyInstaller 6's
        # incremental builds, going through the spec's datas/COLLECT alone
        # is unreliable - the file sometimes ends up under _internal, or is
        # skipped entirely. Same approach as build.bat.
        $licenseSrc = Join-Path $root 'THIRD_PARTY_LICENSES.txt'
        $licenseDst = Join-Path $root 'dist\TapReplay\THIRD_PARTY_LICENSES.txt'
        Copy-Item $licenseSrc $licenseDst -Force
        if (-not (Test-Path $licenseDst)) {
            Write-Host "[$ts] ERROR: Failed to copy THIRD_PARTY_LICENSES.txt into dist\TapReplay\" -ForegroundColor Red
            Write-Host "Watching continues... (Ctrl+C to stop)"
            return
        }
        Write-Host "[$ts] Build succeeded -> dist\TapReplay\TapReplay.exe updated" -ForegroundColor Green
    } else {
        Write-Host "[$ts] Build failed (check the log above)" -ForegroundColor Red
    }
    Write-Host "Watching continues... (Ctrl+C to stop)"
}

Write-Host "Watching: $root"
Write-Host "Rebuilds automatically whenever core.py or gui.py is saved. Ctrl+C to stop."

Invoke-Build

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $root
$watcher.Filter = '*.py'
$watcher.IncludeSubdirectories = $false
$watcher.EnableRaisingEvents = $true

$global:pending = $false

$action = { $global:pending = $true }
Register-ObjectEvent $watcher Changed -Action $action | Out-Null
Register-ObjectEvent $watcher Created -Action $action | Out-Null
Register-ObjectEvent $watcher Renamed -Action $action | Out-Null

try {
    while ($true) {
        Start-Sleep -Seconds 1
        if ($global:pending) {
            # Debounce: coalesce a burst of saves into a single rebuild.
            Start-Sleep -Seconds 2
            $global:pending = $false
            Invoke-Build
        }
    }
}
finally {
    Get-EventSubscriber | Unregister-Event
    $watcher.Dispose()
}
