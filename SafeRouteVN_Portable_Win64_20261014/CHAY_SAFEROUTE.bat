@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0portable\python-backend\python.exe" goto portable
if exist "%~dp0portable\manifest.json" (
  echo Portable package is incomplete. Extract the entire ZIP again.
  pause
  exit /b 1
)
set "SAFEROUTE_PYTHON=py -3.12"
py -3.12 -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 set "SAFEROUTE_PYTHON=python"
%SAFEROUTE_PYTHON% -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 (
  echo Python 3.12 is required. Install it or add it to PATH.
  pause
  exit /b 1
)
%SAFEROUTE_PYTHON% -B "%~dp0backend\scripts\launch_local.py" %*
if errorlevel 1 (
  echo.
  echo SafeRoute could not complete the command. Read the error above.
  pause
  exit /b 1
)
endlocal
exit /b 0

:portable
"%~dp0portable\python-backend\python.exe" -X utf8 -B "%~dp0backend\scripts\portable_bootstrap.py" %*
if errorlevel 1 (
  echo.
  echo SafeRoute could not start. Read the error above or portable-state logs.
  pause
  exit /b 1
)
endlocal
