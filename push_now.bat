@echo off
cd /d "%~dp0"
echo Pushing latest commits to GitHub...
git push origin main
echo.
echo Done. If it says "Everything up-to-date" or lists commits, you are pushed.
pause
