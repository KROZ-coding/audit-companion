@echo off
setlocal enabledelayedexpansion
title audit-companion docker launcher

rem ============================================================
rem  audit-companion Docker one-click launcher (app + tunnel)
rem  - app runs in Docker (compose quick profile, naming volume)
rem  - tunnel prefers host cloudflared (Cloudflare panel ingress
rem    points at localhost:8000, token mode cannot change that);
rem    set TUNNEL_MODE=container to run cloudflared in a container
rem  - safe to re-run: healthy services are detected and skipped
rem  - ASCII-only on purpose: works on any Windows code page
rem ============================================================

set "ROOT_DIR=%~dp0"
set "COMPOSE_DIR=%ROOT_DIR%server"
set "CF_EXE=C:\Program Files (x86)\cloudflared\cloudflared.exe"
set "TOKEN_FILE=%USERPROFILE%\AppData\Local\Temp\cf_tunnel_token.txt"
set "APP_PORT=8000"
set "APP_URL=http://127.0.0.1:%APP_PORT%"
set "PUBLIC_URL=https://audit-companion.xyz"
set "CONTAINER=audit-quick-app"
rem tunnel mode: host (default) = host cloudflared exe, container = compose profile
if not defined TUNNEL_MODE set "TUNNEL_MODE=host"
rem optional parallel port: set QUICK_PORT=8001 before calling (tunnel expects 8000 though)
if defined QUICK_PORT (
    set "APP_PORT=%QUICK_PORT%"
    set "APP_URL=http://127.0.0.1:%QUICK_PORT%"
)

echo.
echo  [1/6] Checking environment...
where docker >nul 2>&1 || goto err_docker
docker info >nul 2>&1 || goto err_daemon
if not exist "%COMPOSE_DIR%\docker-compose.quick.yml" goto err_compose
call :check_port
if defined BLOCKER goto err_port

echo  [2/6] Starting app container (docker compose quick)...
docker compose -f "%COMPOSE_DIR%\docker-compose.quick.yml" up -d --build 1>&2
if errorlevel 1 goto err_compose_up
echo  [WAIT]  up to 90s for container health (docker inspect is authoritative)...
set /a TRIES=0
:wait_app
for /f %%i in ('docker inspect --format "{{.State.Health.Status}}" %CONTAINER% 2^>nul') do if "%%i"=="healthy" goto app_ok
set /a TRIES+=1
if !TRIES! geq 30 goto err_health
ping -n 4 127.0.0.1 >nul
goto wait_app
:app_ok
echo  [OK] app healthy in Docker.

echo  [3/6] Checking public endpoint (skips tunnel if already live)...
call :http_code "%PUBLIC_URL%/health" 15
if "!CODE!"=="200" (
    echo  [SKIP] public URL already healthy - tunnel running elsewhere.
    goto verify
)

if /I "%TUNNEL_MODE%"=="container" goto tunnel_container

echo  [4/6] Cloudflare tunnel connector (host cloudflared)...
if not exist "%CF_EXE%" goto err_cf
tasklist /FI "IMAGENAME eq cloudflared.exe" 2>nul | find /I "cloudflared.exe" >nul 2>&1
if not errorlevel 1 (
    echo  [SKIP] cloudflared already running.
    goto verify
)
if not exist "%TOKEN_FILE%" goto err_token
set /p CF_TOKEN=<"%TOKEN_FILE%"
if not defined CF_TOKEN goto err_token
echo  [START] cloudflared window...
start "audit-companion tunnel" "%CF_EXE%" tunnel --no-autoupdate run --token %CF_TOKEN%
echo  [WAIT]  8s for connect...
ping -n 9 127.0.0.1 >nul
goto verify

:tunnel_container
echo  [4/6] Cloudflare tunnel connector (container profile tunnel)...
docker compose -f "%COMPOSE_DIR%\docker-compose.quick.yml" --profile tunnel up -d cloudflared 1>&2
if errorlevel 1 goto err_tunnel_up
echo  [WAIT]  8s for connect...
ping -n 9 127.0.0.1 >nul
goto verify

:verify
echo  [5/6] Verifying endpoints...
echo   container (docker inspect): healthy
call :http_code "%APP_URL%/health" 5
echo   local  health: HTTP !CODE!
call :http_code "%PUBLIC_URL%/health" 20
echo   public health: HTTP !CODE!
if not "!CODE!"=="200" echo   [NOTE] public not ready yet - rerun this script in a minute to re-check.

echo  [6/6] Done.
echo.
echo   Public URL : %PUBLIC_URL%
echo   Local URL  : %APP_URL%
if /I "%TUNNEL_MODE%"=="container" (
    echo   Stop app + tunnel : docker compose -f server\docker-compose.quick.yml --profile tunnel down
) else (
    echo   Stop app  : docker compose -f server\docker-compose.quick.yml down
    echo   Stop tunnel: close the "audit-companion tunnel" window
)
echo   Logs      : docker logs %CONTAINER%
echo.
pause >nul
exit /b 0

:http_code
set "CODE=000"
for /f %%i in ('curl -s -o nul -w "%%{http_code}" --max-time %~2 "%~1" 2^>nul') do set "CODE=%%i"
exit /b 0

rem --- check APP_PORT: refuse when a NON-Docker process already listens there.
rem --- (host curl hitting a stale venv instance would fake "app healthy" and mask
rem ---  a dead container; docker backend + wslrelay exempt - they front the container)
:check_port
set "BLOCKER="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%APP_PORT% .*LISTENING"') do (
    tasklist /FI "PID eq %%p" 2>nul | findstr /I /C:"com.docker.backend" /C:"wslrelay" >nul 2>&1
    if errorlevel 1 set "BLOCKER=%%p"
)
exit /b 0

:err_docker
echo  [ERROR] docker not found in PATH.
echo          Install Docker Desktop and make sure it is running.
goto fail
:err_daemon
echo  [ERROR] Docker daemon not reachable. Start Docker Desktop first,
echo          wait for the whale icon, then re-run this script.
goto fail
:err_compose
echo  [ERROR] compose file not found: %COMPOSE_DIR%\docker-compose.quick.yml
goto fail
:err_compose_up
echo  [ERROR] docker compose up failed - see output above.
goto fail
:err_cf
echo  [ERROR] cloudflared not found: "%CF_EXE%"
echo          Install with: winget install Cloudflare.cloudflared
echo          Or set TUNNEL_MODE=container to run the tunnel in Docker.
goto fail
:err_token
echo  [ERROR] tunnel token file not found: %TOKEN_FILE%
echo          Re-export the token from the Cloudflare Zero Trust Tunnels page.
goto fail
:err_tunnel_up
echo  [ERROR] container tunnel failed to start. Set CF_TUNNEL_TOKEN
echo          in the environment or server\docker-compose.quick.yml .env first.
goto fail
:err_port
echo  [ERROR] Port %APP_PORT% is held by a non-Docker process (PID %BLOCKER%).
echo          Host curl would hit that stale process and mask the container.
echo          Stop the venv instance first (close the "audit-companion app"
echo          window), or:  taskkill /PID %BLOCKER% /F
echo          To run both side by side instead:  set QUICK_PORT=8001 before rerun.
goto fail
:err_health
echo  [ERROR] Container did not become healthy in 90s. Check:
echo          docker logs %CONTAINER%
goto fail
:fail
echo.
pause >nul
exit /b 1
