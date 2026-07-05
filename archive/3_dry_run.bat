@echo off
cd /d "%~dp0"
echo ============================================================
echo   STEP 3: DRY RUN - the bot decides but places NOTHING
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% run.py once
echo.
echo Lines marked [DRY] are what the bot WOULD do. Nothing was sent.
echo A record was saved to logs\trades.csv. When you're happy, Step 4.
echo.
pause
