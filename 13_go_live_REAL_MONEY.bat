@echo off
cd /d "%~dp0"
echo ============================================================
echo   *** REAL MONEY MODE - THIS TRADES YOUR ACTUAL FUNDS ***
echo ============================================================
echo.
echo This runs the WEATHER EDGE strategy live, with hard caps from
echo config_live.yaml (max $2/bet, $15 open, $3 daily loss halt).
echo.
echo Requirements (or it will safely refuse to start):
echo   1. A LIVE Kalshi API key, saved here as  kalshi-live.key
echo   2. Your live Key ID pasted into  config_live.yaml
echo.
set /p CONF="Type  LIVE  (all caps) to start real-money trading: "
if not "%CONF%"=="LIVE" (
  echo.
  echo Cancelled. No real-money trading started.
  pause
  exit /b
)
echo.
echo Starting REAL-MONEY weather trading. Press Ctrl and C to stop.
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% weather_live.py --yes-live
echo.
echo Stopped. Bets saved in logs\weather_live_bets.csv
pause
