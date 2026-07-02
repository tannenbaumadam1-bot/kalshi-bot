@echo off
cd /d "%~dp0"
echo ============================================================
echo   ONE-TIME GitHub setup - connects this folder to your repo
echo ============================================================
echo.
where git >nul 2>nul || (echo Git is not installed. Install "Git for Windows" first, then re-run this. & pause & exit /b)
git init
git config user.name "tannenbaumadam1-bot"
git config user.email "tannenbaumadam1-bot@users.noreply.github.com"
git branch -M main
git remote remove origin >nul 2>nul
git remote add origin https://github.com/tannenbaumadam1-bot/kalshi-bot.git
git add -A
echo.
echo Safety check: making sure no secret key files are being uploaded...
git diff --cached --name-only | findstr /I /C:".key" >nul && (echo.& echo STOP - a .key file was about to upload. Aborting. Tell Claude. & pause & exit /b)
echo   OK - no key files staged.
git commit -m "Kalshi bot - initial commit"
echo.
echo   ** A GitHub sign-in window may pop up - sign in to allow the upload. **
echo.
git push -u origin main
echo.
echo Enabling automatic upload every 3 minutes...
schtasks /Create /SC MINUTE /MO 3 /TN "KalshiBotAutoPush" /TR "\"%~dp0auto_push.bat\"" /F
echo.
echo ============================================================
echo   DONE. Changes you make here will now upload automatically.
echo ============================================================
pause
