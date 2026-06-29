@echo off
cd /d "%~dp0"
echo ============================================================
echo   RESET the paper bot to a fresh $100 start
echo ============================================================
echo.
echo This clears the saved portfolio and all history so the next
echo run starts clean. (Only affects the paper test - nothing real.)
echo.
del /q logs\paper_sim.json   2>nul
del /q logs\paper_pnl.csv     2>nul
del /q logs\paper_trades.csv  2>nul
del /q logs\paper_state.json  2>nul
del /q logs\paper.lock        2>nul
echo Done - portfolio and history cleared.
echo Now double-click 9_paper_live.bat for a fresh $100 run.
echo.
pause
