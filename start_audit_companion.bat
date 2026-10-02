@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_audit_companion.ps1"
if errorlevel 1 pause
