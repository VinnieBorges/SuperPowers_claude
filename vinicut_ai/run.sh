#!/bin/bash
# EiBeleza Studio - Auto Cut : launcher para macOS (Apple Silicon, M1 ou superior)
# Uso:  ./run.sh          inicia o app (instala o que faltar automaticamente)
#       ./run.sh test     roda a suite de auto-teste
set -u
cd "$(dirname "$0")"

echo "============================================"
echo "  EiBeleza Studio - Auto Cut (macOS)"
echo "============================================"

# ----------------------------------------------------------------
# 0) Plataforma suportada: apenas Apple Silicon (M1+)
# ----------------------------------------------------------------
if [ "$(uname -s)" != "Darwin" ]; then
    echo "[ERRO] Este script e para macOS. No Windows use o run.bat."
    exit 1
fi
if [ "$(uname -m)" != "arm64" ]; then
    echo "[ERRO] Suporte apenas para Macs com chip Apple Silicon (M1 ou superior)."
    echo "       Macs Intel nao sao suportados."
    exit 1
fi

# Homebrew do Apple Silicon
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

if ! command -v brew >/dev/null 2>&1; then
    echo "[ERRO] Homebrew nao encontrado. Instale primeiro com:"
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    echo "Depois rode ./run.sh novamente."
    exit 1
fi

echo "[Check] Verificando a instalacao..."

# ----------------------------------------------------------------
# 1) Python 3
# ----------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "[Setup] Python nao encontrado - instalando via Homebrew..."
    brew install python@3.12
fi

# ----------------------------------------------------------------
# 2) FFmpeg
# ----------------------------------------------------------------
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[Setup] FFmpeg nao encontrado - instalando via Homebrew..."
    brew install ffmpeg
fi

# ----------------------------------------------------------------
# 3) Ollama (IA local - usa a GPU do M-series via Metal)
# ----------------------------------------------------------------
if ! command -v ollama >/dev/null 2>&1; then
    echo "[Setup] Ollama nao encontrado - instalando via Homebrew..."
    brew install ollama
fi

# ----------------------------------------------------------------
# 4) Ambiente Python + dependencias
# ----------------------------------------------------------------
if [ ! -d ".venv" ]; then
    echo "[Setup] Criando ambiente Python..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

# Cria o .env na primeira execucao.
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[Setup] Arquivo .env criado - opcional: adicione sua ANTHROPIC_API_KEY nele."
fi

# ----------------------------------------------------------------
# 5) Modo de teste:  ./run.sh test
# ----------------------------------------------------------------
if [ "${1:-}" = "test" ]; then
    echo "[Test] Rodando a suite de auto-teste..."
    python3 tests/run_tests.py
    exit $?
fi

# ----------------------------------------------------------------
# 6) Ollama ativo + modelo gemma4:12b baixado
# ----------------------------------------------------------------
if ! pgrep -x ollama >/dev/null 2>&1; then
    echo "[Setup] Iniciando o Ollama em segundo plano..."
    (ollama serve >/dev/null 2>&1 &)
    sleep 3
fi
if ! ollama list 2>/dev/null | grep -qi "gemma4:12b"; then
    echo "[Setup] Baixando o modelo local de IA gemma4:12b (~8 GB, so na 1a vez)..."
    ollama pull gemma4:12b || echo "[AVISO] Nao consegui baixar agora. Rode depois: ollama pull gemma4:12b"
fi

# ----------------------------------------------------------------
# 7) Modelo de transcricao Whisper ja baixado?
# ----------------------------------------------------------------
WHISPER_CACHE="$HOME/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3"
if [ ! -d "$WHISPER_CACHE" ]; then
    echo "[Setup] Baixando o modelo de transcricao Whisper large-v3 (~3 GB, so na 1a vez)..."
    python3 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8'); print('[Setup] Whisper OK')" \
        || echo "[AVISO] O Whisper sera baixado automaticamente no primeiro video."
fi

# ----------------------------------------------------------------
# 8) Iniciar
# ----------------------------------------------------------------
echo "[Run] Iniciando o EiBeleza Studio em http://127.0.0.1:8000"
( sleep 2 && open "http://127.0.0.1:8000" ) &
python3 main.py
