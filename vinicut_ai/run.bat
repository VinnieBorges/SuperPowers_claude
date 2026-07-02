@echo off
title Vinicut AI - Auto Cutter
cd /d "%~dp0"

echo ============================================
echo   Vinicut AI - Auto Cutter Launcher
echo ============================================

REM 1. Verify Python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)

REM 2. Create/activate virtual environment
if not exist ".venv" (
    echo [Setup] Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

REM 3. Install/refresh dependencies
echo [Setup] Installing dependencies...
pip install -q -r requirements.txt

REM 4. Boot Ollama in the background only if it is installed AND no cloud key
REM    is configured (the .env loader in config.py reads ANTHROPIC_API_KEY).
where ollama >nul 2>nul
if not errorlevel 1 (
    echo [Setup] Starting Ollama in the background...
    start "" /b ollama serve >nul 2>nul
)

REM 5. Launch the server
echo [Run] Starting Vinicut AI at http://127.0.0.1:8000
python main.py

pause
