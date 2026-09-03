@echo off
REM ===================================================================
REM  Builds TapReplay.exe (run on Windows)
REM  Prerequisite: pip install pyinstaller
REM
REM  This file is ASCII-only on purpose. Batch files are parsed using
REM  whatever console code page is active at run time, and on a machine
REM  whose default code page differs from the one this file was saved
REM  with, non-ASCII bytes inside REM comments / echo text can corrupt
REM  parenthesis matching and get executed as bogus commands. Keeping
REM  this file plain ASCII avoids that class of bug entirely.
REM ===================================================================

echo Checking for PyInstaller...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Installing...
    pip install pyinstaller
)

echo Starting build...

REM Build settings live in TapReplay.spec (shared with watch_build.ps1).
REM Edit TapReplay.spec instead of adding --onedir/icon/data flags here.

REM PyInstaller rebuilds dist\TapReplay from scratch, so back up the
REM recorded recipes (recipes\) here and restore them after the build.
set "RECIPES_DIR=%~dp0dist\TapReplay\recipes"
set "BACKUP_DIR=%TEMP%\TapReplay_recipes_backup"
if exist "%RECIPES_DIR%" (
    if exist "%BACKUP_DIR%" rmdir /s /q "%BACKUP_DIR%"
    move "%RECIPES_DIR%" "%BACKUP_DIR%" >nul
)

pyinstaller --noconfirm TapReplay.spec

REM Capture errorlevel immediately: any command that runs after this
REM (move, copy, ...) would otherwise overwrite it before we can check
REM whether PyInstaller actually succeeded.
set "BUILD_ERR=%errorlevel%"

if exist "%BACKUP_DIR%" (
    if exist "%RECIPES_DIR%" rmdir /s /q "%RECIPES_DIR%"
    move "%BACKUP_DIR%" "%RECIPES_DIR%" >nul
)

if %BUILD_ERR% neq 0 (
    echo.
    echo Build failed. Check the errors above.
    pause
    exit /b 1
)

REM THIRD_PARTY_LICENSES.txt (license notices for bundled third-party
REM binaries) is copied here explicitly: with PyInstaller 6's incremental
REM builds, going through the spec's datas/COLLECT alone is unreliable -
REM the file sometimes ends up under _internal, or is skipped entirely.
copy /y "%~dp0THIRD_PARTY_LICENSES.txt" "%~dp0dist\TapReplay\THIRD_PARTY_LICENSES.txt" >nul
if not exist "%~dp0dist\TapReplay\THIRD_PARTY_LICENSES.txt" (
    echo.
    echo ERROR: Failed to copy THIRD_PARTY_LICENSES.txt into dist\TapReplay\.
    pause
    exit /b 1
)

echo.
echo Done: dist\TapReplay\TapReplay.exe
echo.
echo Copy the whole dist\TapReplay\ folder to distribute it - it runs as-is.
echo (adb.exe is bundled. No manual copying or PATH setup is needed.)
echo.
pause
