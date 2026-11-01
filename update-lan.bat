@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" goto missing_python
if /i "%~1"=="--check" goto check_only
if not "%~1"=="" goto usage
echo [1/2] Detecting physical LAN and updating this project configuration...
"%~dp0.venv\Scripts\python.exe" -m gift_registry.lan_setup
if errorlevel 1 goto setup_failed
echo [2/2] Updating this app firewall rule. Windows may request administrator permission...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0allow-lan.ps1" -Port 8765
if errorlevel 1 goto firewall_failed
echo.
echo LAN configuration and firewall updated. Restart the workbench normally if the IP changed.
pause
exit /b 0
:setup_failed
echo.
echo Network detection/configuration failed. Firewall update was NOT started.
echo Review the specific error above. For a read-only check: update-lan.bat --check
pause
exit /b 1
:firewall_failed
echo.
echo LAN configuration step completed, but firewall update failed or was cancelled.
echo Review the message above and data\firewall-update.log. Remote access may still be blocked.
pause
exit /b 1
:missing_python
echo Missing .venv\Scripts\python.exe. Run this BAT from the original project with its Python environment.
if /i "%~1"=="--check" exit /b 1
pause
exit /b 1
:check_only
"%~dp0.venv\Scripts\python.exe" -m gift_registry.lan_setup --diagnose
exit /b %errorlevel%
:usage
echo Usage: update-lan.bat [--check]
exit /b 2
