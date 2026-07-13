# Vinicut AI — Auto Cutter 🎬

AI-powered short-form video factory: drop raw UGC footage in, get retention-optimized
5s / 15s / 30s / 60s vertical cuts out — transcribed, semantically segmented into
**Hook / Demo / CTA**, re-ordered into montage variations, and burned with karaoke-style
captions. Now with a **Claude-powered editorial brain**.

## What's new in v2.7 — local-first: built around Ollama + Gemma

The local engine is now the primary path, engineered for a 12B-class model:

- **Staged analysis for local models.** Instead of one giant nested-JSON
  request (Claude-grade work), Gemma answers three small, focused questions —
  boundaries, best cut ranges, variations — each with an independent fallback.
  One bad answer no longer discards the whole plan.
- **Whisper releases its VRAM (~3 GB) after every transcription** so Gemma has
  room on single-GPU machines — the classic cause of Ollama OOM crashes.
  (`whisper_keep_loaded=1` restores the old caching for multi-GPU setups.)
- **Ollama is the explicit default provider**, listed first in Settings; the
  local transcript budget is tuned to the 8k context window.
- Claude stays one dropdown away as an optional cloud upgrade.

## What's new in v2.3 — AI hook ranking + Portuguese-first

- **Hooks are ranked before you render.** Right after a creator hook is
  transcribed, the AI scores its scroll-stopping power (0-100) with a short
  critique — pattern interrupt, curiosity gap, specificity, clarity in the
  first second. The library sorts best-first, so you always test the
  strongest hooks first. Unscored hooks get a one-click "score" action.
- **Portuguese-first defaults.** Transcription is forced to `pt` out of the
  box (editable in Settings), the Whisper priming prompt is written in
  Portuguese, and every AI output — hook critiques, variation names and
  rationales, marketing copy — is generated in the video's own language.

## What's new in v2.2 — Hook Swap

Creators keep sending new hooks; the winning body stays the same. The Hook Swap
tab (↑↓ icon in the editor) turns that into a factory:

1. **The AI already cut the original hook** — its end boundary comes from the
   editorial analysis (adjustable by dragging the timeline handle).
2. **Upload creator hooks** into the reusable hook library. Each clip is
   probed, thumbnailed, and transcribed in the background so swapped videos
   keep full karaoke captions over the new hook too.
3. **Generate** — one full video per hook: `new hook + this video's body`,
   with per-clip framing (a phone-shot hook rides cleanly on a wide product
   video), silence jump-cuts on the body, loudness mastering, and burned
   captions. Subtitled + raw deliverables, one pair per hook, with per-hook
   failure isolation.

Endpoints: `POST/GET/DELETE /api/hooks`, `POST /api/projects/{id}/hook-swap`,
`GET /api/projects/{id}/download-hookswap/{swap_id}/{subs}`.

## What's new in v2.1 — high-end cutter upgrade

- **Framing engine (fixes wide-footage cropping)** — sources that aren't 9:16 are no
  longer blindly center-cropped. `auto` gently crops near-portrait footage and
  blur-fits wide footage (blurred, darkened canvas behind the full frame);
  `crop` / `fit_blur` / `fit_black` selectable per project in Style & Framing.
- **Silence jump-cuts** — silent gaps between words are skipped automatically
  (configurable threshold), giving cuts the tight pacing of professional
  short-form edits. Toggle in Style & Framing.
- **Broadcast audio mastering** — every deliverable is loudness-normalized to
  -14 LUFS (the TikTok/Reels/Shorts standard) with true-peak limiting.
- **Real encoder quality** — NVENC VBR CQ rate control (CRF on CPU fallback),
  guaranteed yuv420p, constant 30 fps, 48 kHz audio, and `+faststart` so files
  stream instantly.
- **AI virality scores** — every montage variation gets a 0-100 retention
  estimate from the AI, shown as a badge on its deliverable card.
- **Marketing copy pack** — one click generates alternative hooks, TikTok and
  Instagram captions, hashtags and CTA lines in the video's own language
  (new Copy tab, with copy-to-clipboard).
- **Real waveform timeline** — the editor timeline now shows the actual audio
  peaks of your footage (cached server-side).
- **Poster thumbnails** — deliverable previews show a real frame instantly.
- **Delete & Retry** — remove a project and all its files, or requeue a failed
  one, right from the project table.
- **Parallel workers** — set `VINICUT_WORKERS=2` (or 3) in `.env` to process
  multiple projects at once on GPUs with spare NVENC sessions.
- **Forced transcription language** — set `pt`, `en`, etc. in System settings
  for better accuracy on noisy audio ("auto" detects per video).
- Smarter AI prompting: the transcript sent to Claude now carries silence-gap
  annotations, measurably improving boundary picks.

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

1. **Double-click `run.bat`** — every launch verifies the whole stack and
   auto-installs anything missing: Python, FFmpeg, Ollama (winget), Python
   dependencies, NVIDIA GPU transcription libraries, the local AI model
   (`gemma4:12b`, ~8 GB on first run) and the Whisper model (~3 GB on first
   run). If a fresh Python install isn't visible yet, it tells you to reopen
   the window and continues from where it stopped.
2. **Enable Claude** *(optional)* — edit `.env` and paste your
   `ANTHROPIC_API_KEY`. The primary engine is local Ollama + Gemma.
3. **Verify** anytime with `run.bat test` (self-test suite).
   `install.bat` remains available as a verbose first-time installer.

## Quick start (macOS — Apple Silicon M1 or newer)

1. Install [Homebrew](https://brew.sh) if you don't have it.
2. In Terminal, inside the project folder:
   ```bash
   chmod +x run.sh && ./run.sh
   ```
   Like the Windows launcher, it self-heals on every run: installs Python,
   FFmpeg and Ollama via Homebrew, the Python dependencies, pulls
   `gemma4:12b` (Ollama uses the M-series GPU via Metal) and the Whisper
   model, then opens the app. `./run.sh test` runs the self-test suite.

Video encoding uses Apple's **VideoToolbox** hardware encoder (the M-series
media engine) with automatic CPU fallback; Whisper runs on CPU int8 via the
Accelerate framework. Intel Macs are not supported.

Linux: install FFmpeg + Ollama yourself, then
`pip install -r requirements.txt && python main.py`.

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
