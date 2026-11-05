@echo off
cd /d "%~dp0"
echo Dang khoi dong Reels AI Batch Studio tren cong http://localhost:8050 ...
start "" "http://localhost:8050"
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8050
pause

