@echo off
title Plybit AI Server
cd /d "e:\OTC live trading"

echo  ================================
echo   Plybit AI -- Starting...
echo  ================================
echo.
echo  Browser: http://localhost:8000
echo  Press Ctrl+C to stop.
echo.

.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000 --reload

pause
