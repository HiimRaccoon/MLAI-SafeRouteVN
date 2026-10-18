@echo off
setlocal
call "%~dp0SafeRouteVN_Portable_Win64_20261014\CHAY_SAFEROUTE.bat" %*
exit /b %errorlevel%
