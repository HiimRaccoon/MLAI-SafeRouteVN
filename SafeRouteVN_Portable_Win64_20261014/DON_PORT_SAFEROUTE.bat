@echo off
setlocal
call "%~dp0CHAY_SAFEROUTE.bat" --stop %*
exit /b %errorlevel%
