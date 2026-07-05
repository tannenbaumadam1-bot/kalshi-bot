@echo off
cd /d "%~dp0"
echo ============================================================
echo   PAPER TEST on LIVE markets - real prices, ZERO money
echo ============================================================
echo.
echo This reads REAL live Kalshi prices and runs your strategy
echo with SIMULATED fills and P&L. It places NO orders, uses NO
echo API key, and cannot touch money - it never logs in.
echo.
echo This is the honest test of whether the strategy makes money,
echo because the markets are real and deep (unlike demo).
echo.
echo Let it run for hours. Watch the PAPER P&L line each cycle.
echo To STOP it: click this window, then press Ctrl and C together.
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% paper.py --config=config_balanced.yaml --start=100
echo.
echo Paper test stopped. Nothing was ever placed.
pause
