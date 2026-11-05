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
echo Reading config\gifts-*.txt; using the first product name row in BanNiu for each code.
echo If BanNiu is not logged in, finish the login in the opened window; the tool waits then continues.
echo The browser may stay open for several minutes; no order or gift task is submitted.
echo.
"%PY%" -X utf8 "tools\update_gifts.py" --interactive %*
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" echo Update finished.
if "%RESULT%"=="2" echo Nothing was written. See the report above.
if "%RESULT%"=="3" echo Cancelled. Nothing was written.
if not "%RESULT%"=="0" if not "%RESULT%"=="2" if not "%RESULT%"=="3" echo Update did not complete. See the report above.
pause
exit /b %RESULT%
