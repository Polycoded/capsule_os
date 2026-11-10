@echo off
setlocal

if "%CAPSULEOS_PORT%"=="" set CAPSULEOS_PORT=8768

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0demo_capsuleos.ps1"

endlocal
