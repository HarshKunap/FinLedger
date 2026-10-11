@echo off
rem Double-click me. Read-only FinLedger company-isolation check (needs two companies loaded in TallyPrime).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-Isolation-Check.ps1" %*
echo.
pause
