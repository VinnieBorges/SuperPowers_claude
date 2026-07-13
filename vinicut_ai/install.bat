@echo off
setlocal
title EiBeleza Studio - Instalador
cd /d "%~dp0"

echo ====================================================
echo   EiBeleza Studio - Auto Cut  :  INSTALADOR COMPLETO
echo ====================================================
echo Este instalador configura tudo automaticamente:
echo   1. Python           4. Dependencias Python
echo   2. FFmpeg           5. Modelo local de IA (gemma4:12b)
echo   3. Ollama           6. Modelo de transcricao Whisper
echo.
echo Rode este arquivo UMA vez. Depois use o run.bat no dia a dia.
echo.
pause

REM ----------------------------------------------------------------
REM winget disponivel?
REM ----------------------------------------------------------------
where winget >nul 2>nul
if errorlevel 1 (
    echo [AVISO] winget nao encontrado neste Windows.
    echo         Instale manualmente e rode install.bat de novo:
    echo           Python:  https://python.org/downloads  ^(marque "Add to PATH"^)
    echo           FFmpeg:  winget ou https://www.gyan.dev/ffmpeg/builds/
    echo           Ollama:  https://ollama.com/download
    echo.
)

REM ----------------------------------------------------------------
REM [1/6] Python
REM ----------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [1/6] Instalando Python 3.12...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
) else (
    echo [1/6] Python ja instalado.
)

REM Torna as instalacoes recem-feitas visiveis NESTA janela.
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%LOCALAPPDATA%\Programs\Ollama;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERRO] Python instalado mas ainda nao visivel nesta janela.
    echo        FECHE esta janela e rode install.bat NOVAMENTE para continuar.
    pause
    exit /b 1
)

REM ----------------------------------------------------------------
REM [2/6] FFmpeg
REM ----------------------------------------------------------------
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [2/6] Instalando FFmpeg...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
) else (
    echo [2/6] FFmpeg ja instalado.
)
where ffmpeg >nul 2>nul
if errorlevel 1 echo        [AVISO] FFmpeg pode precisar de uma janela nova para aparecer. O app tambem resolve isso sozinho ao iniciar.

REM ----------------------------------------------------------------
REM [3/6] Ollama (IA local)
REM ----------------------------------------------------------------
where ollama >nul 2>nul
if errorlevel 1 (
    echo [3/6] Instalando Ollama...
    winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
) else (
    echo [3/6] Ollama ja instalado.
)

REM ----------------------------------------------------------------
REM [4/6] Ambiente Python + dependencias
REM ----------------------------------------------------------------
echo [4/6] Instalando dependencias Python...
if not exist ".venv" python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt
if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias. Verifique sua internet e rode de novo.
    pause
    exit /b 1
)

REM Aceleracao por GPU NVIDIA para a transcricao (so quando ha placa NVIDIA).
where nvidia-smi >nul 2>nul
if not errorlevel 1 (
    echo        GPU NVIDIA detectada - instalando aceleracao de transcricao...
    pip install -q nvidia-cublas-cu12 nvidia-cudnn-cu12
)

REM Cria o .env na primeira instalacao (cole sua ANTHROPIC_API_KEY nele).
if not exist ".env" if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo        Arquivo .env criado - edite-o para adicionar sua ANTHROPIC_API_KEY ^(Claude^).
)

REM ----------------------------------------------------------------
REM [5/6] Modelo local de IA
REM ----------------------------------------------------------------
echo [5/6] Baixando o modelo local de IA gemma4:12b ^(pode demorar, ~8 GB^)...
where ollama >nul 2>nul
if errorlevel 1 (
    echo        [AVISO] Ollama ainda nao visivel nesta janela.
    echo        Depois de fechar, rode:  ollama pull gemma4:12b
) else (
    start "" /b ollama serve >nul 2>nul
    timeout /t 5 /nobreak >nul
    ollama pull gemma4:12b
    if errorlevel 1 (
        echo        [AVISO] Nao consegui baixar agora. Rode depois:  ollama pull gemma4:12b
    )
    REM Opcional - analise visual de quadros (nao usada no fluxo principal):
    REM ollama pull llama3.2-vision:latest
)

REM ----------------------------------------------------------------
REM [6/6] Modelo de transcricao Whisper
REM ----------------------------------------------------------------
echo [6/6] Baixando o modelo de transcricao Whisper large-v3 ^(~3 GB, so na 1a vez^)...
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8'); print('        Whisper OK')"
if errorlevel 1 echo        [AVISO] O Whisper sera baixado automaticamente no primeiro video.

echo.
echo ====================================================
echo   INSTALACAO CONCLUIDA!
echo ====================================================
echo   - Para usar o Claude: edite o .env e cole sua ANTHROPIC_API_KEY
echo   - Dia a dia: run.bat        - Testes: run.bat test
echo.
choice /C SN /M "Iniciar o EiBeleza Studio agora? [S/N]"
if errorlevel 2 exit /b 0
call run.bat
