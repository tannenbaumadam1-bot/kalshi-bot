@echo off
cd /d "%~dp0"
echo ============================================================
echo   Pushing your Kalshi Bot updates to the server
echo ============================================================
echo.
git add -A
git diff --cached --name-only | findstr /I /C:".key" >nul && (
  echo SAFETY STOP: a .key file was about to be uploaded. Nothing pushed.
  pause & exit /b 1
)
git diff --cached --quiet || git commit -m "manual update"
git push origin main
if %errorlevel%==0 (
  echo.
  echo SUCCESS - the server will pick this up within ~3 minutes.
) else (
  echo.
  echo PUSH FAILED - check your internet connection and try again.
)
pause
