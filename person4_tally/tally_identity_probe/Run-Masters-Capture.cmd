@echo off
rem Double-click me. Read-only FinLedger ledger-masters capture (company list, pinned Ledger export, company list).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-Masters-Capture.ps1" %*
echo.
pause
