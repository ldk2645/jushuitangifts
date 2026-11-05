@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "TEMP=%~dp0.tmp"
set "TMP=%TEMP%"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not exist "%TEMP%" mkdir "%TEMP%"
"%PY%" -X utf8 "tools\inspect_honor_page.py"
pause
