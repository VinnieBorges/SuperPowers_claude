"""
Central configuration for Vinicut AI.

Every tunable lives here and can be overridden with an environment variable
(optionally loaded from a `.env` file next to this module), so the project is
portable across machines and deployable to a server without code edits.

Precedence: environment variable > .env file > default.
"""
import os
import sys
import logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Minimal .env loader (KEY=VALUE lines, # comments). No dependency needed."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv(os.path.join(BASE_DIR, ".env"))


def env(key, default=None):
    return os.environ.get(key, default)


def env_int(key, default):
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def env_float(key, default):
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
RAW_DIR = env("VINICUT_RAW_DIR", os.path.join(BASE_DIR, "raw"))
CUTS_DIR = env("VINICUT_CUTS_DIR", os.path.join(BASE_DIR, "cuts"))
DB_DIR = env("VINICUT_DB_DIR", os.path.join(BASE_DIR, "database"))
DB_PATH = env("VINICUT_DB_PATH", os.path.join(DB_DIR, "eibeleza.db"))
WATCH_DIR = env("VINICUT_WATCH_DIR", os.path.join(BASE_DIR, "watch"))
AUTO_CUTS_DIR = env("VINICUT_AUTO_CUTS_DIR", os.path.join(BASE_DIR, "auto_cuts"))
FONTS_DIR = env("VINICUT_FONTS_DIR", os.path.join(BASE_DIR, "fonts"))
# Library of replacement hook clips uploaded by creators (hook-swap feature).
HOOKS_DIR = env("VINICUT_HOOKS_DIR", os.path.join(BASE_DIR, "hooks"))

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
HOST = env("VINICUT_HOST", "127.0.0.1")
PORT = env_int("VINICUT_PORT", 8000)
# Comma-separated list; "*" keeps the permissive default for local use.
CORS_ORIGINS = [o.strip() for o in env("VINICUT_CORS_ORIGINS", "*").split(",") if o.strip()]

# ---------------------------------------------------------------------------
# Media / render
# ---------------------------------------------------------------------------
FFMPEG_BIN = env("VINICUT_FFMPEG", env("EIBELEZA_FFMPEG", "ffmpeg"))
FFPROBE_BIN = env("VINICUT_FFPROBE", env("EIBELEZA_FFPROBE", "ffprobe"))
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
FONT_EXTENSIONS = {".ttf", ".otf"}
OUTPUT_WIDTH = env_int("VINICUT_OUTPUT_WIDTH", 1080)
OUTPUT_HEIGHT = env_int("VINICUT_OUTPUT_HEIGHT", 1920)
STANDARD_CUT_DURATIONS = (5, 15, 30, 60)

# Days a completed project's raw upload is kept before the janitor removes it.
RAW_RETENTION_DAYS = env_int("VINICUT_RAW_RETENTION_DAYS", 7)
CLEANUP_INTERVAL_SECONDS = env_int("VINICUT_CLEANUP_INTERVAL", 3600)

# Concurrent project workers. 1 is the safe default; 2-3 works well on GPUs
# with spare NVENC sessions (consumer NVIDIA cards allow 3-5 concurrent).
WORKERS = max(1, min(4, env_int("VINICUT_WORKERS", 1)))

# ---------------------------------------------------------------------------
# AI providers
# ---------------------------------------------------------------------------
# llm provider: "ollama" (local Gemma — the primary engine), "anthropic"
# (Claude API), "auto" (Claude when ANTHROPIC_API_KEY is set, else Ollama),
# or "openai" (any OpenAI-compatible endpoint). The DB `system_settings`
# table can override this at runtime from the UI; these are the boot defaults.
LLM_PROVIDER_DEFAULT = env("VINICUT_LLM_PROVIDER", "ollama")
OLLAMA_HOST = env("OLLAMA_HOST", "http://localhost:11434")
# 12B is the sweet spot for this pipeline: big models (26B+) routinely blow
# past HTTP timeouts on consumer GPUs while loading/offloading.
OLLAMA_EDIT_MODEL = env("VINICUT_OLLAMA_EDIT_MODEL", "gemma4:12b")
OLLAMA_VISION_MODEL = env("VINICUT_OLLAMA_VISION_MODEL", "llama3.2-vision:latest")
# Keep the model resident in VRAM between pipeline calls (segmentation,
# scoring, copy pack all hit it back-to-back) instead of reloading each time.
OLLAMA_KEEP_ALIVE = env("VINICUT_OLLAMA_KEEP_ALIVE", "15m")
# Transcript prompts are long; the Ollama default context (often 4k) would
# silently truncate them.
OLLAMA_NUM_CTX = env_int("VINICUT_OLLAMA_NUM_CTX", 8192)

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = env("VINICUT_ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_BASE_URL = env("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_BASE_URL = env("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = env("VINICUT_OPENAI_MODEL", "gpt-4o-mini")

# Local models can take minutes on first call while loading into VRAM; 120s
# proved too tight in the field (read timeouts against gemma 26B).
LLM_TIMEOUT_SECONDS = env_int("VINICUT_LLM_TIMEOUT", 300)
LLM_MAX_RETRIES = env_int("VINICUT_LLM_RETRIES", 2)

WHISPER_MODEL_DEFAULT = env("VINICUT_WHISPER_MODEL", "large-v3")
# Whether Whisper stays resident between transcriptions. On Apple Silicon the
# memory is unified (no VRAM war with the local LLM), so keeping it loaded is
# free speed; on single-GPU Windows boxes unloading frees ~3 GB for Gemma.
WHISPER_KEEP_LOADED_DEFAULT = env(
    "VINICUT_WHISPER_KEEP_LOADED", "1" if sys.platform == "darwin" else "0"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = env("VINICUT_LOG_LEVEL", "INFO").upper()


class _ClientDisconnectNoiseFilter(logging.Filter):
    """
    Drops the scary-but-meaningless ConnectionResetError (WinError 10054)
    tracebacks Windows' proactor loop emits whenever a browser tab closes a
    WebSocket or aborts a video preview request mid-stream.
    """
    def filter(self, record):
        try:
            if record.exc_info and isinstance(record.exc_info[1], (ConnectionResetError, ConnectionAbortedError)):
                return False
            msg = record.getMessage()
            if "10054" in msg or "ConnectionResetError" in msg or "WinError 10054" in msg:
                return False
        except Exception:
            pass
        return True


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # FFmpeg spam lands on stderr, not the logger; keep third-party libs quieter.
    for noisy in ("botocore", "boto3", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Client-disconnect chatter on Windows is not an application error.
    noise_filter = _ClientDisconnectNoiseFilter()
    for name in ("asyncio", "uvicorn.error"):
        logging.getLogger(name).addFilter(noise_filter)


def get_logger(name):
    return logging.getLogger(name)
