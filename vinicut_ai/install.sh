#!/bin/bash
# EiBeleza Studio - Auto Cut : INSTALADOR COMPLETO para macOS (Apple Silicon, M1+)
# Verifica cada componente e baixa/instala automaticamente o que faltar:
#   1. Homebrew            4. Dependencias Python
#   2. Python / FFmpeg     5. Modelo local de IA (Ollama + gemma4:12b)
#   3. Ollama              6. Modelo de transcricao Whisper
# Rode UMA vez:  chmod +x install.sh && ./install.sh
# Dia a dia:     ./run.sh
set -u
cd "$(dirname "$0")"

echo "===================================================="
echo "  EiBeleza Studio - Auto Cut  :  INSTALADOR (macOS)"
echo "===================================================="

# ----------------------------------------------------------------
# 0) Plataforma: apenas Apple Silicon (M1 ou superior)
# ----------------------------------------------------------------
if [ "$(uname -s)" != "Darwin" ]; then
    echo "[ERRO] Este instalador e para macOS. No Windows use o install.bat."
    exit 1
fi
if [ "$(uname -m)" != "arm64" ]; then
    echo "[ERRO] Suporte apenas para Macs com chip Apple Silicon (M1 ou superior)."
    exit 1
fi

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

STATUS_BREW="FALTA"; STATUS_PYTHON="FALTA"; STATUS_FFMPEG="FALTA"
STATUS_OLLAMA="FALTA"; STATUS_DEPS="FALTA"; STATUS_GEMMA="FALTA"; STATUS_WHISPER="FALTA"

# ----------------------------------------------------------------
# Espaco em disco: os modelos somam ~11 GB
# ----------------------------------------------------------------
FREE_GB=$(df -g / | awk 'NR==2 {print $4}')
if [ -n "${FREE_GB:-}" ] && [ "$FREE_GB" -lt 15 ]; then
    echo "[AVISO] Apenas ${FREE_GB} GB livres no disco. Os modelos de IA precisam de ~11 GB."
    echo "        Libere espaco antes de continuar, ou os downloads podem falhar."
fi

# ----------------------------------------------------------------
# 1) Homebrew
# ----------------------------------------------------------------
if command -v brew >/dev/null 2>&1; then
    echo "[1/6] Homebrew ja instalado."
    STATUS_BREW="OK"
else
    echo "[1/6] Homebrew nao encontrado."
    read -r -p "      Instalar o Homebrew agora? (pede a senha do Mac) [S/N] " RESP
    case "$RESP" in
        [Ss]*)
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
            if command -v brew >/dev/null 2>&1; then STATUS_BREW="OK"; fi
            ;;
        *)
            echo "      Sem o Homebrew nao consigo instalar o resto. Instale e rode ./install.sh de novo."
            exit 1
            ;;
    esac
fi

# ----------------------------------------------------------------
# 2) Python 3 e FFmpeg
# ----------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    echo "[2/6] Python ja instalado ($(python3 --version 2>&1))."
else
    echo "[2/6] Instalando Python..."
    brew install python@3.12
fi
command -v python3 >/dev/null 2>&1 && STATUS_PYTHON="OK"

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    echo "      FFmpeg ja instalado."
else
    echo "      Instalando FFmpeg..."
    brew install ffmpeg
fi
command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 && STATUS_FFMPEG="OK"

# ----------------------------------------------------------------
# 3) Ollama (IA local - usa a GPU do M-series via Metal)
# ----------------------------------------------------------------
if command -v ollama >/dev/null 2>&1; then
    echo "[3/6] Ollama ja instalado."
else
    echo "[3/6] Instalando Ollama..."
    brew install ollama
fi
command -v ollama >/dev/null 2>&1 && STATUS_OLLAMA="OK"

# ----------------------------------------------------------------
# 4) Ambiente Python + dependencias
# ----------------------------------------------------------------
echo "[4/6] Instalando dependencias Python..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python3 -m pip install -q --upgrade pip
if pip install -q -r requirements.txt; then
    STATUS_DEPS="OK"
else
    echo "      [AVISO] Falha ao instalar dependencias - verifique a internet e rode de novo."
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "      Arquivo .env criado - opcional: adicione sua ANTHROPIC_API_KEY (Claude)."
fi

# ----------------------------------------------------------------
# 5) Modelo local de IA (gemma4:12b via Ollama)
# ----------------------------------------------------------------
if [ "$STATUS_OLLAMA" = "OK" ]; then
    if ! pgrep -x ollama >/dev/null 2>&1; then
        echo "[5/6] Iniciando o Ollama em segundo plano..."
        (ollama serve >/dev/null 2>&1 &)
        sleep 3
    fi
    if ollama list 2>/dev/null | grep -qi "gemma4:12b"; then
        echo "[5/6] Modelo gemma4:12b ja baixado."
        STATUS_GEMMA="OK"
    else
        echo "[5/6] Baixando o modelo local de IA gemma4:12b (~8 GB, pode demorar)..."
        if ollama pull gemma4:12b; then
            STATUS_GEMMA="OK"
        else
            echo "      [AVISO] Download falhou. Rode depois:  ollama pull gemma4:12b"
        fi
    fi
else
    echo "[5/6] Pulado - Ollama nao disponivel."
fi

# ----------------------------------------------------------------
# 6) Modelo de transcricao Whisper large-v3
# ----------------------------------------------------------------
WHISPER_CACHE="$HOME/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3"
if [ -d "$WHISPER_CACHE" ]; then
    echo "[6/6] Modelo Whisper ja baixado."
    STATUS_WHISPER="OK"
else
    echo "[6/6] Baixando o modelo de transcricao Whisper large-v3 (~3 GB)..."
    if python3 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8')"; then
        STATUS_WHISPER="OK"
    else
        echo "      [AVISO] Sera baixado automaticamente no primeiro video."
    fi
fi

# ----------------------------------------------------------------
# Resumo
# ----------------------------------------------------------------
echo ""
echo "===================================================="
echo "  RESUMO DA INSTALACAO"
echo "===================================================="
echo "  Homebrew ............ $STATUS_BREW"
echo "  Python .............. $STATUS_PYTHON"
echo "  FFmpeg .............. $STATUS_FFMPEG"
echo "  Ollama .............. $STATUS_OLLAMA"
echo "  Dependencias ........ $STATUS_DEPS"
echo "  Modelo IA (gemma) ... $STATUS_GEMMA"
echo "  Modelo Whisper ...... $STATUS_WHISPER"
echo "===================================================="
if [ "$STATUS_PYTHON" = "OK" ] && [ "$STATUS_FFMPEG" = "OK" ] && [ "$STATUS_OLLAMA" = "OK" ] && \
   [ "$STATUS_DEPS" = "OK" ] && [ "$STATUS_GEMMA" = "OK" ]; then
    echo "  Tudo pronto!"
else
    echo "  Alguns itens falharam - rode ./install.sh de novo (ele continua de onde parou)."
fi
echo "  - Para usar o Claude: edite o .env e cole sua ANTHROPIC_API_KEY"
echo "  - Dia a dia: ./run.sh        - Testes: ./run.sh test"
echo ""
read -r -p "Iniciar o EiBeleza Studio agora? [S/N] " RESP
case "$RESP" in
    [Ss]*) exec ./run.sh ;;
    *) exit 0 ;;
esac
