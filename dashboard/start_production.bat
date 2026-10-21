@echo off
title Trading Dashboard - Production
echo.
echo ============================================================
echo   Trading Dashboard + Cloudflare Tunnel
echo ============================================================
echo.

REM Start Flask via Waitress in background
echo [1/2] Starting Flask server...
start "Flask-Waitress" cmd /k "cd /d %~dp0 && python serve.py"

REM Wait a moment for Flask to bind
timeout /t 3 /nobreak >nul

REM Start Cloudflare Tunnel
echo [2/2] Starting Cloudflare Tunnel...
start "Cloudflare-Tunnel" cmd /k "cloudflared tunnel run trading-dashboard"

echo.
echo Both services started. Close their windows to stop.
echo Dashboard URL: https://dashboard.<YOUR-DOMAIN>.com
echo.
pause
