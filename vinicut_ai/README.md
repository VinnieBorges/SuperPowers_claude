# Vinicut AI — Auto Cutter 🎬

AI-powered short-form video factory: drop raw UGC footage in, get retention-optimized
5s / 15s / 30s / 60s vertical cuts out — transcribed, semantically segmented into
**Hook / Demo / CTA**, re-ordered into montage variations, and burned with karaoke-style
captions. Now with a **Claude-powered editorial brain**.

## What's new in v2.0

### 🧠 Claude API integration (pluggable AI providers)
The editorial analysis no longer requires a local GPU LLM. One abstraction
(`llm.py`) routes every AI feature through your choice of provider:

| Provider | What it is | When to use |
|---|---|---|
| `auto` *(default)* | Claude if `ANTHROPIC_API_KEY` is set, else Ollama | Just works |
| `anthropic` | Claude API (cloud) | Best segmentation & copywriting quality, zero VRAM |
| `ollama` | Local models (original behavior) | Fully offline |
| `openai` | Any OpenAI-compatible endpoint | Groq, Together, LM Studio, vLLM... |

Claude drives all of it: Hook/Demo/CTA boundary detection, **per-duration best-cut
selection** (the model picks the exact source ranges for each target length),
creative montage variations with marketing rationale, AI subtitle style presets,
and the Whisper self-improvement loop. Switch providers and **Test AI Connection**
live from Settings (⚙️) — no restart needed.

### 🖥 Redesigned studio interface
The dashboard was rebuilt as a proper editing tool: dense project table with live
status and inline progress, three-pane studio view (captions / monitor / inspector)
with a draggable segment timeline, keyboard transport (Space to play, arrow keys to
seek), real diagnostic modals instead of browser alerts, and zero external
dependencies — system fonts only, fully offline. The AI engine (Claude or local)
is switchable from the System panel with one-click connection testing.

### ✅ Self-test suite
`run.bat test` (or `python tests/run_tests.py`) runs 16 checks: pure-logic unit
tests plus real FFmpeg renders over synthetic fixtures covering every shape users
upload — landscape 16:9 with audio, portrait 9:16 with no audio track and no
speech, a 2-second micro clip, a square video with a hostile filename
(apostrophe/&/unicode), reordered montage variations with crossfades, and corrupt
files (which must fail fast with a clear message). Tests run in an isolated temp
workspace and never touch your real database or media.

### 🛠 Reliability & pipeline fixes
- **New `ai_editor.py`** with strict JSON validation and clamping — malformed model
  output can never reach FFmpeg; a deterministic fallback plan always exists.
- **Per-project stop**: stopping one project now terminates only *its* FFmpeg
  processes (previously a global handle could kill another project's render).
- **Upload collision safety**: re-uploading `clip.mp4` becomes `clip_1.mp4` instead
  of overwriting another project's source footage.
- **Non-blocking uploads**: files stream to disk in 1 MiB chunks off the event loop.
- **Real render progress**: FFmpeg progress maps to the project bar across all
  stages (transcribe → analyze → standard cuts → AI variations).
- **Worker crash fix**: an error before a project was claimed no longer crashes the
  queue loop with an unbound variable.
- **No busy-wait broadcasting**; WebSocket UI now **auto-reconnects with backoff**
  instead of reloading the page.
- **ZIP downloads clean up after themselves**; janitor and retention are configurable.
- Central `config.py` (+ `.env` support), proper logging, DB indexes and safe
  in-place schema migrations.

## Quick start (Windows)

1. **Launch** — double-click `run.bat`. First run creates the virtualenv,
   installs dependencies, creates `.env` from the template, and opens the app
   at `http://127.0.0.1:8000`.
2. **Enable Claude** *(optional)* — edit `.env` and paste your
   `ANTHROPIC_API_KEY` (from console.anthropic.com), then restart. Without a
   key the app runs fully local via Ollama.
3. **Verify** — `run.bat test` runs the 16-check self-test suite.

FFmpeg + FFprobe must be on PATH (`winget install Gyan.FFmpeg`), or set
`VINICUT_FFMPEG` / `VINICUT_FFPROBE`. Linux/macOS: `pip install -r
requirements.txt && python main.py`.

## How the pipeline works

```
upload / watch-folder drop
        │
        ▼
 projects table (SQLite queue, crash-resumable)
        │  sequential_queue_worker
        ▼
 1. faster-whisper  → word-level transcript (grouped ≤3 words / 1.5s for captions)
 2. Claude/Ollama   → Hook/Demo/CTA boundaries + best source ranges per duration
                      + creative montage variations           (ai_editor.py)
 3. FFmpeg          → word-snapped semantic cuts (5/15/30/60s, raw + subbed,
                      9:16 1080×1920, NVENC w/ CPU fallback, xfade transitions,
                      karaoke ASS captions, retention zoom, music ducking)
        │
        ▼
 cuts/ + auto_cuts/edits_YYYY-MM-DD/ + live WebSocket progress in the dashboard
```

## Key files

| File | Role |
|---|---|
| `main.py` | FastAPI app: routes, queue worker, watch folder, WebSockets |
| `config.py` | All settings, env-driven, `.env` loader, logging |
| `llm.py` | Provider abstraction: Claude / Ollama / OpenAI-compatible + strict JSON |
| `ai_editor.py` | Editorial analysis: boundaries, best cuts per duration, variations |
| `processor.py` | Whisper transcription + render orchestration |
| `render_engine.py` | FFmpeg core: cuts, xfades, ASS subtitles, per-project process registry |
| `database.py` | SQLite schema/migrations, settings, self-improvement loop |
| `webui/index.html` | Zero-build dashboard (editor, timeline, outputs, settings) |

## API surface (unchanged + new)

All original endpoints are preserved. New:

- `GET /api/health` — liveness + active AI provider
- `GET /api/llm/status` — provider/model configuration snapshot
- `POST /api/llm/test` — round-trip test of the configured provider
