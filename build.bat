@echo off
chcp 65001 > nul
REM ===================================================================
REM  TapReplay.exe を作るバッチ (Windows で実行)
REM  事前に: pip install pyinstaller
REM ===================================================================

echo PyInstaller を確認中...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller が入っていません。インストールします...
    pip install pyinstaller
)

echo ビルドを開始します...

REM  --onedir で作る（onefile より起動が速く、初回展開の待ちがない）
REM  最初は --console のままにして、エラーが見えるようにしておく
REM  安定したら --console を --windowed に変えるとコンソール窓が消える

REM  PyInstaller は dist\TapReplay を丸ごと作り直すため、
REM  記録済みレシピ(recipes\)を退避してビルド後に戻す
set RECIPES_DIR=%~dp0dist\TapReplay\recipes
set BACKUP_DIR=%TEMP%\TapReplay_recipes_backup
if exist "%RECIPES_DIR%" (
    if exist "%BACKUP_DIR%" rmdir /s /q "%BACKUP_DIR%"
    move "%RECIPES_DIR%" "%BACKUP_DIR%" >nul
)

pyinstaller --noconfirm --onedir --console ^
  --name TapReplay ^
  --icon icon.ico ^
  --collect-all uiautomator2 ^
  --collect-all adbutils ^
  --collect-all cv2 ^
  gui.py

REM  直後に退避しておかないと、この後のmoveコマンドの結果で
REM  errorlevelが上書きされてしまい、pyinstallerの成否を判定できない
set BUILD_ERR=%errorlevel%

if exist "%BACKUP_DIR%" (
    if exist "%RECIPES_DIR%" rmdir /s /q "%RECIPES_DIR%"
    move "%BACKUP_DIR%" "%RECIPES_DIR%" >nul
)

if %BUILD_ERR% neq 0 (
    echo.
    echo !! ビルドに失敗しました。上のエラーを確認してください。
    pause
    exit /b 1
)

echo.
echo 完成: dist\TapReplay\TapReplay.exe
echo.
echo 【重要】adb.exe を dist\TapReplay\ の中にコピーするか、
echo         PATH を通しておいてください（platform-tools 一式）。
echo.
pause
