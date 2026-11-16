@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run_local.ps1" refresh
exit /b %errorlevel%
