@echo off
title EiBeleza Studio - Auto Cut
cd /d "%~dp0"

echo ============================================
echo   EiBeleza Studio - Auto Cut
echo ============================================

REM ---------------------------------------------------------------
REM 1. Verify Python
REM ---------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install Python 3.10+ from python.org first.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------
REM 2. First-run bootstrap: create .env from the template
REM ---------------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo [Setup] Created .env from .env.example.
        echo [Setup] Edit .env to add your ANTHROPIC_API_KEY and enable Claude.
    )
)

REM ---------------------------------------------------------------
REM 3. Virtual environment + dependencies
REM ---------------------------------------------------------------
if not exist ".venv" (
    echo [Setup] Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [Setup] Installing dependencies...
pip install -q -r requirements.txt

REM ---------------------------------------------------------------
REM 4. Verify FFmpeg
REM ---------------------------------------------------------------
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo [WARNING] FFmpeg was not found on PATH. Rendering will fail without it.
    echo           Install it with:  winget install Gyan.FFmpeg
    echo           Then close this window and run run.bat again.
    echo.
)

REM ---------------------------------------------------------------
REM 5. Optional self-test mode:  run.bat test
REM ---------------------------------------------------------------
if /i "%~1"=="test" (
    echo [Test] Running the self-test suite...
    python tests\run_tests.py
    pause
    exit /b %errorlevel%
)

REM ---------------------------------------------------------------
REM 6. Boot Ollama in the background if it is installed (local LLM)
REM ---------------------------------------------------------------
where ollama >nul 2>nul
if not errorlevel 1 (
    echo [Setup] Starting Ollama in the background...
    start "" /b ollama serve >nul 2>nul
)

REM ---------------------------------------------------------------
REM 7. Launch
REM ---------------------------------------------------------------
echo [Run] Starting EiBeleza Studio at http://127.0.0.1:8000
start "" http://127.0.0.1:8000
python main.py

pause
