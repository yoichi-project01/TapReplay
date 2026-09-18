@echo off
REM ===================================================================
REM  Builds TapReplay.exe (run on Windows)
REM
REM  This file is ASCII-only on purpose. Batch files are parsed using
REM  whatever console code page is active at run time, and on a machine
REM  whose default code page differs from the one this file was saved
REM  with, non-ASCII bytes inside REM comments / echo text can corrupt
REM  parenthesis matching and get executed as bogus commands. Keeping
REM  this file plain ASCII avoids that class of bug entirely.
REM
REM  Design notes:
REM   - Every python/pip call below uses "python -m ..." so it always
REM     runs against the same interpreter that "python" resolves to.
REM     A bare "pip" command can silently point at a different Python
REM     install than "python" does, which is what let a past build
REM     complete on a machine without opencv-python installed, and the
REM     resulting exe crashed with "No module named 'cv2'".
REM   - Every python call is prefixed with "call". On most machines
REM     "python" is an .exe and this makes no difference, but on some
REM     (pyenv-win shims, some corporate wrappers, the Microsoft Store
REM     stub) "python" resolves to a .bat/.cmd file, and invoking a
REM     batch file from another batch file without "call" hands control
REM     to it permanently - the rest of this script would silently never
REM     run. Confirmed this failure mode while testing this script.
REM   - PyInstaller is intentionally NOT listed in requirements.txt.
REM     requirements.txt describes what the shipped app needs at
REM     runtime; PyInstaller is a build-only tool for developers, so
REM     it is installed separately below instead.
REM   - Every step that can fail saves its errorlevel into its own
REM     variable immediately after it runs, before any other command
REM     executes. Reusing one shared/late-checked errorlevel let
REM     earlier failures (pyinstaller, recipes restore, license copy)
REM     go unnoticed in the past.
REM   - The Python version check has both a floor and a ceiling
REM     (PY_MIN_MINOR / PY_MAX_MINOR below). A too-new Python is just as
REM     fatal as a too-old one here: opencv-python, PySide6, adbutils and
REM     PyInstaller are only published as Windows wheels for a specific
REM     range of Python versions, and pip falling back to a source build
REM     of opencv/PySide6 needs a C++ toolchain that is not realistic to
REM     set up on Windows - it fails, or hangs, partway through instead
REM     of giving a clear error.
REM   - If no usable Python is found, this script can install one via
REM     winget, but only after an explicit Y/N prompt (see :py_offer_install
REM     below). This repo is public, so a script that silently installs a
REM     Python interpreter onto someone else's machine is not acceptable -
REM     it always asks first, and never retries automatically.
REM   - The desktop shortcut step (end of the script) never fails the
REM     build: the exe and _internal folder already exist in dist\ at that
REM     point, so a shortcut problem is only ever reported as a warning.
REM   - The release ZIP step (also end of the script) is the same: it
REM     never fails the build for the same reason, and can be skipped
REM     entirely by running "build.bat nozip" (handy for repeated builds
REM     during development, where the ZIP is just wasted time).
REM   - A running TapReplay.exe is checked for twice: once up front
REM     (fails fast, before wasting minutes on a dependency install) and
REM     again right before the recipes backup (in case someone started
REM     the exe during that wait). Both checks jump to the same
REM     :tapreplay_running label instead of being wrapped in a callable
REM     subroutine - "exit /b" inside a "call"ed label only returns from
REM     that call, it does not stop the whole script, so a shared
REM     "goto" target is used instead to get a real hard stop.
REM   - The recipes backup/restore (inside "Building with PyInstaller")
REM     always checks the errorlevel of "move" now. It used to not check
REM     it at all: if TapReplay.exe (or anything else) held a file open
REM     under dist\TapReplay\recipes\, the backup "move" failed silently
REM     and the script carried on to run PyInstaller anyway - which then
REM     tried to delete that same locked folder itself and crashed with
REM     a Python PermissionError (WinError 32). Confirmed on a real
REM     build: gui.py's playback log is opened via plain open(path, "a"),
REM     which on Windows does not set FILE_SHARE_DELETE, so any playback
REM     log still open blocks rename/delete of the whole recipes\ tree.
REM     The backup folder name also now includes a timestamp instead of
REM     being fixed - a fixed name meant that if a *previous* run's
REM     restore ever failed and left recipes sitting in the backup
REM     folder, the *next* run's unconditional "rmdir /s /q" on that same
REM     fixed path would silently destroy them before making its own
REM     backup. A timestamped name means two runs never collide, so nothing
REM     ever gets silently deleted by a later run.
REM ===================================================================

cd /d "%~dp0"
set "PY_CMD=python"
set "SKIP_ZIP=0"
if /i "%~1"=="nozip" set "SKIP_ZIP=1"
if /i "%~1"=="--no-zip" set "SKIP_ZIP=1"

REM Repo root without a trailing backslash. A trailing backslash directly
REM before a closing quote (e.g. "%~dp0") can be misread as escaping that
REM quote when passed as a command-line argument to another program
REM (including powershell.exe), so it is stripped once here and reused
REM below instead of passing "%~dp0" itself as an argument.
set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"

echo ===================================================================
echo  TapReplay build
echo ===================================================================

echo.
echo [1/8] Checking for a running TapReplay.exe...
tasklist /FI "IMAGENAME eq TapReplay.exe" 2>nul | find /I "TapReplay.exe" >nul
if not errorlevel 1 goto :tapreplay_running
echo No running TapReplay.exe found.

echo.
echo [2/8] Checking Python environment...
where python >nul 2>&1
if errorlevel 1 goto :no_python

for /f "delims=" %%V in ('call python --version 2^>^&1') do set "PY_VERSION_STR=%%V"
if not defined PY_VERSION_STR goto :no_python
echo Python version:
echo %PY_VERSION_STR%
echo Python executable:
call python -c "import sys; print(sys.executable)"

for /f "tokens=1,2" %%A in ('call python -c "import sys; print(sys.version_info[0], sys.version_info[1])" 2^>^&1') do (
    set "PY_MAJOR=%%A"
    set "PY_MINOR=%%B"
)

REM Verified working range. Widen PY_MAX_MINOR (and update the wheel list
REM in :py_too_new below) once opencv-python/PySide6/adbutils/PyInstaller
REM all ship wheels for the newer Python.
set "PY_MIN_MINOR=11"
set "PY_MAX_MINOR=13"

if not defined PY_MAJOR goto :no_python
if %PY_MAJOR% LSS 3 goto :py_too_old
if %PY_MAJOR% EQU 3 if %PY_MINOR% LSS %PY_MIN_MINOR% goto :py_too_old
if %PY_MAJOR% GTR 3 goto :py_too_new
if %PY_MAJOR% EQU 3 if %PY_MINOR% GTR %PY_MAX_MINOR% goto :py_too_new
goto :py_ok

:tapreplay_running
echo.
echo ERROR: TapReplay.exe is currently running.
echo.
echo PyInstaller needs to delete and rebuild dist\TapReplay\ from
echo scratch, and it cannot do that while the running exe - or a file it
echo has open under dist\TapReplay\recipes\ (for example an active
echo playback log) - is locked. Building over a running instance risks
echo losing or corrupting recorded recipes.
echo.
echo Close TapReplay.exe, then re-run build.bat.
pause
exit /b 1

:no_python
echo.
echo ERROR: could not run "python". Install Python 3.11 or later and
echo make sure "python" runs it (check PATH).
pause
exit /b 1

:py_too_old
echo.
echo ERROR: Python 3.11 or later is required.
echo requirements.txt needs numpy 2.4+ (Python 3.11+), and PySide6 6.11+
echo and pillow 12.0+ (Python 3.10+).
echo Stopping before installing anything, so a Python version mismatch
echo is reported clearly instead of failing partway through install.
pause
exit /b 1

:py_too_new
echo.
echo ERROR: %PY_VERSION_STR% is newer than this project supports.
echo Verified working range: Python 3.11 - 3.13.
echo.
echo As of this writing, PyPI has no Windows wheel for this Python version
echo of the following packages this project depends on:
echo   - opencv-python 5.0.0.93
echo   - PySide6 6.11.2
echo   - adbutils 2.12.0
echo   - PyInstaller 6.22.2
echo pip would fall back to building these from source. opencv-python and
echo PySide6 need a C++ build toolchain that is not realistic to set up on
echo Windows, so that build fails - or hangs for a long time - partway
echo through, instead of giving a clear error up front.
echo.
echo Recommended fix: install Python 3.12 or 3.13 alongside your current
echo Python - you do not need to remove it. The "py" launcher lets you
echo pick a version per command, for example:
echo   py -3.13 -m pip install -r requirements.txt

set "PY_ALT="
where py >nul 2>&1
if errorlevel 1 goto :py_offer_install

call py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY_ALT=3.13"
if not defined PY_ALT (
    call py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY_ALT=3.12"
)
if not defined PY_ALT (
    call py -3.11 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY_ALT=3.11"
)
if not defined PY_ALT goto :py_offer_install

echo.
echo Found "py -%PY_ALT%" already installed on this machine.
choice /C YN /N /M "Use py -%PY_ALT% to continue this build now (Y/N)? "
if errorlevel 2 goto :py_too_new_stop
if not errorlevel 1 goto :py_too_new_stop

set "PY_CMD=py -%PY_ALT%"
echo Using py -%PY_ALT% for the rest of this build.
goto :py_ok

REM No usable Python 3.11-3.13 was found via the py launcher (or the py
REM launcher itself is missing). Offer to install one with winget, but
REM only with an explicit Y/N - this script must never install anything
REM silently.
:py_offer_install
where winget >nul 2>&1
if errorlevel 1 goto :py_no_winget

echo.
choice /C YN /N /M "Install Python 3.13 now (via winget) (Y/N)? "
if errorlevel 2 goto :py_install_declined
if not errorlevel 1 goto :py_install_declined

echo.
echo Installing Python 3.13 via winget - this can take a few minutes...
call winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements
set "WINGET_ERR=%errorlevel%"

REM Check actual usability rather than trusting winget's own exit code:
REM winget returns non-zero ("no applicable update") when Python 3.13 is
REM already installed but just not registered with the py launcher -
REM confirmed on a real machine during testing. In that case py -3.13
REM might already work (or a shell restart fixes it), so a winget
REM "failure" here does not necessarily mean nothing can be done.
echo.
echo Checking whether Python 3.13 is usable now...
call py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3.13"
    echo Using py -3.13 for the rest of this build.
    goto :py_ok
)

if not "%WINGET_ERR%"=="0" (
    echo.
    echo ERROR: winget install failed ^(errorlevel %WINGET_ERR%^) and
    echo py -3.13 is still not usable.
    echo Install Python 3.12 or 3.13 manually from https://www.python.org/downloads/
    echo and re-run build.bat.
    pause
    exit /b 1
)

:py_install_needs_new_shell
echo.
echo Python 3.13 was installed, but this window does not see it on PATH
echo yet (winget just registered it). Close this window, open a new
echo Command Prompt, and run build.bat again.
pause
exit /b 1

:py_no_winget
echo.
echo winget is not available on this machine, so Python cannot be
echo installed automatically. Install Python 3.12 or 3.13 manually from
echo https://www.python.org/downloads/ and re-run build.bat.
pause
exit /b 1

:py_install_declined
echo.
echo OK, not installing anything. To build later, install Python 3.12 or
echo 3.13 manually from https://www.python.org/downloads/ (or run this
echo script again and choose Y), then re-run build.bat.
pause
exit /b 1

:py_too_new_stop
pause
exit /b 1

:py_ok
echo Python version OK.

echo.
echo [3/8] Checking whether dependencies are already installed...
call %PY_CMD% -c "import cv2, numpy, PIL, PySide6, uiautomator2, adbutils, PyInstaller" >nul 2>&1
if errorlevel 1 goto :do_install
echo All required packages are already importable. Skipping install.
goto :verify_imports

:do_install
echo Some packages are missing or broken. Installing dependencies...
echo (this can take a few minutes on the first run)

echo.
echo Installing packages from requirements.txt...
call %PY_CMD% -m pip install -r requirements.txt
set "REQ_ERR=%errorlevel%"
if not "%REQ_ERR%"=="0" (
    echo.
    echo ERROR: "%PY_CMD% -m pip install -r requirements.txt" failed.
    pause
    exit /b 1
)

echo.
echo Installing PyInstaller...
call %PY_CMD% -m pip install pyinstaller
set "PYI_ERR=%errorlevel%"
if not "%PYI_ERR%"=="0" (
    echo.
    echo ERROR: "%PY_CMD% -m pip install pyinstaller" failed.
    pause
    exit /b 1
)

:verify_imports
echo.
echo [4/8] Verifying that dependencies actually import...
call %PY_CMD% -c "import cv2, numpy, PIL, PySide6, uiautomator2, adbutils" >nul 2>&1
if errorlevel 1 goto :import_failed
echo OK: all required packages are importable.
goto :do_build

:import_failed
echo.
echo ERROR: the packages could not be imported, even though installation
echo reported success (or they were already reported as importable).
echo This usually means "python" and "pip" point at different
echo environments. Python executable currently in use:
call %PY_CMD% -c "import sys; print(sys.executable)"
echo.
echo Run this manually to see the exact error:
echo   %PY_CMD% -c "import cv2, numpy, PIL, PySide6, uiautomator2, adbutils"
pause
exit /b 1

:backup_failed
echo.
echo Build stopped before running PyInstaller - see the details above
echo for exactly which file (and, if it could be determined, which
echo program) is blocking it.
pause
exit /b 1

:do_build
echo.
echo [5/8] Building with PyInstaller...

REM Build settings live in TapReplay.spec (shared with watch_build.ps1).
REM Edit TapReplay.spec instead of adding --onedir/icon/data flags here.

REM Re-check right before the point of no return: the dependency install
REM above can take several minutes, long enough for someone to start the
REM app while waiting.
tasklist /FI "IMAGENAME eq TapReplay.exe" 2>nul | find /I "TapReplay.exe" >nul
if not errorlevel 1 goto :tapreplay_running

REM PyInstaller rebuilds dist\TapReplay from scratch, so back up the
REM recorded recipes (recipes\) here and restore them after the build.
REM The backup folder name includes a timestamp (not a fixed name): a
REM fixed name meant that if a *previous* run's restore ever failed and
REM left recipes stranded in the backup folder, this run's own backup
REM step would silently rmdir /s /q that same path (to make room for its
REM own backup) before anyone noticed - destroying the stranded recipes.
REM A per-run name means two runs never collide, so nothing prior is
REM ever touched, let alone deleted.
REM
REM The actual copy/verify work is delegated to backup_recipes.ps1
REM (invoked directly, not through a "for /f ... do set" capture, so its
REM possibly multi-line diagnostics print straight to the console instead
REM of only the last line surviving). It copies file-by-file rather than
REM moving the whole folder in one shot, so a single locked file no
REM longer fails the entire backup with just an opaque "Access is
REM denied" - it reports exactly which file, and (best-effort, via the
REM Restart Manager API) which process holds it. See that script for the
REM full reasoning, including why even a locked *.log still blocks the
REM build (PyInstaller's own cleanup cannot tolerate anything left behind
REM either) despite being fine to lose on its own.
set "RECIPES_DIR=%~dp0dist\TapReplay\recipes"
set "BACKUP_DIR="
if exist "%RECIPES_DIR%" (
    set "BACKUP_STAMP="
    for /f "delims=" %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "BACKUP_STAMP=%%T"
    if not defined BACKUP_STAMP set "BACKUP_STAMP=fallback_%RANDOM%%RANDOM%"
    set "BACKUP_DIR=%TEMP%\TapReplay_recipes_backup_%BACKUP_STAMP%"
    if exist "%BACKUP_DIR%" rmdir /s /q "%BACKUP_DIR%"

    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backup_recipes.ps1" -SourceDir "%RECIPES_DIR%" -BackupDir "%BACKUP_DIR%"
    set "BACKUP_ERR=%errorlevel%"
    if not "%BACKUP_ERR%"=="0" goto :backup_failed
)

call %PY_CMD% -m PyInstaller --noconfirm TapReplay.spec

REM Capture errorlevel immediately: any command that runs after this
REM (move, copy, ...) would otherwise overwrite it before we can check
REM whether PyInstaller actually succeeded.
set "BUILD_ERR=%errorlevel%"

REM Always attempt the restore, win or lose, so a PyInstaller failure
REM never strands the backup on top of failing the build.
set "RESTORE_ERR=0"
if defined BACKUP_DIR (
    if exist "%RECIPES_DIR%" rmdir /s /q "%RECIPES_DIR%"
    move "%BACKUP_DIR%" "%RECIPES_DIR%" >nul
    set "RESTORE_ERR=%errorlevel%"
)

if not "%BUILD_ERR%"=="0" (
    echo.
    echo Build failed. Check the errors above.
    if not "%RESTORE_ERR%"=="0" (
        echo.
        echo In addition, your recorded recipes could not be restored.
        echo They were NOT lost - they are still sitting at:
        echo   %BACKUP_DIR%
        echo Move that folder back to dist\TapReplay\recipes\ manually.
    )
    pause
    exit /b 1
)

if not "%RESTORE_ERR%"=="0" (
    echo.
    echo ERROR: the exe built successfully, but recorded recipes could not
    echo be restored into dist\TapReplay\recipes\ afterward ^(likely
    echo something still has a file open under that folder^). Your
    echo recipes were NOT lost - they are still sitting at:
    echo   %BACKUP_DIR%
    echo Move that folder back to dist\TapReplay\recipes\ manually, then
    echo re-run build.bat if you want a clean dist\TapReplay\ with
    echo recipes already in place.
    pause
    exit /b 1
)

REM THIRD_PARTY_LICENSES.txt (license notices for bundled third-party
REM binaries) is copied here explicitly: with PyInstaller 6's incremental
REM builds, going through the spec's datas/COLLECT alone is unreliable -
REM the file sometimes ends up under _internal, or is skipped entirely.
copy /y "%~dp0THIRD_PARTY_LICENSES.txt" "%~dp0dist\TapReplay\THIRD_PARTY_LICENSES.txt" >nul
set "LICENSE_ERR=%errorlevel%"
if not exist "%~dp0dist\TapReplay\THIRD_PARTY_LICENSES.txt" (
    echo.
    echo ERROR: Failed to copy THIRD_PARTY_LICENSES.txt into dist\TapReplay\.
    pause
    exit /b 1
)

echo.
echo [6/8] Verifying build output...
set "DIST_DIR=%~dp0dist\TapReplay"
set "DIST_OK=1"

if not exist "%DIST_DIR%\TapReplay.exe" (
    echo ERROR: missing %DIST_DIR%\TapReplay.exe
    set "DIST_OK=0"
)
if not exist "%DIST_DIR%\_internal" (
    echo ERROR: missing %DIST_DIR%\_internal
    set "DIST_OK=0"
)
if not exist "%DIST_DIR%\_internal\cv2" (
    echo ERROR: missing %DIST_DIR%\_internal\cv2 - opencv-python was not bundled
    set "DIST_OK=0"
)
if not exist "%DIST_DIR%\_internal\PySide6" (
    echo ERROR: missing %DIST_DIR%\_internal\PySide6
    set "DIST_OK=0"
)
if not exist "%DIST_DIR%\_internal\adbutils" (
    echo ERROR: missing %DIST_DIR%\_internal\adbutils
    set "DIST_OK=0"
)

if "%DIST_OK%"=="0" (
    echo.
    echo Build output verification failed. Not marking this build as complete.
    pause
    exit /b 1
)

echo.
echo [7/8] Creating desktop shortcut...

REM This never fails the build: dist\TapReplay already verified OK above,
REM so a shortcut problem is only a warning, not a build failure.
set "SHORTCUT_RESULT="
for /f "delims=" %%L in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" -TargetPath "%DIST_DIR%\TapReplay.exe" -WorkingDirectory "%DIST_DIR%" -IconPath "%~dp0icon.ico" 2^>^&1') do set "SHORTCUT_RESULT=%%L"

set "SHORTCUT_PATH=%SHORTCUT_RESULT:~3%"
echo %SHORTCUT_RESULT% | findstr /b "OK:" >nul 2>&1
if not errorlevel 1 (
    echo Desktop shortcut created: %SHORTCUT_PATH%
) else (
    echo WARNING: could not create the desktop shortcut automatically.
    echo You can still run the app from dist\TapReplay\TapReplay.exe
)

echo.
if "%SKIP_ZIP%"=="1" (
    echo [8/8] Skipping release ZIP ^(build.bat was run with a nozip argument^).
    goto :zip_done
)
echo [8/8] Creating release ZIP...

REM VERSION lives in core.py (single source of truth - also shown in the
REM window title and startup log). Dependencies were already verified
REM importable above, so "import core" here is safe.
set "APP_VERSION="
for /f "delims=" %%V in ('call %PY_CMD% -c "import core; print(core.VERSION)" 2^>^&1') do set "APP_VERSION=%%V"
echo %APP_VERSION%| findstr /r "^[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*$" >nul 2>&1
if errorlevel 1 (
    echo WARNING: could not read core.VERSION ^(got "%APP_VERSION%"^). Using 0.0.0.
    set "APP_VERSION=0.0.0"
)

set "ZIP_PATH=%~dp0dist\TapReplay_v%APP_VERSION%.zip"
set "ZIP_RESULT="
for /f "delims=" %%L in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_release_zip.ps1" -DistDir "%DIST_DIR%" -RepoRoot "%ROOT_DIR%" -OutputZip "%ZIP_PATH%" 2^>^&1') do set "ZIP_RESULT=%%L"

echo %ZIP_RESULT% | findstr /b "OK:" >nul 2>&1
if not errorlevel 1 (
    echo Release ZIP created: %ZIP_PATH%
) else (
    echo WARNING: could not create the release ZIP automatically.
    echo %ZIP_RESULT%
    echo You can still distribute dist\TapReplay\ manually.
)

:zip_done
echo.
echo Done: dist\TapReplay\TapReplay.exe
echo.
echo Copy the whole dist\TapReplay\ folder to distribute it - it runs as-is.
echo (adb.exe is bundled. No manual copying or PATH setup is needed.)
if not "%SKIP_ZIP%"=="1" (
    echo Or hand out dist\TapReplay_v%APP_VERSION%.zip instead - it unpacks to
    echo the same thing, and is what gets attached to a GitHub Release
    echo ^(see RELEASE.md^).
)
echo.
pause
