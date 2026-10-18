@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\issue-driver.ps1"
set "rc=%errorlevel%"
pause
exit /b %rc%
