@echo off
setlocal enabledelayedexpansion
title audit-companion launcher
set "SERVER_DIR=D:\deepseek harness document\老师那边的智能体9_2\server"
set "CF_EXE=C:\Program Files (x86)\cloudflared\cloudflared.exe"
set "TOKEN_FILE=%USERPROFILE%\AppData\Local\Temp\cf_tunnel_token.txt"
echo.
echo  [1/4] Checking environment...
echo.
if not exist "%SERVER_DIR%\.venv\Scripts\python.exe" (
    echo  [ERROR] venv python not found.
    echo  Check that the project path is unchanged.
    pause
    exit /b 1
)
if not exist "%CF_EXE%" (
    echo  [ERROR] cloudflared not found.
    echo  Install with: winget install Cloudflare.cloudflared
    pause
    exit /b 1
)
if not exist "%TOKEN_FILE%" (
    echo  [ERROR] tunnel token file not found.
    echo  Get the token again from Cloudflare Tunnels page and
    echo  save it to: %TOKEN_FILE%
    pause
    exit /b 1
)
set /p CF_TOKEN=<"%TOKEN_FILE%"
echo  [2/4] Starting local app (uvicorn, port 8000)...
start "audit-companion app" cmd /k "cd /d ""%SERVER_DIR%"" && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
echo  [3/4] Starting Cloudflare tunnel connector...
start "audit-companion tunnel" "%CF_EXE%" tunnel --no-autoupdate run --token %CF_TOKEN%
echo  [4/4] Waiting for services to get ready...
timeout /t 10 /nobreak >nul
echo.
echo  ============================================
echo   Both services are up:
echo     - app window    : uvicorn on port 8000
echo     - tunnel window : cloudflared to Cloudflare
echo   Public URL : https://audit-companion.xyz
echo   Verify     : /health returns JSON
echo   Stop       : close the corresponding window
echo  ============================================
echo.
curl -s -o nul -w "local  health: HTTP %%{http_code}\n" --max-time 5 http://127.0.0.1:8000/health
curl -s -o nul -w "public health: HTTP %%{http_code}\n" --max-time 20 https://audit-companion.xyz/health
echo.
echo  Press any key to close this window (keeps both services running)...
pause >nul
