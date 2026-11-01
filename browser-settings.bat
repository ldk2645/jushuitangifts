@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "main.py" --browser-settings
  exit /b 0
)
if exist "runtime\pythonw.exe" (
  start "" "runtime\pythonw.exe" "main.py" --browser-settings
  exit /b 0
)
echo Python runtime not found. Please run setup.ps1 first.
pause
exit /b 1
