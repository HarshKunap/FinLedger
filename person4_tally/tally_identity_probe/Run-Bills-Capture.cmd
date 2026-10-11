@echo off
rem Double-click me. Read-only FinLedger open-bills capture (company list, pinned Bill export, company list).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-Bills-Capture.ps1" %*
echo.
pause
