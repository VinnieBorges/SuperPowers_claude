@echo off
title EiBeleza Studio - Auto Cut
cd /d "%~dp0"

echo ============================================
echo   EiBeleza Studio - Auto Cut
echo ============================================
echo [Check] Verificando a instalacao...

REM Torna instalacoes via winget visiveis nesta janela.
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%LOCALAPPDATA%\Programs\Ollama;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"

REM ----------------------------------------------------------------
REM 1) Python - instala automaticamente se faltar
REM ----------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [Setup] Python nao encontrado - instalando automaticamente...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
)
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERRO] Python foi instalado mas ainda nao esta visivel nesta janela.
    echo        FECHE esta janela e rode run.bat de novo - ele continua sozinho.
    pause
    exit /b 1
)

REM ----------------------------------------------------------------
REM 2) FFmpeg - instala automaticamente se faltar
REM ----------------------------------------------------------------
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [Setup] FFmpeg nao encontrado - instalando automaticamente...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)
where ffmpeg >nul 2>nul
if errorlevel 1 echo [AVISO] FFmpeg pode precisar de uma janela nova; o app tambem se ajusta sozinho ao iniciar.

REM ----------------------------------------------------------------
REM 3) Ollama - instala automaticamente se faltar
REM ----------------------------------------------------------------
where ollama >nul 2>nul
if errorlevel 1 (
    echo [Setup] Ollama nao encontrado - instalando automaticamente...
    winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
)

REM ----------------------------------------------------------------
REM 4) Ambiente Python + dependencias
REM ----------------------------------------------------------------
if not exist ".venv" (
    echo [Setup] Criando ambiente Python...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

REM Bibliotecas de GPU para a transcricao quando ha placa NVIDIA.
where nvidia-smi >nul 2>nul
if not errorlevel 1 (
    pip install -q nvidia-cublas-cu12 nvidia-cudnn-cu12
)

REM Cria o .env na primeira execucao.
if not exist ".env" if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo [Setup] Arquivo .env criado - opcional: adicione sua ANTHROPIC_API_KEY nele.
)

REM ----------------------------------------------------------------
REM 5) Modo de teste:  run.bat test
REM ----------------------------------------------------------------
if /i "%~1"=="test" (
    echo [Test] Rodando a suite de auto-teste...
    python tests\run_tests.py
    pause
    exit /b %errorlevel%
)

REM ----------------------------------------------------------------
REM 6) Ollama ativo + modelo gemma4:12b baixado
REM ----------------------------------------------------------------
where ollama >nul 2>nul
if not errorlevel 1 (
    start "" /b ollama serve >nul 2>nul
    timeout /t 3 /nobreak >nul
    ollama list 2>nul | findstr /i /c:"gemma4:12b" >nul
    if errorlevel 1 (
        echo [Setup] Baixando o modelo local de IA gemma4:12b ^(~8 GB, so na 1a vez^)...
        ollama pull gemma4:12b
    )
) else (
    echo [AVISO] Ollama ainda nao visivel nesta janela - feche e rode run.bat de novo.
)

REM ----------------------------------------------------------------
REM 7) Modelo de transcricao Whisper ja baixado?
REM ----------------------------------------------------------------
if not exist "%USERPROFILE%\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3" (
    echo [Setup] Baixando o modelo de transcricao Whisper large-v3 ^(~3 GB, so na 1a vez^)...
    python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8'); print('[Setup] Whisper OK')"
)

REM ----------------------------------------------------------------
REM 8) Iniciar
REM ----------------------------------------------------------------
echo [Run] Iniciando o EiBeleza Studio em http://127.0.0.1:8000
start "" http://127.0.0.1:8000
python main.py

pause
