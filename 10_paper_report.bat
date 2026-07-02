@echo off
cd /d "%~dp0"
echo ============================================================
echo   PAPER REPORT - performance scorecard (safe anytime)
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% paper_report.py
echo.
pause
