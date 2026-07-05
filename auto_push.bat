@echo off
cd /d "%~dp0"
if not exist "autopush_hidden.flag" (
  schtasks /Create /SC MINUTE /MO 3 /TN "KalshiBotAutoPush" /TR "wscript.exe \"%~dp0auto_push_hidden.vbs\"" /F >nul 2>nul && echo done> "autopush_hidden.flag"
)
git add -A
git diff --cached --name-only | findstr /I /C:".key" >nul && exit /b 1
git diff --cached --quiet || git commit -m "auto update"
git push origin main
exit /b 0
