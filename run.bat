@echo off
cd /d "%~dp0"
if not exist venv python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
start "" cmd /c "timeout /t 5 >nul & start http://127.0.0.1:5000"
echo Keep this window OPEN. Website: http://127.0.0.1:5000
python app.py
pause
