@echo off
cd /d "%~dp0"
echo ============================================================
echo   WEATHER EDGE SCAN - finds mispriced temperature markets
echo ============================================================
echo.
echo Reads live Kalshi temp markets + free weather forecasts,
echo and lists where our forecast disagrees with the market.
echo (Read-only research - places NO orders.)
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% weather_edge.py
echo.
pause
