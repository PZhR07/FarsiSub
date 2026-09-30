@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
echo Downloading NVIDIA CUDA libraries into the local environment. Internet required.
".venv\Scripts\python.exe" -m pip install "nvidia-cublas-cu12==12.9.2.10" "nvidia-cudnn-cu12==9.26.0.51"
if errorlevel 1 (
    echo GPU setup failed. Automatic mode can still use CPU.
    pause
    exit /b 1
)
echo GPU libraries installed. Restart FarsiSub. An up-to-date NVIDIA driver is required.
pause
