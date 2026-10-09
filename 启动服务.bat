@echo off
setlocal enabledelayedexpansion
title audit-companion launcher

rem ============================================================
rem  audit-companion one-click launcher (app + Cloudflare tunnel)
rem  - safe to re-run: healthy services are detected and skipped
rem  - paths derived from this file's location, no hardcoded path
rem  - ASCII-only on purpose: works on any Windows code page
rem ============================================================

set "SERVER_DIR=%~dp0server"
set "CF_EXE=C:\Program Files (x86)\cloudflared\cloudflared.exe"
set "TOKEN_FILE=%USERPROFILE%\AppData\Local\Temp\cf_tunnel_token.txt"
set "APP_PORT=8000"
set "APP_URL=http://127.0.0.1:%APP_PORT%"
set "PUBLIC_URL=https://audit-companion.xyz"

echo.
echo  [1/5] Checking environment...
if not exist "%SERVER_DIR%\.venv\Scripts\python.exe" goto err_venv
if not exist "%CF_EXE%" goto err_cf
if not exist "%TOKEN_FILE%" goto err_token

echo  [2/5] Local app on port %APP_PORT%...
call :http_code "%APP_URL%/health" 3
if "!CODE!"=="200" (
    echo  [SKIP] app already healthy.
    goto tunnel
)
netstat -ano | findstr /C:":%APP_PORT% " | findstr LISTENING >nul 2>&1
if not errorlevel 1 goto err_port
echo  [START] uvicorn window on 0.0.0.0:%APP_PORT%...
start "audit-companion app" cmd /k "cd /d "%SERVER_DIR%" && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port %APP_PORT%"
echo  [WAIT]  up to 20s for boot...
rem ping-based sleep: immune to GNU timeout shadowing in Git Bash PATH
set /a TRIES=0
:wait_app
ping -n 3 127.0.0.1 >nul
call :http_code "%APP_URL%/health" 3
set /a TRIES+=1
if not "!CODE!"=="200" if !TRIES! lss 10 goto wait_app
if not "!CODE!"=="200" goto err_health
echo  [OK] app healthy.

:tunnel
echo  [3/5] Cloudflare tunnel connector...
tasklist /FI "IMAGENAME eq cloudflared.exe" 2>nul | find /I "cloudflared.exe" >nul 2>&1
if not errorlevel 1 (
    echo  [SKIP] cloudflared already running.
    goto verify
)
set /p CF_TOKEN=<"%TOKEN_FILE%"
if not defined CF_TOKEN goto err_token
echo  [START] cloudflared window...
start "audit-companion tunnel" "%CF_EXE%" tunnel --no-autoupdate run --token %CF_TOKEN%
echo  [WAIT]  8s for connect...
ping -n 9 127.0.0.1 >nul

:verify
echo  [4/5] Verifying endpoints...
call :http_code "%APP_URL%/health" 5
echo   local  health: HTTP !CODE!
call :http_code "%PUBLIC_URL%/health" 20
echo   public health: HTTP !CODE!
if not "!CODE!"=="200" echo   [NOTE] public not ready yet - rerun this script in a minute to re-check.

echo  [5/5] Done.
echo.
echo   Public URL : %PUBLIC_URL%
echo   Local URL  : %APP_URL%
echo   Stop       : close the "audit-companion app" / "audit-companion tunnel" windows
echo.
pause >nul
exit /b 0

:http_code
set "CODE=000"
for /f %%i in ('curl -s -o nul -w "%%{http_code}" --max-time %~2 "%~1" 2^>nul') do set "CODE=%%i"
exit /b 0

:err_venv
echo  [ERROR] venv python not found:
echo          %SERVER_DIR%\.venv\Scripts\python.exe
echo          Create it first:  cd server ^&^& python -m venv .venv
goto fail
:err_cf
echo  [ERROR] cloudflared not found: "%CF_EXE%"
echo          Install with: winget install Cloudflare.cloudflared
goto fail
:err_token
echo  [ERROR] tunnel token file not found: %TOKEN_FILE%
echo          Re-export the token from the Cloudflare Zero Trust Tunnels page.
goto fail
:err_port
echo  [ERROR] Port %APP_PORT% is occupied but /health is not OK.
echo          Kill the stale process first:
echo          netstat -ano ^| findstr :%APP_PORT%     then     taskkill /PID ^<pid^> /F
goto fail
:err_health
echo  [ERROR] App did not become healthy in 20s. Check the app window for errors.
goto fail
:fail
echo.
pause >nul
exit /b 1
