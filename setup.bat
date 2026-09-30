@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" ensure_ffmpeg.py
if errorlevel 1 goto failed
".venv\Scripts\python.exe" download_models.py
if errorlevel 1 goto failed
echo Setup complete. Double-click start.bat to open FarsiSub.
pause
exit /b 0
:failed
echo Setup failed. Python 3.12 and internet access are needed. See README_FA.md.
pause
exit /b 1
