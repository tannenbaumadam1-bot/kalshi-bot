@echo off
cd /d "%~dp0"
echo ============================================================
echo   DEMO-ACTIVE: loose filters so the bot TRADES a lot
echo ============================================================
echo.
echo This is a PLUMBING TEST on the demo sandbox (fake money).
echo It uses config_active.yaml - your real strategy in
echo config.yaml is NOT touched.
echo.
echo Watch for: SENT lines, take-profit sells, ARB lines,
echo and "cleaned up duplicate" messages.
echo.
echo To STOP it: click this window, then press Ctrl and C together.
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% run.py run --execute --config=config_active.yaml
echo.
echo Bot stopped. Your activity is saved in logs\trades.csv
pause
