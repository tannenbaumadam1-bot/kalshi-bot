@echo off
echo Turning OFF the every-3-minutes auto-upload...
schtasks /Delete /TN "KalshiBotAutoPush" /F >nul 2>nul
echo Done. Nothing uploads automatically anymore.
echo From now on, double-click  2_push_updates.bat  whenever you want
echo to send updates to the server.
pause
