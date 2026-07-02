@echo off
cd /d "%~dp0"
echo ============================================================
echo   STEP 4: TRADE ON DEMO - real orders, but FAKE money
echo ============================================================
echo.
echo This places actual orders in the Kalshi DEMO sandbox.
echo It is fake money, so it is safe. The bot re-checks every minute.
echo.
echo To STOP it: click this window, then press Ctrl and C together.
echo.
echo Starting the bot now. This window will stay open while it runs.
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% run.py run --execute
echo.
echo Bot stopped. Your activity is saved in logs\trades.csv
pause
