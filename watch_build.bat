@echo off
REM ===================================================================
REM  watch_build.bat
REM  Rebuilds TapReplay.exe automatically whenever core.py or gui.py
REM  is saved. Leave this window open. Press Ctrl+C to stop.
REM
REM  ASCII-only on purpose - see build.bat for why.
REM ===================================================================
powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0watch_build.ps1"
