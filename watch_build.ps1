# ===================================================================
#  watch_build.ps1
#  core.py / gui.py の変更を検知して自動で TapReplay.exe を再ビルドする。
#  終了するときは Ctrl+C。
# ===================================================================

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Invoke-Build {
    $ts = Get-Date -Format 'HH:mm:ss'
    Write-Host "`n[$ts] 変更を検知。ビルド開始..." -ForegroundColor Cyan

    # PyInstaller は dist\TapReplay を丸ごと作り直すため、
    # 記録済みレシピ(recipes\)を退避してビルド後に戻す
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
        # THIRD_PARTY_LICENSES.txt(同梱している第三者バイナリのライセンス表示)は
        # specのdatas/COLLECT経由だとPyInstaller 6のインクリメンタルビルドで
        # 収録先が_internal配下になったり収録されなかったりと不安定なため、
        # ビルド後にここで確実にトップレベルへコピーする(build.batと同じ対応)
        Copy-Item (Join-Path $root 'THIRD_PARTY_LICENSES.txt') `
            (Join-Path $root 'dist\TapReplay\THIRD_PARTY_LICENSES.txt') -Force
        Write-Host "[$ts] ビルド完了 → dist\TapReplay\TapReplay.exe を更新しました" -ForegroundColor Green
    } else {
        Write-Host "[$ts] !! ビルド失敗（上のログを確認してください）" -ForegroundColor Red
    }
    Write-Host "監視を継続中... (Ctrl+C で終了)"
}

Write-Host "監視開始: $root"
Write-Host "core.py / gui.py を保存するたびに自動で再ビルドします。終了は Ctrl+C。"

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
            # 保存が連続する場合にまとめて1回にする(デバウンス)
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

