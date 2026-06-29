@echo off
cd /d "%~dp0"
echo ============================================================
echo   *** REAL MONEY MODE - THIS TRADES YOUR ACTUAL FUNDS ***
echo ============================================================
echo.
echo Only use this AFTER your paper results look good.
echo It runs the SAME strategy you tested, on real money, with
echo conservative caps set in config_live.yaml.
echo.
echo Requirements (or it will safely refuse to start):
echo   1. A LIVE Kalshi API key, saved here as  kalshi-live.key
echo   2. Your live Key ID pasted into  config_live.yaml
echo.
set /p CONF="Type  LIVE  (all caps) to start real-money trading: "
if /i not "%CONF%"=="LIVE" (
  echo.
  echo Cancelled. No real-money trading started.
  pause
  exit /b
)
echo.
echo Starting REAL-MONEY trading. Press Ctrl and C to stop.
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% run.py run --execute --i-understand-live --config=config_live.yaml
echo.
echo Stopped. Activity saved in logs\trades.csv
pause
