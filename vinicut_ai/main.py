"""
Vinicut AI — FastAPI backend.

Serves the dashboard, ingests uploads (drag & drop + watch folder), runs the
DB-backed sequential processing queue (Whisper transcription -> AI editorial
analysis via local Ollama or the Claude API -> FFmpeg renders), and streams
live progress to the UI over WebSockets.
"""
import asyncio
import json
import os
import queue
import re
import shutil
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

import config

config.setup_logging()
log = config.get_logger("main")

import database

database.init_db()

import ai_editor
import font_parser
import llm
import processor
import render_engine

APP_VERSION = "2.0.0"

UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MiB


@asynccontextmanager
async def lifespan(app: FastAPI):
    resume_interrupted_projects()
    bins = render_engine.check_binaries()
    if not (bins.get("ffmpeg") and bins.get("ffprobe")):
        log.error("FFmpeg/FFprobe NOT FOUND — rendering will fail until installed. "
                  "Windows: winget install Gyan.FFmpeg (then restart the app).")
    broadcast_task = asyncio.create_task(broadcast_loop())
    cleanup_task = asyncio.create_task(automated_cleanup_loop())
    for i in range(config.WORKERS):
        threading.Thread(target=sequential_queue_worker, daemon=True, name=f"queue-worker-{i + 1}").start()
    threading.Thread(target=watch_folder_worker, daemon=True, name="watch-folder").start()
    log.info("Vinicut AI v%s ready — %d worker(s) and watch folder active.", APP_VERSION, config.WORKERS)
    yield
    broadcast_task.cancel()
    cleanup_task.cancel()


app = FastAPI(title="Vinicut AI — Auto Cutter Backend", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# WebSocket broadcasting
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()

# Worker threads push messages here; broadcast_loop drains it on the event
# loop. The blocking get runs in a thread pool, so there is no polling.
broadcast_queue: "queue.Queue[dict]" = queue.Queue()


def broadcast_sync(message: dict):
    broadcast_queue.put(message)


def _next_broadcast(timeout=1.0):
    """Blocking dequeue with a timeout so shutdown never hangs on a parked thread."""
    try:
        return broadcast_queue.get(timeout=timeout)
    except queue.Empty:
        return None


async def broadcast_loop():
    while True:
        msg = await asyncio.to_thread(_next_broadcast)
        if msg is None:
            continue
        await manager.broadcast(msg)
        broadcast_queue.task_done()


@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # No client -> server messages expected; keep the connection alive.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    return database.get_db_connection()


def safe_media_filename(filename, allowed_exts=None):
    """
    Reduces an uploaded filename to a safe basename and (optionally) validates
    its extension, preventing path traversal (e.g. '..\\..\\evil') from escaping
    the intended upload directory.
    """
    base = os.path.basename(filename or "")
    base = base.replace("\\", "").replace("/", "").strip()
    if not base or base in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if allowed_exts is not None:
        ext = os.path.splitext(base)[1].lower()
        if ext not in allowed_exts:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(allowed_exts))}."
            )
    return base


def allocate_raw_filename(filename, directory=None):
    """
    Returns (filename, path) inside `directory` (RAW_DIR by default) that does
    not collide with an existing file — repeated uploads of 'clip.mp4' become
    clip_1.mp4, clip_2... instead of silently overwriting existing footage.
    """
    directory = directory or database.RAW_DIR
    raw_path = os.path.join(directory, filename)
    base_name, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(raw_path):
        filename = f"{base_name}_{counter}{ext}"
        raw_path = os.path.join(directory, filename)
        counter += 1
    return filename, raw_path


async def save_upload_async(upload: UploadFile, dest_path: str):
    """Streams an upload to disk in chunks without blocking the event loop."""
    with open(dest_path, "wb") as buffer:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            await asyncio.to_thread(buffer.write, chunk)


def create_project(filename):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO projects (filename, status) VALUES (?, 'pending')", (filename,))
    project_id = cursor.lastrowid
    conn.commit()
    conn.close()
    broadcast_sync({"type": "status", "project_id": project_id, "status": "pending", "progress": 0})
    return project_id


def is_project_stopped(project_id: int) -> bool:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT stop_requested, status FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        conn.close()
        return bool(row and (row[0] == 1 or row[1] == 'failed'))
    except Exception:
        return False


def set_project_status(project_id, status, progress=None, error=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if progress is not None and error is not None:
        cursor.execute("UPDATE projects SET status = ?, progress = ?, error_message = ? WHERE id = ?",
                       (status, progress, error, project_id))
    elif progress is not None:
        cursor.execute("UPDATE projects SET status = ?, progress = ? WHERE id = ?",
                       (status, progress, project_id))
    elif error is not None:
        cursor.execute("UPDATE projects SET status = ?, error_message = ? WHERE id = ?",
                       (status, error, project_id))
    else:
        cursor.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
    conn.commit()
    conn.close()
    msg = {"type": "status", "project_id": project_id, "status": status}
    if progress is not None:
        msg["progress"] = progress
    if error is not None:
        msg["error"] = error
    broadcast_sync(msg)


def make_progress_reporter(project_id, base_pct, span_pct):
    """
    Returns a callback(0-100) that maps a render stage's local progress onto
    the project's overall progress bar, throttled to whole-percent steps so
    the DB and WebSocket aren't hammered on every FFmpeg stderr line.
    """
    state = {"last": -1.0}

    def report(stage_pct):
        overall = min(99.0, base_pct + (max(0.0, min(100.0, stage_pct)) / 100.0) * span_pct)
        if overall - state["last"] < 1.0:
            return
        state["last"] = overall
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE projects SET progress = ? WHERE id = ?", (round(overall, 1), project_id))
            conn.commit()
            conn.close()
            broadcast_sync({"type": "status", "project_id": project_id,
                            "status": "rendering", "progress": round(overall, 1)})
        except Exception as e:
            log.debug("Progress update failed for project %s: %s", project_id, e)

    return report


# ---------------------------------------------------------------------------
# Background queue processors
# ---------------------------------------------------------------------------

# Serializes the select-then-update job claim so multiple workers never grab
# the same project.
_claim_lock = threading.Lock()


def claim_next_project():
    with _claim_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, filename FROM projects WHERE status IN ('pending', 'queued') ORDER BY id ASC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE projects SET status = 'analyzing', progress = 0 WHERE id = ?", (row[0],))
            conn.commit()
        conn.close()
        return row


def sequential_queue_worker():
    while True:
        project_id = None
        try:
            row = claim_next_project()
            if not row:
                time.sleep(1.0)
                continue

            project_id, filename = row
            render_engine.set_current_project(project_id)

            if is_project_stopped(project_id):
                set_project_status(project_id, "failed", error="Stopped by user")
                continue

            video_path = os.path.join(database.RAW_DIR, filename)
            if not os.path.exists(video_path):
                set_project_status(project_id, "failed", error=f"Source file missing: {filename}")
                continue

            # Fail fast on unreadable sources BEFORE spending minutes in Whisper.
            total_dur = processor.get_video_duration(video_path)
            if total_dur <= 0.0:
                bins = render_engine.check_binaries()
                if not (bins.get("ffmpeg") and bins.get("ffprobe")):
                    err = ("FFmpeg/FFprobe not found on this machine. Install FFmpeg "
                           "(winget install Gyan.FFmpeg) or set VINICUT_FFMPEG / VINICUT_FFPROBE.")
                else:
                    err = (f"Could not read '{filename}' — the file appears corrupt, "
                           "still uploading, or uses an unsupported codec.")
                set_project_status(project_id, "failed", error=err)
                continue

            # Step 1: transcription
            set_project_status(project_id, "analyzing", progress=0)
            try:
                trans_segs = processor.run_audio_transcription(video_path)
                transcription = [dict(s) for s in trans_segs]
            except Exception as e:
                log.warning("Transcription failed for project %s: %s", project_id, e)
                transcription = [{"start": 0.0, "end": 5.0, "text": "Failed to transcribe."}]

            # Step 2: AI editorial analysis (Claude / Ollama via llm.py)
            try:
                ai_data = ai_editor.analyze_transcript_and_segment(transcription, total_dur)
            except Exception as e:
                log.warning("AI analysis failed for project %s: %s", project_id, e)
                ai_data = ai_editor.get_fallback_segmentation(total_dur)

            segments_map = {
                "hook": ai_data["hook"],
                "demo": ai_data["demo"],
                "cta": ai_data["cta"],
                "standard_cuts": ai_data.get("standard_cuts", {}),
            }

            # Ready-to-post marketing copy (hooks/captions/hashtags). Optional:
            # a failure here must never block the render.
            marketing_json = None
            try:
                pack = ai_editor.generate_marketing_pack(transcription)
                if pack:
                    marketing_json = json.dumps(pack)
            except Exception as me:
                log.warning("Marketing pack failed for project %s: %s", project_id, me)

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE projects
                SET segments_map_json = ?, transcript_json = ?, marketing_json = COALESCE(?, marketing_json),
                    status = 'rendering', progress = 5
                WHERE id = ?
            """, (json.dumps(segments_map), json.dumps(transcription), marketing_json, project_id))
            conn.commit()
            conn.close()
            broadcast_sync({"type": "status", "project_id": project_id, "status": "rendering", "progress": 5})

            if is_project_stopped(project_id):
                raise RuntimeError("Project rendering was stopped by the user.")

            # Step 3: standard cuts (5s / 15s / 30s / 60s) — 5% -> 55% overall
            processor.render_final_cuts(
                project_id=project_id,
                filename=filename,
                original_video_path=video_path,
                segments=transcription,
                style_preset="Bold Yellow",
                segments_map=segments_map,
                order=["Hook", "Demo", "CTA"],
                progress_callback=make_progress_reporter(project_id, 5.0, 50.0),
            )

            # Step 4: AI montage variations — 55% -> 95% overall
            render_ai_variations(project_id, filename, video_path, transcription,
                                 segments_map, ai_data, total_dur)

            set_project_status(project_id, "completed", progress=100)
            log.info("Project %s completed.", project_id)

            try:
                database.run_self_improvement_loop(project_id)
            except Exception as se:
                log.warning("Self-improvement loop failed: %s", se)

        except Exception as ex:
            error_msg = str(ex)
            log.error("Queue worker error on project %s: %s", project_id, error_msg)
            if project_id is not None:
                try:
                    set_project_status(project_id, "failed", error=error_msg)
                except Exception as dberr:
                    log.error("Failed to record error to db: %s", dberr)
            else:
                # Error before a project was even claimed (e.g. DB unavailable):
                # back off so a persistent fault doesn't spin the loop.
                time.sleep(2.0)
        finally:
            render_engine.set_current_project(None)


def resolve_style_preset(project_id, default="Bold Yellow"):
    """Returns the project's style preset, honoring a custom AI-generated preset JSON."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT style_preset, custom_preset_json FROM projects WHERE id = ?", (project_id,))
    p_row = cursor.fetchone()
    conn.close()

    preset = p_row[0] if (p_row and p_row[0]) else default
    if p_row and p_row[1]:
        try:
            preset = json.loads(p_row[1])
        except ValueError:
            pass
    return preset


def get_project_framing(project_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT framing FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()
    return (row[0] if row and row[0] else "auto")


def render_ai_variations(project_id, filename, video_path, transcription, segments_map, ai_data, total_dur):
    """
    Renders every AI montage variation at every target duration (raw + subbed),
    registers them in ai_montages, and mirrors raw outputs to the daily
    auto_cuts folder.
    """
    resolved_preset = resolve_style_preset(project_id)
    framing = get_project_framing(project_id)
    transition_style, transition_duration = processor.get_transition_settings()
    animation, fade_ms = processor.get_subtitle_animation_settings()

    variations = ai_data.get("variations", [])
    durations = list(config.STANDARD_CUT_DURATIONS)
    combos_total = max(1, len(variations) * len(durations))
    combo_idx = 0
    progress = make_progress_reporter(project_id, 55.0, 40.0)

    date_str = datetime.now().strftime("%Y-%m-%d")
    auto_cuts_dir = os.path.join(database.AUTO_CUTS_DIR, f"edits_{date_str}")
    os.makedirs(auto_cuts_dir, exist_ok=True)
    base_name = os.path.splitext(filename)[0]

    for var in variations:
        var_name = var["name"]
        var_desc = var["description"]
        var_order = var["order"]

        for dur in durations:
            if is_project_stopped(project_id):
                raise RuntimeError("Project rendering was stopped by the user.")

            dur_name = f"{var_name} ({dur}s)"
            dur_desc = f"{var_desc} ({dur}s variation)"
            # Strict filesystem slug: AI-invented names like "Editor's Pick!"
            # must never leak quotes/punctuation into FFmpeg filter paths.
            safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", var_name).strip("_") or "variation"
            sub_path = os.path.join(database.CUTS_DIR, f"project_{project_id}_ai_montage_{safe_name}_{dur}s_subbed.mp4")
            raw_path = os.path.join(database.CUTS_DIR, f"project_{project_id}_ai_montage_{safe_name}_{dur}s_raw.mp4")

            # Render the raw concat ONCE (word-snapped) and capture its slice
            # plan; derive the subbed version by burning shifted captions onto
            # that short clip instead of concatenating twice.
            plan = None
            try:
                _, plan = render_engine.render_custom_reordered_cut(
                    video_path, segments_map, var_order, transcription, resolved_preset,
                    raw_path, target_duration=dur, total_dur=total_dur,
                    transition=transition_style, transition_duration=transition_duration,
                    animation=animation, fade_ms=fade_ms,
                    burn=False, return_slices=True, framing=framing,
                )
            except Exception as re_raw:
                log.error("Error rendering AI raw variation %s: %s", dur_name, re_raw)
                raw_path = ""

            if plan is not None and raw_path:
                try:
                    shifted = render_engine.shift_subtitles_for_slices(
                        transcription, plan["slices"], plan["use_xfade"], plan["trans_d"]
                    )
                    if shifted:
                        sub_ass = sub_path + ".ass"
                        render_engine.generate_ass_file(
                            shifted, resolved_preset, sub_ass,
                            animation=animation, fade_ms=fade_ms,
                            slices=plan["slices"], use_xfade=plan["use_xfade"], trans_d=plan["trans_d"],
                        )
                        render_engine.render_subtitles(raw_path, sub_ass, sub_path)
                    else:
                        shutil.copy2(raw_path, sub_path)
                except Exception as re_sub:
                    log.error("Error rendering AI subbed variation %s: %s", dur_name, re_sub)
                    sub_path = ""
            else:
                sub_path = ""

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ai_montages (project_id, name, description, order_json, filepath_subbed, filepath_raw, score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (project_id, dur_name, dur_desc, json.dumps(var_order), sub_path, raw_path, var.get("score")))
            conn.commit()
            conn.close()

            # Poster thumbnails for the dashboard.
            for outp in (raw_path, sub_path):
                if outp and os.path.exists(outp):
                    render_engine.generate_thumbnail(outp)

            # Mirror the raw variation into the daily auto_cuts folder.
            if raw_path and os.path.exists(raw_path):
                dest_path = os.path.join(auto_cuts_dir, f"{base_name}_{safe_name}_{dur}s_raw.mp4")
                try:
                    shutil.copy2(raw_path, dest_path)
                except Exception as cp_err:
                    log.warning("Error copying raw variation to auto_cuts: %s", cp_err)

            combo_idx += 1
            progress((combo_idx / combos_total) * 100.0)


def resume_interrupted_projects():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM projects WHERE status IN ('pending', 'queued', 'analyzing', 'rendering') ORDER BY id ASC")
        rows = cursor.fetchall()
        if rows:
            stuck_ids = [r[0] for r in rows]
            log.info("Found %d interrupted projects: %s. Resuming...", len(stuck_ids), stuck_ids)
            cursor.execute("UPDATE projects SET status = 'pending', stop_requested = 0 WHERE status IN ('analyzing', 'rendering')")
            conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Failed to resume interrupted projects: %s", e)


# ---------------------------------------------------------------------------
# Watch folder ingestion
# ---------------------------------------------------------------------------

def watch_folder_worker():
    watch_dir = database.WATCH_DIR
    file_sizes = {}

    while True:
        try:
            if not os.path.exists(watch_dir):
                time.sleep(3.0)
                continue

            video_files = []
            for root, _dirs, files_in_dir in os.walk(watch_dir):
                for f in files_in_dir:
                    video_files.append(os.path.join(root, f))

            # Drop tracking entries for files that vanished.
            for path in list(file_sizes.keys()):
                if not os.path.exists(path):
                    del file_sizes[path]

            for file_path in video_files:
                filename = os.path.basename(file_path)
                if os.path.splitext(filename)[1].lower() not in config.VIDEO_EXTENSIONS:
                    continue

                try:
                    current_size = os.path.getsize(file_path)
                except OSError:
                    continue

                if file_path not in file_sizes:
                    file_sizes[file_path] = (current_size, 0)
                    continue

                prev_size, checks_stable = file_sizes[file_path]
                if current_size != prev_size:
                    file_sizes[file_path] = (current_size, 0)
                    continue
                checks_stable += 1
                file_sizes[file_path] = (current_size, checks_stable)

                # Two consecutive stable size checks => the copy has finished.
                if checks_stable >= 2:
                    del file_sizes[file_path]
                    filename, raw_path = allocate_raw_filename(filename)

                    try:
                        shutil.move(file_path, raw_path)
                        parent_dir = os.path.dirname(file_path)
                        if parent_dir != watch_dir:
                            try:
                                if not os.listdir(parent_dir):
                                    os.rmdir(parent_dir)
                            except OSError:
                                pass
                    except Exception as mv_err:
                        log.error("Error moving file from watch folder: %s", mv_err)
                        continue

                    project_id = create_project(filename)
                    log.info("[Watch Folder] Ingested %s as Project #%s", filename, project_id)

        except Exception as e:
            log.error("Watch worker error: %s", e)

        time.sleep(1.5)


# ---------------------------------------------------------------------------
# Manual re-render pipeline (Export button)
# ---------------------------------------------------------------------------

def bg_run_manual_render(project_id: int, filename: str, video_path: str, segments: list,
                         style_preset: str, segments_map: dict, order: list):
    try:
        render_engine.set_current_project(project_id)

        # Log the editor's subtitle corrections for the self-improvement loop.
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT transcript_json FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        conn.close()

        if row and row[0]:
            original_segments = json.loads(row[0])
            for idx, (orig, curr) in enumerate(zip(original_segments, segments)):
                if (orig["text"].strip() != curr["text"].strip()
                        or abs(orig["start"] - curr["start"]) > 0.05
                        or abs(orig["end"] - curr["end"]) > 0.05):
                    database.log_subtitle_correction(
                        project_id=project_id,
                        segment_index=idx,
                        start_time=curr["start"],
                        end_time=curr["end"],
                        original_text=orig["text"],
                        corrected_text=curr["text"],
                    )

        set_project_status(project_id, "rendering", progress=0)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT font_family, zoom_effect, bg_music_path, framing FROM projects WHERE id = ?", (project_id,))
        p_row = cursor.fetchone()
        conn.close()

        font_family = p_row[0] if (p_row and p_row[0]) else "Montserrat"
        zoom_effect = int(p_row[1]) if (p_row and p_row[1] is not None) else 1
        bg_music_path = p_row[2] if (p_row and p_row[2]) else None
        framing = p_row[3] if (p_row and p_row[3]) else "auto"
        resolved_preset = resolve_style_preset(project_id, default=style_preset or "Bold Yellow")

        if is_project_stopped(project_id):
            raise RuntimeError("Project rendering was stopped by the user.")

        processor.render_final_cuts(
            project_id=project_id,
            filename=filename,
            original_video_path=video_path,
            segments=segments,
            style_preset=resolved_preset,
            segments_map=segments_map,
            order=order,
            progress_callback=make_progress_reporter(project_id, 0.0, 60.0),
        )

        # Re-render existing AI variations with the (possibly new) preset.
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, order_json, filepath_subbed, filepath_raw FROM ai_montages WHERE project_id = ?", (project_id,))
        montages_rows = cursor.fetchall()
        conn.close()

        transition_style, transition_duration = processor.get_transition_settings()
        animation, fade_ms = processor.get_subtitle_animation_settings()
        manual_total_dur = processor.get_video_duration(video_path)
        progress = make_progress_reporter(project_id, 60.0, 38.0)

        for m_idx, (m_name, m_order_json, m_filepath_sub, m_filepath_raw) in enumerate(montages_rows):
            if is_project_stopped(project_id):
                raise RuntimeError("Project rendering was stopped by the user.")
            m_order = json.loads(m_order_json)
            try:
                m_dur = int(m_name.split(" (")[1].replace("s)", ""))
            except (IndexError, ValueError):
                m_dur = None

            # Render the raw concat once (word-snapped), reusing one decode.
            raw_target = m_filepath_raw or ((m_filepath_sub + ".rawtmp.mp4") if m_filepath_sub else None)
            plan = None
            if raw_target:
                try:
                    _, plan = render_engine.render_custom_reordered_cut(
                        video_path, segments_map, m_order, segments, resolved_preset,
                        raw_target, font_family=font_family, zoom_effect=zoom_effect,
                        bg_music_path=bg_music_path, target_duration=m_dur,
                        total_dur=manual_total_dur, transition=transition_style,
                        transition_duration=transition_duration,
                        animation=animation, fade_ms=fade_ms,
                        burn=False, return_slices=True, framing=framing,
                    )
                except Exception as ex_raw:
                    log.error("Error re-rendering AI variation raw: %s", ex_raw)

            if m_filepath_sub and plan is not None and raw_target and os.path.exists(raw_target):
                try:
                    shifted = render_engine.shift_subtitles_for_slices(
                        segments, plan["slices"], plan["use_xfade"], plan["trans_d"]
                    )
                    if shifted:
                        sub_ass = m_filepath_sub + ".ass"
                        render_engine.generate_ass_file(
                            shifted, resolved_preset, sub_ass,
                            font_family=font_family, animation=animation, fade_ms=fade_ms,
                            slices=plan["slices"], use_xfade=plan["use_xfade"], trans_d=plan["trans_d"],
                        )
                        render_engine.render_subtitles(raw_target, sub_ass, m_filepath_sub)
                    else:
                        shutil.copy2(raw_target, m_filepath_sub)
                except Exception as ex_sub:
                    log.error("Error re-rendering AI variation subbed: %s", ex_sub)

            # Remove the temp raw if it wasn't itself an output target.
            if not m_filepath_raw and raw_target and os.path.exists(raw_target):
                try:
                    os.remove(raw_target)
                except OSError:
                    pass

            progress(((m_idx + 1) / max(1, len(montages_rows))) * 100.0)

        set_project_status(project_id, "completed", progress=100)

        try:
            database.run_self_improvement_loop(project_id)
        except Exception as se:
            log.warning("Self-improvement loop failed: %s", se)

    except Exception as e:
        log.error("Error in background manual render: %s", e)
        try:
            set_project_status(project_id, "failed", error=str(e))
        except Exception as dberr:
            log.error("Failed to record manual render error: %s", dberr)
    finally:
        render_engine.set_current_project(None)


# ---------------------------------------------------------------------------
# Storage janitor
# ---------------------------------------------------------------------------

async def automated_cleanup_loop():
    while True:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT filename FROM projects WHERE status = 'completed' AND created_at < datetime('now', ?)",
                (f"-{config.RAW_RETENTION_DAYS} days",),
            )
            rows = cursor.fetchall()
            conn.close()

            for (filename,) in rows:
                raw_path = os.path.join(database.RAW_DIR, filename)
                if os.path.exists(raw_path):
                    try:
                        os.remove(raw_path)
                        log.info("[Cleanup] Deleted aged raw file: %s", raw_path)
                    except OSError:
                        pass
        except Exception as e:
            log.warning("Cleanup loop error: %s", e)
        await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SubtitleSegmentModel(BaseModel):
    start: float
    end: float
    text: str
    words: Optional[List[dict]] = None


class RenderRequest(BaseModel):
    transcript: List[SubtitleSegmentModel]
    style_preset: str
    order: List[str]
    font_family: Optional[str] = "Montserrat"
    zoom_effect: Optional[int] = 1
    segments_map: Optional[dict] = None
    framing: Optional[str] = None  # auto | crop | fit_blur | fit_black


class SubtitlePromptRequest(BaseModel):
    prompt: str


class BulkDownloadRequest(BaseModel):
    project_ids: List[int]


class SettingsUpdateRequest(BaseModel):
    settings: dict


class HookSwapRequest(BaseModel):
    hook_ids: Optional[List[int]] = None  # None / empty = every hook in the library


# ---------------------------------------------------------------------------
# Upload / project endpoints
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    filename = safe_media_filename(file.filename, config.VIDEO_EXTENSIONS)
    filename, raw_path = allocate_raw_filename(filename)
    await save_upload_async(file, raw_path)
    project_id = create_project(filename)
    return {"project_id": project_id, "filename": filename, "status": "pending"}


@app.post("/api/upload-bulk")
async def upload_bulk(files: List[UploadFile] = File(...)):
    uploaded_projects = []
    for file in files:
        filename = safe_media_filename(file.filename, config.VIDEO_EXTENSIONS)
        filename, raw_path = allocate_raw_filename(filename)
        await save_upload_async(file, raw_path)
        project_id = create_project(filename)
        uploaded_projects.append({"id": project_id, "filename": filename, "status": "pending"})
    return {"message": f"Enqueued {len(files)} videos successfully.", "projects": uploaded_projects}


@app.get("/api/projects")
def list_projects():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, filename, created_at, status, progress, error_message FROM projects ORDER BY id DESC"
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "filename": r[1],
            "created_at": r[2],
            "status": r[3],
            "progress": r[4],
            "error_message": r[5],
        }
        for r in rows
    ]


@app.get("/api/projects/{project_id}")
def get_project_details(project_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, filename, status, segments_map_json, transcript_json, style_preset,
               font_family, zoom_effect, bg_music_path, custom_preset_json, framing, marketing_json
        FROM projects WHERE id = ?
    """, (project_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Project not found")

    cursor.execute("SELECT cut_type, filepath FROM cuts WHERE project_id = ?", (project_id,))
    cuts_rows = cursor.fetchall()

    cursor.execute("""
        SELECT name, description, order_json, filepath_subbed, filepath_raw, score
        FROM ai_montages WHERE project_id = ?
    """, (project_id,))
    ai_rows = cursor.fetchall()

    cursor.execute("""
        SELECT id, hook_label, filepath_subbed, filepath_raw
        FROM hook_swaps WHERE project_id = ? ORDER BY id
    """, (project_id,))
    swap_rows = cursor.fetchall()
    conn.close()

    cuts = {}
    for cut_type, path in cuts_rows:
        cuts[cut_type] = os.path.basename(path) if path else None

    ai_montages = [
        {
            "name": name,
            "description": desc,
            "order": json.loads(order_json),
            "filename_subbed": os.path.basename(filepath_sub) if filepath_sub else None,
            "filename_raw": os.path.basename(filepath_raw) if filepath_raw else None,
            "score": score,
        }
        for name, desc, order_json, filepath_sub, filepath_raw, score in ai_rows
    ]

    marketing = None
    if row[11]:
        try:
            marketing = json.loads(row[11])
        except ValueError:
            pass

    return {
        "id": row[0],
        "filename": row[1],
        "status": row[2],
        "segments_map": json.loads(row[3]) if row[3] else None,
        "transcript": json.loads(row[4]) if row[4] else None,
        "style_preset": row[5] or "Bold Yellow",
        "font_family": row[6] or "Montserrat",
        "zoom_effect": row[7] if row[7] is not None else 1,
        "bg_music_path": os.path.basename(row[8]) if row[8] else None,
        "custom_preset": json.loads(row[9]) if row[9] else None,
        "framing": row[10] or "auto",
        "marketing": marketing,
        "cuts": cuts,
        "ai_montages": ai_montages,
        "hook_swaps": [
            {
                "id": sid,
                "label": label,
                "filename_subbed": os.path.basename(sub) if sub else None,
                "filename_raw": os.path.basename(raw) if raw else None,
            }
            for sid, label, sub, raw in swap_rows
        ],
    }


# ---------------------------------------------------------------------------
# AI endpoints (Claude / Ollama via llm.py)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/montage")
def get_ai_montage_recommendation(project_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT transcript_json, segments_map_json FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()

    if not row or not row[0] or not row[1]:
        raise HTTPException(status_code=400, detail="Project transcripts are not analyzed yet.")

    transcript = json.loads(row[0])
    segments_map = json.loads(row[1])

    hook_text = " ".join(s["text"] for s in transcript if float(s["start"]) < segments_map["hook"][1])
    demo_text = " ".join(s["text"] for s in transcript
                         if segments_map["demo"][0] <= float(s["start"]) < segments_map["demo"][1])
    cta_text = " ".join(s["text"] for s in transcript if float(s["start"]) >= segments_map["cta"][0])

    prompt = f"""You are the Creative UGC Montage Editor.
Analyze this UGC script and recommend the sequence:
Hook: "{hook_text.strip()}"
Demo: "{demo_text.strip()}"
CTA: "{cta_text.strip()}"

Return JSON:
{{
  "recommended_order": ["Segment1", "Segment2", "Segment3"],
  "reasoning": "Brief explanation."
}}
"""
    try:
        return llm.chat_json(prompt)
    except Exception:
        return {
            "recommended_order": ["CTA", "Hook", "Demo"],
            "reasoning": "Curiosity pattern sequence loop.",
        }


@app.post("/api/projects/{project_id}/generate-subtitle-preset")
def generate_subtitle_preset(project_id: int, req: SubtitlePromptRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    prompt = f"""You are an expert ASS subtitle stylist.
The user wants custom subtitle styling described as: "{req.prompt}"

Generate a styling config JSON object for ASS subtitles.
Colors in ASS use the BGR format: "&H00BBGGRR" (Blue, Green, Red).
For example:
- Pure Red is "&H000000FF"
- Pure Green is "&H0000FF00"
- Pure Blue is "&H00FF0000"
- Pure Yellow is "&H0000FFFF"
- Pure White is "&H00FFFFFF"
- Pure Black is "&H00000000"
- Pure Neon Cyan is "&H00FFFF00"
- Pure Neon Pink/Magenta is "&H00FF00FF"

Format:
- fontsize: Integer (usually between 40 and 90, e.g. 70)
- bold: 0 (normal) or -1 (bold)
- border_style: 1 (outline) or 3 (solid background box)
- outline: float border width (e.g. 3.0)
- shadow: float shadow depth (e.g. 2.0)
- alignment: 2 (bottom center), 8 (top center), 5 (middle center)
- margin_v: vertical margin from edge (e.g. 65)
- primary: hex BGR string for main text (e.g. "&H00FFFFFF")
- highlight: hex BGR string for active/highlighted word (e.g. "&H0000FFFF")
- outline_col: hex BGR string for outline (e.g. "&H00000000")
- shadow_col: hex BGR string for shadow/box (e.g. "&H00000000")
- active_word_tags: string containing ASS formatting tags applied to the active/highlighted word. Must start with a backslash and include formatting or animation tags. Do NOT wrap in double braces.
  Examples:
  - Bouncing/Zoom active word: "\\\\fscx120\\\\fscy120\\\\c[highlight_color]" (replace [highlight_color] with your selected highlight color, e.g., "\\\\fscx120\\\\fscy120\\\\c&H0000FFFF&")
  - High Bounce and Red Neon Glow active word: "\\\\fscx130\\\\fscy130\\\\c&H000000FF&\\\\xbord5\\\\ybord5"
  - Standard highlight color only: "\\\\c[highlight_color]"
- inactive_word_tags: string containing ASS formatting tags applied to the inactive words. Should restore scale and color to default.
  Examples:
  - Restore scale and primary color: "\\\\fscx100\\\\fscy100\\\\c[primary_color]" (replace [primary_color] with your selected primary color, e.g., "\\\\fscx100\\\\fscy100\\\\c&H00FFFFFF&")
  - Standard primary color only: "\\\\c[primary_color]"

Return ONLY a valid JSON object. Do not include markdown code blocks, explanations, or any extra text.

JSON Output Format:
{{
  "fontsize": 75,
  "bold": -1,
  "border_style": 1,
  "outline": 4.0,
  "shadow": 0.0,
  "alignment": 2,
  "margin_v": 70,
  "primary": "&H00FFFFFF",
  "highlight": "&H0000FF00",
  "outline_col": "&H00000000",
  "shadow_col": "&H00000000",
  "active_word_tags": "\\\\fscx120\\\\fscy120\\\\c&H0000FF00&",
  "inactive_word_tags": "\\\\fscx100\\\\fscy100\\\\c&H00FFFFFF&"
}}
"""
    try:
        data = llm.chat_json(prompt)
        if not isinstance(data, dict):
            raise ValueError("Model did not return a JSON object.")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE projects SET custom_preset_json = ? WHERE id = ?", (json.dumps(data), project_id))
        conn.commit()
        conn.close()

        log.info("[AI Presets] Configured custom styling preset for project %s.", project_id)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI preset generation failed: {e}")


@app.get("/api/llm/status")
def llm_status():
    return llm.provider_status()


@app.post("/api/llm/test")
def llm_test(provider: Optional[str] = None):
    """Round-trips a tiny prompt through the selected provider to verify it works."""
    return llm.test_connection(provider)


# ---------------------------------------------------------------------------
# Render / stop
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/render")
def trigger_render(project_id: int, req: RenderRequest, background_tasks: BackgroundTasks):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename, segments_map_json FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Project not found")

    filename = row[0]
    db_map = json.loads(row[1]) if row[1] else {"hook": [0.0, 5.0], "demo": [5.0, 20.0], "cta": [20.0, 25.0]}
    segments_map = req.segments_map if req.segments_map else db_map
    video_path = os.path.join(database.RAW_DIR, filename)

    segments_list = []
    for s in req.transcript:
        d = dict(s)
        if d.get("words") is not None:
            d["words"] = [dict(w) for w in s.words] if s.words else []
        segments_list.append(d)

    framing = req.framing if (req.framing in render_engine.FRAMING_MODES) else None
    cursor.execute("""
        UPDATE projects
        SET style_preset = ?, font_family = ?, zoom_effect = ?, transcript_json = ?,
            segments_map_json = ?, framing = COALESCE(?, framing),
            status = 'rendering', stop_requested = 0, error_message = NULL
        WHERE id = ?
    """, (req.style_preset, req.font_family, req.zoom_effect, json.dumps(segments_list),
          json.dumps(segments_map), framing, project_id))
    conn.commit()
    conn.close()

    background_tasks.add_task(
        bg_run_manual_render,
        project_id, filename, video_path, segments_list,
        req.style_preset, segments_map, req.order,
    )
    return {"status": "rendering"}


@app.post("/api/projects/{project_id}/stop")
def stop_project(project_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE projects SET status = 'failed', stop_requested = 1, error_message = 'Stopped by user' WHERE id = ?",
        (project_id,),
    )
    conn.commit()
    conn.close()

    # Only terminate FFmpeg processes belonging to THIS project.
    render_engine.terminate_project_processes(project_id)

    broadcast_sync({"type": "status", "project_id": project_id, "status": "failed", "error": "Stopped by user"})
    return {"status": "success", "message": "Project rendering stopped successfully."}


def _project_artifact_paths(project_id, cursor):
    """All files a project owns on disk: cuts, montages, sidecar .ass/.jpg files."""
    paths = []
    cursor.execute("SELECT filepath FROM cuts WHERE project_id = ?", (project_id,))
    paths += [r[0] for r in cursor.fetchall() if r[0]]
    cursor.execute("SELECT filepath_subbed, filepath_raw FROM ai_montages WHERE project_id = ?", (project_id,))
    for sub, raw in cursor.fetchall():
        paths += [p for p in (sub, raw) if p]
    cursor.execute("SELECT filepath_subbed, filepath_raw FROM hook_swaps WHERE project_id = ?", (project_id,))
    for sub, raw in cursor.fetchall():
        paths += [p for p in (sub, raw) if p]
    with_sidecars = []
    for p in paths:
        with_sidecars += [p, p + ".ass", p + ".jpg"]
    return with_sidecars


@app.post("/api/projects/{project_id}/retry")
def retry_project(project_id: int):
    """Requeues a project for a full re-run (used on failed or completed projects)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Project not found")
    if row[0] in ("pending", "queued", "analyzing", "rendering"):
        conn.close()
        raise HTTPException(status_code=400, detail="Project is already queued or processing.")

    # Old montage rows would duplicate on the re-run; the files get overwritten.
    cursor.execute("DELETE FROM ai_montages WHERE project_id = ?", (project_id,))
    cursor.execute("""
        UPDATE projects SET status = 'pending', progress = 0, stop_requested = 0, error_message = NULL
        WHERE id = ?
    """, (project_id,))
    conn.commit()
    conn.close()
    broadcast_sync({"type": "status", "project_id": project_id, "status": "pending", "progress": 0})
    return {"status": "pending"}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int):
    """Removes a project: DB rows plus every file it produced or uploaded."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename, bg_music_path FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Project not found")
    filename, bg_music_path = row

    render_engine.terminate_project_processes(project_id)

    doomed = _project_artifact_paths(project_id, cursor)
    doomed.append(os.path.join(database.RAW_DIR, filename))
    raw_main = os.path.join(database.RAW_DIR, filename)
    doomed += [raw_main + ".peaks.json"]

    cursor.execute("DELETE FROM cuts WHERE project_id = ?", (project_id,))
    cursor.execute("DELETE FROM ai_montages WHERE project_id = ?", (project_id,))
    cursor.execute("DELETE FROM hook_swaps WHERE project_id = ?", (project_id,))
    cursor.execute("DELETE FROM subtitle_corrections WHERE project_id = ?", (project_id,))
    cursor.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()

    removed = 0
    for path in doomed:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            pass
    music_dir = os.path.join(database.RAW_DIR, f"project_{project_id}_music")
    if bg_music_path and os.path.isdir(music_dir):
        shutil.rmtree(music_dir, ignore_errors=True)

    log.info("Deleted project %s (%d files removed).", project_id, removed)
    return {"status": "deleted", "files_removed": removed}


@app.post("/api/projects/{project_id}/marketing")
def regenerate_marketing(project_id: int):
    """(Re)generates the AI marketing pack for a project on demand."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT transcript_json FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    if not row[0]:
        raise HTTPException(status_code=400, detail="Project has no transcript yet.")

    pack = ai_editor.generate_marketing_pack(json.loads(row[0]))
    if not pack:
        raise HTTPException(status_code=502, detail="The AI engine did not return usable copy. Check the AI connection in Settings.")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE projects SET marketing_json = ? WHERE id = ?", (json.dumps(pack), project_id))
    conn.commit()
    conn.close()
    return pack


@app.get("/api/projects/{project_id}/waveform")
def project_waveform(project_id: int, buckets: int = 240):
    """
    Amplitude peaks of the source audio for the timeline (cached on disk).
    Returns {"peaks": [0..1 floats], "duration": seconds}.
    """
    buckets = max(40, min(1000, buckets))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    raw_path = os.path.join(database.RAW_DIR, row[0])
    if not os.path.exists(raw_path):
        raise HTTPException(status_code=404, detail="Source file not available.")

    cache_path = raw_path + ".peaks.json"
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    import array
    import subprocess
    # Binary PCM must bypass the text-mode run_command helper.
    try:
        proc = subprocess.run(
            [render_engine.FFMPEG_BIN, "-v", "error", "-i", raw_path, "-map", "a:0?",
             "-ac", "1", "-ar", "4000", "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=render_engine.get_ffmpeg_env(), timeout=180,
        )
        pcm = proc.stdout or b""
    except (OSError, subprocess.SubprocessError):
        pcm = b""
    duration = render_engine.get_video_duration(raw_path)
    samples = array.array("h")
    usable = len(pcm) - (len(pcm) % 2)
    if usable:
        samples.frombytes(pcm[:usable])

    peaks = []
    if len(samples) > 0:
        step = max(1, len(samples) // buckets)
        for i in range(0, len(samples) - step + 1, step):
            chunk = samples[i:i + step]
            peaks.append(round(max(abs(s) for s in chunk) / 32768.0, 3))
    payload = {"peaks": peaks, "duration": duration}
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass
    return payload


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/download/{cut_type}/{with_subs}")
def download_cut(project_id: int, cut_type: str, with_subs: str):
    conn = get_db_connection()
    cursor = conn.cursor()

    db_cut_type = cut_type
    if with_subs.lower() != "true":
        db_cut_type = f"{cut_type}_raw"
        if not db_cut_type.endswith("s_raw") and db_cut_type != "custom_raw":
            db_cut_type = f"{cut_type}s_raw"

    cursor.execute("SELECT filepath FROM cuts WHERE project_id = ? AND cut_type = ?", (project_id, db_cut_type))
    row = cursor.fetchone()
    conn.close()

    if not row or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail=f"Cut {db_cut_type} not found or not rendered yet.")

    return FileResponse(row[0], media_type="video/mp4", filename=os.path.basename(row[0]))


@app.get("/api/projects/{project_id}/download-ai/{montage_name}/{with_subs}")
def download_ai_montage(project_id: int, montage_name: str, with_subs: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT filepath_subbed, filepath_raw
        FROM ai_montages WHERE project_id = ? AND name = ?
    """, (project_id, montage_name))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="AI montage not found")

    filepath = row[0] if with_subs.lower() == "true" else row[1]
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Rendered file not found")

    return FileResponse(filepath, media_type="video/mp4", filename=os.path.basename(filepath))


def _zip_response(paths_to_zip, zip_filename):
    """Builds a ZIP in CUTS_DIR and returns it, deleting the archive after send."""
    zip_filepath = os.path.join(database.CUTS_DIR, zip_filename)
    try:
        with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for file_path, archive_name in paths_to_zip:
                zip_file.write(file_path, archive_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create ZIP package: {e}")

    def _cleanup():
        try:
            os.remove(zip_filepath)
        except OSError:
            pass

    return FileResponse(zip_filepath, media_type="application/zip",
                        filename=zip_filename, background=BackgroundTask(_cleanup))


@app.get("/api/projects/{project_id}/download-zip")
def download_all_cuts_zip(project_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT cut_type, filepath FROM cuts WHERE project_id = ?", (project_id,))
    cuts_rows = cursor.fetchall()
    cursor.execute("SELECT name, filepath_subbed, filepath_raw FROM ai_montages WHERE project_id = ?", (project_id,))
    ai_rows = cursor.fetchall()
    conn.close()

    paths_to_zip = []
    for _cut_type, filepath in cuts_rows:
        if filepath and os.path.exists(filepath):
            paths_to_zip.append((filepath, f"standard_cuts/{os.path.basename(filepath)}"))
    for _name, filepath_sub, filepath_raw in ai_rows:
        if filepath_sub and os.path.exists(filepath_sub):
            paths_to_zip.append((filepath_sub, f"ai_montages/{os.path.basename(filepath_sub)}"))
        if filepath_raw and os.path.exists(filepath_raw):
            paths_to_zip.append((filepath_raw, f"ai_montages/{os.path.basename(filepath_raw)}"))

    if not paths_to_zip:
        raise HTTPException(status_code=404, detail="No rendered cuts or montages found to package.")

    return _zip_response(paths_to_zip, f"project_{project_id}_all_cuts.zip")


@app.post("/api/projects/download-zip-bulk")
def download_bulk_zip(req: BulkDownloadRequest):
    paths_to_zip = []
    conn = get_db_connection()
    cursor = conn.cursor()

    for project_id in req.project_ids:
        cursor.execute("SELECT filename FROM projects WHERE id = ?", (project_id,))
        p_row = cursor.fetchone()
        if not p_row:
            continue
        p_name = os.path.splitext(p_row[0])[0]

        cursor.execute("SELECT cut_type, filepath FROM cuts WHERE project_id = ?", (project_id,))
        for _cut_type, filepath in cursor.fetchall():
            if filepath and os.path.exists(filepath):
                paths_to_zip.append((filepath, f"project_{project_id}_{p_name}/standard_cuts/{os.path.basename(filepath)}"))

        cursor.execute("SELECT name, filepath_subbed, filepath_raw FROM ai_montages WHERE project_id = ?", (project_id,))
        for _name, filepath_sub, filepath_raw in cursor.fetchall():
            if filepath_sub and os.path.exists(filepath_sub):
                paths_to_zip.append((filepath_sub, f"project_{project_id}_{p_name}/ai_montages/{os.path.basename(filepath_sub)}"))
            if filepath_raw and os.path.exists(filepath_raw):
                paths_to_zip.append((filepath_raw, f"project_{project_id}_{p_name}/ai_montages/{os.path.basename(filepath_raw)}"))

    conn.close()

    if not paths_to_zip:
        raise HTTPException(status_code=404, detail="No rendered assets found for selected projects.")

    return _zip_response(paths_to_zip, f"bulk_export_{int(time.time())}.zip")


# ---------------------------------------------------------------------------
# Hook library + hook swapping
# ---------------------------------------------------------------------------

def _transcribe_hook_async(hook_id, path):
    """Transcribes an uploaded hook clip in the background so swapped videos
    can carry captions over the new hook too. Best effort — a hook without a
    transcript still swaps fine (body captions only)."""
    def work():
        try:
            segs = processor.run_audio_transcription(path)
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE hooks SET transcript_json = ? WHERE id = ?",
                           (json.dumps([dict(s) for s in segs]), hook_id))
            conn.commit()
            conn.close()
            log.info("Hook %s transcribed (%d caption groups).", hook_id, len(segs))
        except Exception as e:
            log.warning("Hook %s transcription skipped: %s", hook_id, e)

    threading.Thread(target=work, daemon=True, name=f"hook-transcribe-{hook_id}").start()


@app.post("/api/hooks")
async def upload_hook(file: UploadFile = File(...)):
    """Adds a creator hook clip to the reusable hook library."""
    filename = safe_media_filename(file.filename, config.VIDEO_EXTENSIONS)
    filename, hook_path = allocate_raw_filename(filename, directory=config.HOOKS_DIR)
    await save_upload_async(file, hook_path)

    duration = render_engine.get_video_duration(hook_path)
    if duration <= 0:
        try:
            os.remove(hook_path)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=f"Could not read '{filename}' — corrupt file or unsupported codec.")

    label = os.path.splitext(filename)[0]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO hooks (filename, label, duration) VALUES (?, ?, ?)",
                   (filename, label, duration))
    hook_id = cursor.lastrowid
    conn.commit()
    conn.close()

    render_engine.generate_thumbnail(hook_path)
    _transcribe_hook_async(hook_id, hook_path)
    return {"id": hook_id, "filename": filename, "label": label, "duration": round(duration, 2)}


@app.get("/api/hooks")
def list_hooks():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, filename, label, duration, transcript_json IS NOT NULL, created_at FROM hooks ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r[0], "filename": r[1], "label": r[2], "duration": round(r[3] or 0, 2),
         "has_transcript": bool(r[4]), "created_at": r[5]}
        for r in rows
    ]


@app.delete("/api/hooks/{hook_id}")
def delete_hook(hook_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM hooks WHERE id = ?", (hook_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Hook not found")
    cursor.execute("DELETE FROM hooks WHERE id = ?", (hook_id,))
    conn.commit()
    conn.close()
    for suffix in ("", ".jpg", ".ass"):
        try:
            os.remove(os.path.join(config.HOOKS_DIR, row[0]) + suffix)
        except OSError:
            pass
    return {"status": "deleted"}


def bg_run_hook_swaps(project_id: int, hook_rows: list):
    """Renders one deliverable per replacement hook: new hook + base video body."""
    try:
        render_engine.set_current_project(project_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT filename, segments_map_json, transcript_json, font_family, zoom_effect, framing
            FROM projects WHERE id = ?
        """, (project_id,))
        p = cursor.fetchone()
        conn.close()
        if not p:
            return

        filename, smap_json, transcript_json, font_family, zoom_effect, framing = p
        video_path = os.path.join(database.RAW_DIR, filename)
        segments_map = json.loads(smap_json)
        base_segments = json.loads(transcript_json) if transcript_json else []
        body_start = float(segments_map["hook"][1])
        resolved_preset = resolve_style_preset(project_id)
        animation, fade_ms = processor.get_subtitle_animation_settings()
        total_dur = processor.get_video_duration(video_path)

        set_project_status(project_id, "rendering", progress=0)
        progress = make_progress_reporter(project_id, 0.0, 99.0)

        done, failed = 0, 0
        for idx, hook in enumerate(hook_rows):
            if is_project_stopped(project_id):
                raise RuntimeError("Hook swapping was stopped by the user.")
            hook_id, hook_filename, hook_label, hook_transcript_json = hook
            hook_path = os.path.join(config.HOOKS_DIR, hook_filename)
            slug = re.sub(r"[^A-Za-z0-9_-]+", "_", hook_label or f"hook_{hook_id}").strip("_") or f"hook_{hook_id}"
            raw_out = os.path.join(database.CUTS_DIR, f"project_{project_id}_hookswap_{hook_id}_{slug}_raw.mp4")
            sub_out = os.path.join(database.CUTS_DIR, f"project_{project_id}_hookswap_{hook_id}_{slug}.mp4")
            try:
                hook_segments = json.loads(hook_transcript_json) if hook_transcript_json else []
                render_engine.render_hook_swap(
                    hook_path, video_path, body_start, raw_out, subbed_output=sub_out,
                    base_segments=base_segments, hook_segments=hook_segments,
                    style_preset=resolved_preset, font_family=font_family or "Montserrat",
                    zoom_effect=int(zoom_effect) if zoom_effect is not None else 1,
                    framing=framing or "auto", total_dur=total_dur,
                    animation=animation, fade_ms=fade_ms,
                )
                for outp in (raw_out, sub_out):
                    render_engine.generate_thumbnail(outp)

                conn = get_db_connection()
                cursor = conn.cursor()
                # Re-running a hook replaces its previous swap for this project.
                cursor.execute("DELETE FROM hook_swaps WHERE project_id = ? AND hook_id = ?", (project_id, hook_id))
                cursor.execute("""
                    INSERT INTO hook_swaps (project_id, hook_id, hook_label, filepath_subbed, filepath_raw)
                    VALUES (?, ?, ?, ?, ?)
                """, (project_id, hook_id, hook_label, sub_out, raw_out))
                conn.commit()
                conn.close()
                done += 1
            except Exception as swap_err:
                failed += 1
                log.error("Hook swap '%s' failed for project %s: %s", hook_label, project_id, swap_err)
            progress(((idx + 1) / max(1, len(hook_rows))) * 100.0)

        if done == 0 and failed > 0:
            set_project_status(project_id, "failed",
                               error=f"All {failed} hook swaps failed — check the hook clips and FFmpeg log.")
        else:
            set_project_status(project_id, "completed", progress=100)
            log.info("Project %s: %d hook swap(s) rendered, %d failed.", project_id, done, failed)
    except Exception as e:
        log.error("Hook swap batch failed for project %s: %s", project_id, e)
        try:
            set_project_status(project_id, "failed", error=str(e))
        except Exception:
            pass
    finally:
        render_engine.set_current_project(None)


@app.post("/api/projects/{project_id}/hook-swap")
def trigger_hook_swap(project_id: int, req: HookSwapRequest, background_tasks: BackgroundTasks):
    """Queues one render per selected hook: [new hook] + [this video's body]."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status, segments_map_json, filename FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Project not found")
    status, smap_json, filename = row
    if status in ("pending", "queued", "analyzing", "rendering"):
        conn.close()
        raise HTTPException(status_code=400, detail="Project is still processing — wait for it to finish first.")
    if not smap_json:
        conn.close()
        raise HTTPException(status_code=400, detail="Project has no AI analysis yet (no hook boundary to swap at).")
    if not os.path.exists(os.path.join(database.RAW_DIR, filename)):
        conn.close()
        raise HTTPException(status_code=400, detail="Source video is no longer on disk (cleaned up or deleted).")

    if req.hook_ids:
        placeholders = ",".join("?" for _ in req.hook_ids)
        cursor.execute(f"SELECT id, filename, label, transcript_json FROM hooks WHERE id IN ({placeholders}) ORDER BY id", req.hook_ids)
    else:
        cursor.execute("SELECT id, filename, label, transcript_json FROM hooks ORDER BY id")
    hook_rows = cursor.fetchall()

    if not hook_rows:
        conn.close()
        raise HTTPException(status_code=400, detail="No hooks in the library — upload hook clips first.")

    missing = [r[1] for r in hook_rows if not os.path.exists(os.path.join(config.HOOKS_DIR, r[1]))]
    if missing:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Hook files missing on disk: {', '.join(missing)}")

    # Mark rendering synchronously so status polls never race the background task.
    cursor.execute("""
        UPDATE projects SET status = 'rendering', progress = 0, stop_requested = 0, error_message = NULL
        WHERE id = ?
    """, (project_id,))
    conn.commit()
    conn.close()

    background_tasks.add_task(bg_run_hook_swaps, project_id, hook_rows)
    return {"status": "rendering", "hooks": len(hook_rows)}


@app.get("/api/projects/{project_id}/download-hookswap/{swap_id}/{with_subs}")
def download_hook_swap(project_id: int, swap_id: int, with_subs: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filepath_subbed, filepath_raw FROM hook_swaps WHERE id = ? AND project_id = ?",
                   (swap_id, project_id))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Hook swap not found")
    filepath = row[0] if with_subs.lower() == "true" else row[1]
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Rendered file not found")
    return FileResponse(filepath, media_type="video/mp4", filename=os.path.basename(filepath))


# ---------------------------------------------------------------------------
# Fonts / music
# ---------------------------------------------------------------------------

@app.post("/api/fonts")
async def upload_custom_font(file: UploadFile = File(...)):
    filename = safe_media_filename(file.filename, config.FONT_EXTENSIONS)
    font_path = os.path.join(config.FONTS_DIR, filename)
    await save_upload_async(file, font_path)

    family_name = font_parser.parse_font_family(font_path)
    database.register_custom_font(filename, family_name)
    return {"filename": filename, "family_name": family_name}


@app.get("/api/fonts")
def list_custom_fonts():
    return database.get_custom_fonts()


@app.post("/api/projects/{project_id}/bg-music")
async def upload_bg_music(project_id: int, file: UploadFile = File(...)):
    filename = safe_media_filename(file.filename, config.AUDIO_EXTENSIONS)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Project not found")
    conn.close()

    bg_music_dir = os.path.join(database.RAW_DIR, f"project_{project_id}_music")
    os.makedirs(bg_music_dir, exist_ok=True)
    music_path = os.path.join(bg_music_dir, filename)
    await save_upload_async(file, music_path)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE projects SET bg_music_path = ? WHERE id = ?", (music_path, project_id))
    conn.commit()
    conn.close()

    return {"status": "success", "music_path": music_path}


# ---------------------------------------------------------------------------
# Settings / maintenance / health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "llm_provider": llm.active_provider(),
        "ffmpeg": render_engine.check_binaries(),
    }


@app.get("/api/settings")
def get_system_settings():
    total, used, free = shutil.disk_usage(database.BASE_DIR)
    disk_usage_pct = (used / total) * 100

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT prompt_text FROM system_prompts WHERE component = 'whisper'")
    row = cursor.fetchone()
    whisper_prompt = row[0] if row else "Transcribe the audio accurately."

    cursor.execute("SELECT COUNT(*) FROM subtitle_corrections")
    corrections_count = cursor.fetchone()[0]
    conn.close()

    provider = llm.provider_status()
    agent_notes = [
        f"AI Editorial Brain: '{provider['active']}' provider active "
        f"({'Claude API connected' if provider['anthropic_available'] else 'set ANTHROPIC_API_KEY to enable Claude'}).",
        "AI Slicing Boundary Alignment: Snapping is fully active. Snapped all video cuts to the nearest word end or silence gap.",
        "9:16 Portrait Enforcer: Locked all outputs to 1080x1920 (9:16 layout) to prevent metadata or container aspect ratio switching.",
        f"Whisper Self-Learning Loop: Tuned instructions with {corrections_count} manual editor corrections.",
        "Retention Zoom: Automatically triggered 1.15x jump cut zoom on Hook and CTA segments to maximize viewer retention.",
        "Background Ducking: Applied -15dB sidechain ducking compression on background music track during spoken audio phases.",
        "Karaoke Highlighting: Pre-configured primary text to Montserrat white, highlighting active words in Bold Yellow.",
    ]

    return {
        "vision_model": database.get_setting("vision_model", config.OLLAMA_VISION_MODEL),
        "edit_model": database.get_setting("edit_model", config.OLLAMA_EDIT_MODEL),
        "whisper_model": database.get_setting("whisper_model", config.WHISPER_MODEL_DEFAULT),
        "whisper_prompt": whisper_prompt,
        "llm_provider": database.get_setting("llm_provider", config.LLM_PROVIDER_DEFAULT),
        "anthropic_model": database.get_setting("anthropic_model", config.ANTHROPIC_MODEL),
        "openai_model": database.get_setting("openai_model", config.OPENAI_MODEL),
        "llm": provider,
        "transition_style": database.get_setting("transition_style", "fade"),
        "transition_duration": database.get_setting("transition_duration", "0.4"),
        "subtitle_animation": database.get_setting("subtitle_animation", "none"),
        "subtitle_fade_ms": database.get_setting("subtitle_fade_ms", "150"),
        "silence_removal": database.get_setting("silence_removal", "1"),
        "audio_normalize": database.get_setting("audio_normalize", "1"),
        "whisper_language": database.get_setting("whisper_language", "auto"),
        "agent_notes": agent_notes,
        "disk_usage": {
            "total_gb": round(total / (1024 ** 3), 2),
            "used_gb": round(used / (1024 ** 3), 2),
            "free_gb": round(free / (1024 ** 3), 2),
            "percent_used": round(disk_usage_pct, 1),
        },
    }


@app.post("/api/settings")
def update_system_settings(req: SettingsUpdateRequest):
    for key, val in req.settings.items():
        database.update_setting(key, val)
    return {"status": "success"}


@app.post("/api/clean-cache")
def clean_cache():
    import tempfile

    freed_bytes = 0
    cuts_dir = database.CUTS_DIR
    if os.path.exists(cuts_dir):
        for f in os.listdir(cuts_dir):
            fp = os.path.join(cuts_dir, f)
            try:
                if os.path.isfile(fp):
                    freed_bytes += os.path.getsize(fp)
                    os.remove(fp)
                elif os.path.isdir(fp):
                    for root, _dirs, files in os.walk(fp):
                        for file in files:
                            freed_bytes += os.path.getsize(os.path.join(root, file))
                    shutil.rmtree(fp)
            except Exception as e:
                log.warning("[Clean Cache] Error deleting cut file %s: %s", fp, e)

    temp_dir = tempfile.gettempdir()
    if os.path.exists(temp_dir):
        for f in os.listdir(temp_dir):
            if f.startswith("eibeleza_") or f.startswith("vinicut_"):
                fp = os.path.join(temp_dir, f)
                try:
                    if os.path.isfile(fp):
                        freed_bytes += os.path.getsize(fp)
                        os.remove(fp)
                    elif os.path.isdir(fp):
                        for root, _dirs, files in os.walk(fp):
                            for file in files:
                                freed_bytes += os.path.getsize(os.path.join(root, file))
                        shutil.rmtree(fp)
                except Exception as e:
                    log.warning("[Clean Cache] Error deleting temp file %s: %s", fp, e)

    os.makedirs(cuts_dir, exist_ok=True)
    return {"status": "success", "freed_mb": round(freed_bytes / (1024 * 1024), 2)}


# ---------------------------------------------------------------------------
# WebSocket fallback + static mounts
# ---------------------------------------------------------------------------

@app.websocket("/{path:path}")
async def catch_all_ws(websocket: WebSocket, path: str):
    await manager.connect(websocket)
    log.debug("Fallback WS connected on path: %s", path)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# Mount media bins to serve previews directly in HTML5 videos.
app.mount("/raw", StaticFiles(directory=database.RAW_DIR), name="raw")
app.mount("/cuts", StaticFiles(directory=database.CUTS_DIR), name="cuts")
app.mount("/hooks", StaticFiles(directory=config.HOOKS_DIR), name="hooks")

# Serve the web UI. Prefer the zero-build single-page UI in ./webui; fall back
# to a built React app in ./frontend/dist if present.
webui_dir = os.path.join(config.BASE_DIR, "webui")
frontend_dist = os.path.join(config.BASE_DIR, "frontend", "dist")

if os.path.exists(os.path.join(webui_dir, "index.html")):
    app.mount("/", StaticFiles(directory=webui_dir, html=True), name="static")
else:
    os.makedirs(frontend_dist, exist_ok=True)
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
