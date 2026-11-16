@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run_local.ps1" start
exit /b %errorlevel%
