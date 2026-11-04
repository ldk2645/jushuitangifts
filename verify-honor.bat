@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "TEMP=%~dp0.tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
".venv\Scripts\python.exe" -X utf8 "tools\inspect_honor_page.py"
pause
