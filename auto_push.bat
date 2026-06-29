@echo off
cd /d "%~dp0"
git add -A
git diff --cached --name-only | findstr /I /C:".key" >nul && exit /b 1
git diff --cached --quiet && exit /b 0
git commit -m "auto update"
git push origin main
exit /b 0
