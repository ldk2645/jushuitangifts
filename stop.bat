@echo off
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" -Port %PORT%
if errorlevel 1 pause
