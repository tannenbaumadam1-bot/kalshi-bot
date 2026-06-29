@echo off
cd /d "%~dp0"
echo ============================================================
echo   REPORT CARD - how is your bot doing?
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% report.py
echo.
pause
