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

pyinstaller --noconfirm --onedir --console ^
  --name TapReplay ^
  --collect-all uiautomator2 ^
  --collect-all adbutils ^
  --collect-all cv2 ^
  gui.py

if errorlevel 1 (
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
