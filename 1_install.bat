@echo off
cd /d "%~dp0"
echo ============================================================
echo   STEP 1: Installing the bot's requirements (one time only)
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% -m pip install -r requirements.txt
echo.
echo If you saw no red errors above, you're done with Step 1.
echo You can close this window.
echo.
pause
