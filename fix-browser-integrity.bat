@echo off
setlocal
cd /d "%~dp0"
echo Repairing Windows integrity labels for the business browser. No file contents are changed.
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\fix_browser_integrity.ps1"
echo.
pause
