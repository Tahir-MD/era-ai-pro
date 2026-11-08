@echo off
REM Double-click to build ERA.exe  (needs Python 3.11+ installed)
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
python -m playwright install chromium
python build_exe.py
pause
