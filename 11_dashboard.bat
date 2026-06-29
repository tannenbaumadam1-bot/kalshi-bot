@echo off
cd /d "%~dp0"
echo ============================================================
echo   LIVE DASHBOARD - opens a web page in your browser
echo ============================================================
echo.
echo This shows the paper bot's P&L, positions, and every trade,
echo refreshing every few seconds. It reads the log files only.
echo.
echo Your browser will open to http://127.0.0.1:8765
echo (If it does not, type that address into your browser.)
echo.
echo Keep this window open while you watch. Ctrl+C to stop.
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% dashboard.py
echo.
echo Dashboard stopped.
pause
