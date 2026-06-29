@echo off
cd /d "%~dp0"
echo ============================================================
echo   STEP 2: Checking your connection to Kalshi (demo)
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% run.py check
echo.
echo If you see your balance above, your key works! Move to Step 3.
echo If you see a "Config problem" or "Auth failed" message, open
echo config.yaml and double-check the two things you pasted in.
echo.
pause
