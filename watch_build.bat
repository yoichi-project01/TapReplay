@echo off
chcp 65001 > nul
REM ===================================================================
REM  watch_build.bat
REM  core.py / gui.py を保存するたびに自動で TapReplay.exe を再ビルドする。
REM  このウィンドウを開いたまま作業してください。終了は Ctrl+C。
REM ===================================================================
powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0watch_build.ps1"
