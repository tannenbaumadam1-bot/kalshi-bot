@echo off
cd /d "%~dp0"
echo ============================================================
echo   Stopping the black-screen flash (auto-push runs hidden)
echo ============================================================
echo.
schtasks /Delete /TN "KalshiBotAutoPush" /F >nul 2>nul
schtasks /Create /SC MINUTE /MO 3 /TN "KalshiBotAutoPush" /TR "wscript.exe \"%~dp0auto_push_hidden.vbs\"" /F
echo.
echo Done. The auto-upload still runs every 3 minutes, but invisibly now.
echo You can close this window.
pause
