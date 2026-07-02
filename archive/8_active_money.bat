@echo off
cd /d "%~dp0"
echo ============================================================
echo   ACTIVE + MONEY: trades often, but only edge-positive trades
echo ============================================================
echo.
echo This is the everyday money profile (config_balanced.yaml).
echo It trades more than 4, but every trade must still beat fees -
echo no junk trades. Demo sandbox = fake money, safe to run.
echo.
echo Watch for: SENT, enter yes, momentum entry, take-profit, ARB.
echo.
echo To STOP it: click this window, then press Ctrl and C together.
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% run.py run --execute --config=config_balanced.yaml
echo.
echo Bot stopped. Your activity is saved in logs\trades.csv
pause
