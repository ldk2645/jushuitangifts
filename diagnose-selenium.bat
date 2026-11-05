@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "TEMP=%~dp0.tmp"
set "TMP=%TEMP%"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not exist "%TEMP%" mkdir "%TEMP%"
echo Checking independent blank browser windows. No login or orders.
echo.
"%PY%" -X utf8 "tools\check_browser_startup.py" --browser chrome
echo.
"%PY%" -X utf8 "tools\check_browser_startup.py" --browser edge
echo.
echo If a browser reports SessionNotCreatedException or tab crashed, double-click fix-browser-integrity.bat.
echo Result: %~dp0.tmp\browser-startup-result.json
pause
