@echo off
rem Double-click me. Runs the read-only FinLedger Tally capture in this folder.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-FinLedger-Capture.ps1" %*
echo.
pause
