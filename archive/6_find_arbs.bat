@echo off
cd /d "%~dp0"
echo ============================================================
echo   ARB SCANNER - looks for logical mispricings (no trades)
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% arb_scanner.py
echo.
pause
