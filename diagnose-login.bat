@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" (
  echo Python runtime not found. Run setup.ps1 or use the portable package.
  pause
  exit /b 1
)
echo Diagnosing the platform login windows. No order or gift task is submitted.
echo This may open two browser windows and may take about a minute.
echo.
"%PY%" -X utf8 "tools\check_login_startup.py" %*
echo.
echo Report: %~dp0.tmp\login-diagnostic.json
pause
