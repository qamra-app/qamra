@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install flask requests openpyxl reportlab pyopenssl -q
echo.
echo Starting Qamra Kiosk...
start "" "http://localhost:5000"
python server.py
pause
