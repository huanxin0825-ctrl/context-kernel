@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wake.ps1" %*
exit /b %ERRORLEVEL%
