import os
import gc
import json
import requests
import subprocess
import tempfile
import time
from database import get_system_prompt, get_db_connection, log_subtitle_correction
from render_engine import render_subtitles, make_semantic_cut, generate_ass_file, get_video_duration, render_custom_reordered_cut, get_smart_cut_point, shift_subtitles_for_slices
import render_engine
import shutil

import site

def setup_cuda_dll_paths():
    """Dynamically adds pip-installed nvidia packages to Windows DLL search path."""
    if os.name == 'nt':
        try:
            for path in site.getsitepackages():
                nvidia_path = os.path.join(path, "nvidia")
                if os.path.exists(nvidia_path):
                    for root, dirs, files in os.walk(nvidia_path):
                        if "bin" in dirs:
                            bin_path = os.path.join(root, "bin")
                            os.add_dll_directory(bin_path)
                            if bin_path not in os.environ["PATH"]:
                                os.environ["PATH"] = bin_path + os.pathsep + os.environ["PATH"]
        except Exception:
            pass

# Run DLL paths setup
setup_cuda_dll_paths()

# Try to import torch and faster_whisper
try:
    import torch
except ImportError:
    torch = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

import config
from config import get_logger

log = get_logger("processor")

BASE_DIR = config.BASE_DIR
RAW_DIR = config.RAW_DIR
CUTS_DIR = config.CUTS_DIR

def extract_keyframes(video_path, output_dir, interval=2.0):
    """Extracts keyframes from the video at regular intervals (seconds), scaled down for speed and memory efficiency."""
    os.makedirs(output_dir, exist_ok=True)
    pattern = os.path.join(output_dir, "frame_%03d.jpg")

    # We extract 1 frame every 'interval' seconds and downscale to max width
    # 384px (preserving aspect ratio). Argument-list form (shell=False) keeps
    # filenames with spaces/quotes/& intact.
    render_engine.run_command(
        [render_engine.FFMPEG_BIN, "-i", video_path,
         "-vf", f"fps=1/{interval},scale=384:-1", "-q:v", "3", "-y", pattern],
        desc="Keyframe extraction", check=False,
    )

    frames = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.endswith(".jpg")]
    frames.sort()
    return frames

def unload_ollama_model(model_name):
    """Unloads an Ollama model from GPU VRAM by setting keep_alive to 0."""
    try:
        url = f"{config.OLLAMA_HOST}/api/generate"
        payload = {"model": model_name, "keep_alive": 0}
        requests.post(url, json=payload, timeout=5)
        # Reclaim PyTorch/CUDA cache if torch is imported
        if torch is not None:
            gc.collect()
            torch.cuda.empty_cache()
        # Allow Windows WDDM driver to reclaim VRAM
        time.sleep(3.0)
    except Exception as e:
        log.warning("Failed to unload Ollama model %s: %s", model_name, e)

def run_vision_analysis(video_path, model_name="llama3.2-vision:latest"):
    """
    Extracts keyframes and prompts the Ollama Vision model to find semantic cuts.
    Evaluates keyframes individually (since llama3.2-vision on Ollama supports only 1 image).
    Returns a dict with 'hook', 'demo', and 'cta' ranges.
    """
    # Unload edit model to clear VRAM before loading Llama 3.2 Vision
    import database
    edit_model = database.get_setting("edit_model", "gemma2:9b")
    unload_ollama_model(edit_model)

    total_duration = get_video_duration(video_path)
    if total_duration <= 0.0:
        total_duration = 30.0  # Fallback

    # Choose interval based on duration to get a reasonable number of frames (approx 5-10)
    if total_duration <= 10.0:
        interval = 2.0
    elif total_duration <= 30.0:
        interval = 5.0
    else:
        interval = 10.0

    temp_dir = tempfile.mkdtemp()
    try:
        frames = extract_keyframes(video_path, temp_dir, interval=interval)
        if not frames:
            return {
                "hook": [0.0, min(5.0, total_duration)],
                "demo": [min(5.0, total_duration), min(25.0, total_duration)],
                "cta": [min(25.0, total_duration), total_duration]
            }

        import base64
        # Select up to 5 evenly spaced frames to evaluate
        step = max(1, len(frames) // 5)
        selected_frames = frames[::step][:5]

        classifications = []
        for idx, fpath in enumerate(selected_frames):
            timestamp = idx * interval * step
            # Ensure timestamp does not exceed video duration
            timestamp = min(timestamp, total_duration)

            with open(fpath, "rb") as image_file:
                b64_data = base64.b64encode(image_file.read()).decode('utf-8')

            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": "Analyze this video frame. Is it 'hook' (attention grabber), 'demo' (product test/use), or 'cta' (brand/call to action)? Respond with ONLY one word.",
                        "images": [b64_data]
                    }
                ],
                "stream": False,
                "options": {
                    "temperature": 0.1
                }
            }

            try:
                response = requests.post(f"{config.OLLAMA_HOST}/api/chat", json=payload, timeout=20)
                response.raise_for_status()
                res_data = response.json()
                label = res_data["message"]["content"].strip().lower()

                # Classify based on containing substring
                if "hook" in label:
                    classifications.append((timestamp, "hook"))
                elif "cta" in label or "call" in label:
                    classifications.append((timestamp, "cta"))
                else:
                    classifications.append((timestamp, "demo"))
            except Exception as e:
                log.warning("Failed to analyze keyframe at %ss: %s", timestamp, e)
                # Default class based on position if inference fails
                if timestamp <= total_duration * 0.2:
                    classifications.append((timestamp, "hook"))
                elif timestamp >= total_duration * 0.8:
                    classifications.append((timestamp, "cta"))
                else:
                    classifications.append((timestamp, "demo"))

        # Segment calculation based on classifications
        hook_end = min(5.0, total_duration * 0.15)
        demo_end = min(25.0, total_duration * 0.85)

        hook_times = [t for t, label in classifications if label == "hook"]
        demo_times = [t for t, label in classifications if label == "demo"]
        cta_times = [t for t, label in classifications if label == "cta"]

        if hook_times and demo_times:
            hook_end = (max(hook_times) + min(demo_times)) / 2.0
        elif hook_times and not demo_times and cta_times:
            hook_end = (max(hook_times) + min(cta_times)) / 2.0

        if demo_times and cta_times:
            demo_end = (max(demo_times) + min(cta_times)) / 2.0
        elif not demo_times and hook_times and cta_times:
            demo_end = hook_end

        # Bounds checks
        hook_end = max(1.0, min(hook_end, total_duration - 2.0))
        demo_end = max(hook_end + 1.0, min(demo_end, total_duration - 1.0))

        return {
            "hook": [0.0, hook_end],
            "demo": [hook_end, demo_end],
            "cta": [demo_end, total_duration]
        }

    except Exception as e:
        log.warning("Vision analysis failed, using fallback: %s", e)
        return {
            "hook": [0.0, min(5.0, total_duration)],
            "demo": [min(5.0, total_duration), min(25.0, total_duration)],
            "cta": [min(25.0, total_duration), total_duration]
        }
    finally:
        # Clean up temp frames
        for f in os.listdir(temp_dir):
            try:
                os.remove(os.path.join(temp_dir, f))
            except Exception:
                pass
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass

        # Unload the model to free VRAM
        unload_ollama_model(model_name)

    # Final Fallback
    return {
        "hook": [0.0, min(5.0, total_duration)],
        "demo": [min(5.0, total_duration), min(25.0, total_duration)],
        "cta": [min(25.0, total_duration), total_duration]
    }

_cached_whisper_model = None
_cached_whisper_model_name = None
# Sticks after a CUDA runtime failure (missing cublas/cudnn DLLs) so every
# following load goes straight to CPU instead of failing again per video.
_whisper_force_cpu = False
import threading
_whisper_lock = threading.Lock()

_GPU_LIB_HINT = ("Enable GPU transcription with: pip install nvidia-cublas-cu12 "
                 "nvidia-cudnn-cu12 (or re-run install.bat).")


def _is_cuda_lib_error(err):
    """CUDA/cuBLAS/cuDNN runtime library problems (e.g. 'Library
    cublas64_12.dll is not found or cannot be loaded')."""
    s = str(err).lower()
    return any(k in s for k in ("cublas", "cudnn", "cuda", "nvrtc"))


def _load_whisper_locked(model_name):
    """Loads (or returns cached) Whisper model. Caller holds _whisper_lock."""
    global _cached_whisper_model, _cached_whisper_model_name
    if _cached_whisper_model is not None and _cached_whisper_model_name == model_name:
        return _cached_whisper_model

    log.info("[Whisper] Loading model '%s' into memory...", model_name)
    if not _whisper_force_cpu:
        try:
            # int8_float16 optimizes RAM/VRAM footprint while keeping GPU acceleration
            _cached_whisper_model = WhisperModel(model_name, device="cuda", compute_type="int8_float16")
            _cached_whisper_model_name = model_name
            log.info("[Whisper] Loaded model '%s' on GPU (CUDA, int8_float16).", model_name)
            return _cached_whisper_model
        except Exception as e:
            log.warning("[Whisper] GPU initialization failed (%s). Falling back to CPU. %s", e, _GPU_LIB_HINT)

    _cached_whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
    _cached_whisper_model_name = model_name
    log.info("[Whisper] Loaded model '%s' on CPU (int8).", model_name)
    return _cached_whisper_model


def _transcribe_grouped(model, video_path, language, whisper_prompt):
    """
    Runs transcription and groups words for UGC caption styling (max 3 words /
    1.5s per group). The segments generator is consumed HERE, which is where
    CUDA runtime-library failures actually surface.
    """
    segments, _info = model.transcribe(
        video_path,
        beam_size=1,
        vad_filter=True,
        word_timestamps=True,
        language=language,
        initial_prompt=whisper_prompt
    )

    grouped_segments = []
    max_words = 3
    max_duration = 1.5

    for segment in segments:
        words = list(segment.words) if segment.words else []
        if not words:
            # Fallback if word timestamps are missing
            grouped_segments.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
                "words": [{"word": segment.text.strip(), "start": segment.start, "end": segment.end}]
            })
            continue

        current_words = []
        for word in words:
            current_words.append(word)
            dur = current_words[-1].end - current_words[0].start
            if len(current_words) >= max_words or dur >= max_duration:
                text = " ".join([w.word for w in current_words]).strip()
                grouped_segments.append({
                    "start": current_words[0].start,
                    "end": current_words[-1].end,
                    "text": text,
                    "words": [{"word": w.word.strip(), "start": w.start, "end": w.end} for w in current_words]
                })
                current_words = []

        if current_words:
            text = " ".join([w.word for w in current_words]).strip()
            grouped_segments.append({
                "start": current_words[0].start,
                "end": current_words[-1].end,
                "text": text,
                "words": [{"word": w.word.strip(), "start": w.start, "end": w.end} for w in current_words]
            })

    return grouped_segments


def run_audio_transcription(video_path):
    """
    Runs Faster-Whisper to transcribe the video with word-level timestamps.
    Returns a list of subtitle segments.
    """
    if WhisperModel is None:
        raise ImportError("faster-whisper is not installed or importable.")

    import database
    whisper_model = database.get_setting("whisper_model", "large-v3")
    whisper_prompt = get_system_prompt("whisper")

    global _cached_whisper_model, _cached_whisper_model_name, _whisper_force_cpu

    with _whisper_lock:
        model = _load_whisper_locked(whisper_model)

    # Optional forced language ("auto" lets Whisper detect it per video).
    lang = (database.get_setting("whisper_language", "auto") or "auto").strip().lower()
    language = None if lang in ("", "auto") else lang

    try:
        grouped_segments = _transcribe_grouped(model, video_path, language, whisper_prompt)
    except Exception as e:
        # The CUDA constructor can succeed while cuBLAS/cuDNN DLLs are missing;
        # the failure only appears when transcription actually runs. Rebuild on
        # CPU once and remember the decision for all future loads.
        if not _is_cuda_lib_error(e):
            raise
        log.warning("[Whisper] GPU libraries unavailable at runtime (%s). "
                    "Retrying this video on CPU. %s", e, _GPU_LIB_HINT)
        _whisper_force_cpu = True
        with _whisper_lock:
            _cached_whisper_model = None
            _cached_whisper_model_name = None
            if torch is not None:
                gc.collect()
                torch.cuda.empty_cache()
            model = _load_whisper_locked(whisper_model)
        grouped_segments = _transcribe_grouped(model, video_path, language, whisper_prompt)

    # Free Whisper's VRAM (~3 GB) so the local Gemma model has room — on a
    # single GPU the two together are the classic cause of Ollama OOM crashes.
    # Set whisper_keep_loaded=1 to trade VRAM for faster back-to-back
    # transcriptions (multi-GPU or CPU-whisper setups).
    keep_loaded = str(database.get_setting("whisper_keep_loaded", "0")).strip().lower() in ("1", "true", "yes", "on")
    del model
    if not keep_loaded:
        with _whisper_lock:
            _cached_whisper_model = None
            _cached_whisper_model_name = None
        log.info("[Whisper] Model unloaded from VRAM (freeing space for the local LLM).")
    if torch is not None:
        gc.collect()
        torch.cuda.empty_cache()

    return grouped_segments

def process_pipeline(video_path, project_id, vision_model="llama3.2-vision:latest"):
    """
    Runs the full analysis pipeline:
    1. Llama-3.2-Vision (unloads automatically)
    2. Faster-Whisper (unloads automatically)
    Returns: (segments_map, transcription_segments)
    """
    # 1. Vision Analysis
    segments_map = run_vision_analysis(video_path, model_name=vision_model)

    # 2. Audio Transcription
    transcription_segments = run_audio_transcription(video_path)

    return segments_map, transcription_segments

def sanitize_segments_map_to_word_boundaries(segments_map, segments, total_duration):
    """
    Adjusts Hook, Demo, and CTA boundaries in segments_map to align with transcription word boundaries.
    """
    if not segments:
        return segments_map

    hook_start = float(segments_map["hook"][0])
    hook_end = float(segments_map["hook"][1])
    demo_start = float(segments_map["demo"][0])
    demo_end = float(segments_map["demo"][1])
    cta_start = float(segments_map["cta"][0])
    cta_end = float(segments_map["cta"][1])

    # Adjust hook_end
    smart_hook_end = get_smart_cut_point(hook_start, hook_end, segments, total_duration)

    # Enforce contiguity
    smart_demo_end = get_smart_cut_point(smart_hook_end, demo_end, segments, total_duration)

    return {
        "hook": [hook_start, smart_hook_end],
        "demo": [smart_hook_end, smart_demo_end],
        "cta": [smart_demo_end, cta_end]
    }

def get_transition_settings():
    """
    Reads the boundary-transition configuration from the settings table so the
    effect can be tuned without code changes (and later wired to a UI control).

    Returns (transition_style, transition_duration). Defaults to a subtle 0.4s
    cross-fade. Set 'transition_style' to "none" in settings to restore hard cuts.
    """
    import database
    transition_style = database.get_setting("transition_style", "fade")
    try:
        transition_duration = float(database.get_setting("transition_duration", "0.4"))
    except (TypeError, ValueError):
        transition_duration = 0.4
    return transition_style, transition_duration

def get_subtitle_animation_settings():
    """
    Reads the caption entrance/exit animation config from the settings table.

    Returns (animation, fade_ms). animation in {none, fade, pop, bounce}; defaults
    to "none" to preserve the existing look until enabled via the UI.
    """
    import database
    animation = database.get_setting("subtitle_animation", "none")
    try:
        fade_ms = int(float(database.get_setting("subtitle_fade_ms", "150")))
    except (TypeError, ValueError):
        fade_ms = 150
    return animation, fade_ms

def render_final_cuts(project_id, filename, original_video_path, segments, style_preset, segments_map, order=None, progress_callback=None):
    """
    Generates ASS, burns subtitles, and cuts the video into 5s, 15s, 30s, 60s cuts.
    Generates both subtitled and subtitle-free (raw) versions of all cuts.
    Saves cuts records to database.
    """
    conn = get_db_connection()
    try:
        return _render_final_cuts_impl(conn, project_id, filename, original_video_path, segments, style_preset, segments_map, order, progress_callback)
    finally:
        conn.close()

def _render_final_cuts_impl(conn, project_id, filename, original_video_path, segments, style_preset, segments_map, order=None, progress_callback=None):
    cursor = conn.cursor()

    # Retrieve customized rendering configurations
    cursor.execute("SELECT font_family, zoom_effect, bg_music_path, custom_preset_json, framing FROM projects WHERE id = ?", (project_id,))
    p_row = cursor.fetchone()

    font_family = p_row[0] if (p_row and p_row[0]) else "Montserrat"
    zoom_effect = int(p_row[1]) if (p_row and p_row[1] is not None) else 1
    bg_music_path = p_row[2] if (p_row and p_row[2]) else None
    custom_preset_json = p_row[3] if (p_row and p_row[3]) else None
    framing = p_row[4] if (p_row and p_row[4]) else "auto"

    # Boundary transition style (cross-fade between Hook/Demo/CTA segments)
    transition_style, transition_duration = get_transition_settings()
    # Caption entrance/exit animation (none / fade / pop / bounce)
    animation, fade_ms = get_subtitle_animation_settings()

    if custom_preset_json:
        try:
            import json
            style_preset = json.loads(custom_preset_json)
        except Exception:
            pass

    # 1. Clear any existing cuts for this project to allow re-rendering
    cursor.execute("DELETE FROM cuts WHERE project_id = ?", (project_id,))
    conn.commit()

    # Sanitize segments map using AI-driven word boundaries
    total_dur = get_video_duration(original_video_path)
    segments_map = sanitize_segments_map_to_word_boundaries(segments_map, segments, total_dur)

    # NOTE: we no longer burn subtitles into the full-length source (a wasteful
    # encode of the entire video). Each cut is produced raw first, then captions
    # are shifted onto that short cut's timeline and burned on top of it.

    durations = [5, 15, 30, 60]
    cut_paths = {}
    
    total_steps = len(durations) * 2
    
    def get_step_cb(step_idx):
        if not progress_callback: return None
        base_pct = (step_idx / total_steps) * 100.0
        step_weight = 100.0 / total_steps
        def cb(pct):
            progress_callback(base_pct + (pct / 100.0) * step_weight)
        return cb

    seg_dict = {
        "hook": (segments_map["hook"][0], segments_map["hook"][1]),
        "demo": (segments_map["demo"][0], segments_map["demo"][1]),
        "cta": (segments_map["cta"][0], segments_map["cta"][1])
    }

    # Auto cuts output directory (daily subfolder)
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")
    auto_cuts_dir = os.path.join(config.AUTO_CUTS_DIR, f"edits_{date_str}")
    os.makedirs(auto_cuts_dir, exist_ok=True)
    base_name = os.path.splitext(filename)[0]

    failed_durations = []
    for idx, dur in enumerate(durations):
        # Each duration is rendered independently: one failing target (e.g. a
        # filter-graph edge case at 5s) must not abort the 15/30/60s cuts.
        try:
            # 1. Raw cut (no subtitles). return_slices reports exactly which source
            #    ranges + transition plan were used so captions can be placed on the
            #    cut's own (shorter, possibly xfade-overlapped) output timeline.
            cut_filename_raw = f"project_{project_id}_cut_{dur}s_raw.mp4"
            cut_path_raw = os.path.join(CUTS_DIR, cut_filename_raw)
            _, slice_info = render_engine.make_semantic_cut(
                original_video_path, seg_dict, dur, cut_path_raw,
                segments=segments, zoom_effect=zoom_effect, bg_music_path=bg_music_path,
                transition=transition_style, transition_duration=transition_duration,
                total_dur=total_dur, return_slices=True, progress_callback=get_step_cb(idx * 2),
                framing=framing
            )
            if not os.path.exists(cut_path_raw):
                raise RuntimeError(f"FFmpeg produced no output for the {dur}s cut.")
            cut_paths[f"{dur}s_raw"] = cut_path_raw
            cursor.execute("""
                INSERT INTO cuts (project_id, cut_type, start_time, end_time, filepath)
                VALUES (?, ?, ?, ?, ?)
            """, (project_id, f"{dur}s_raw", 0.0, float(dur), cut_path_raw))
            # Commit per write: an open transaction here would hold SQLite's
            # write lock for the whole multi-minute render and starve uploads
            # ("database is locked" 500s from the API).
            conn.commit()

            # 2. Subtitled cut: burn the shifted captions onto the SHORT raw cut
            #    (replaces the old whole-source burn).
            cut_filename = f"project_{project_id}_cut_{dur}s.mp4"
            cut_path = os.path.join(CUTS_DIR, cut_filename)
            shifted_subs = shift_subtitles_for_slices(
                segments, slice_info["slices"], slice_info["use_xfade"], slice_info["trans_d"]
            )
            if shifted_subs:
                cut_ass_path = os.path.join(CUTS_DIR, f"project_{project_id}_cut_{dur}s.ass")
                generate_ass_file(shifted_subs, style_preset, cut_ass_path, font_family=font_family, animation=animation, fade_ms=fade_ms, slices=slice_info["slices"], use_xfade=slice_info["use_xfade"], trans_d=slice_info["trans_d"])
                try:
                    cut_path = render_engine.render_subtitles(
                        cut_path_raw, cut_ass_path, cut_path,
                        progress_callback=get_step_cb(idx * 2 + 1),
                        duration=get_video_duration(cut_path_raw)
                    )
                except Exception as burn_err:
                    log.warning("Subtitle burn failed for %ss cut, using raw as fallback: %s", dur, burn_err)
                    shutil.copy2(cut_path_raw, cut_path)
            else:
                # No captions overlap this cut; subbed == raw.
                shutil.copy2(cut_path_raw, cut_path)
            cut_paths[dur] = cut_path
            cursor.execute("""
                INSERT INTO cuts (project_id, cut_type, start_time, end_time, filepath)
                VALUES (?, ?, ?, ?, ?)
            """, (project_id, f"{dur}s", 0.0, float(dur), cut_path))
            conn.commit()

            # Poster thumbnails so the dashboard previews load instantly.
            render_engine.generate_thumbnail(cut_path_raw)
            render_engine.generate_thumbnail(cut_path)

            # Auto-copy raw cuts to watch outputs
            dest_name = f"{base_name}_{dur}s_raw.mp4"
            dest_path = os.path.join(auto_cuts_dir, dest_name)
            try:
                shutil.copy2(cut_path_raw, dest_path)
            except Exception as cp_err:
                log.warning("Error copying raw cut to auto_cuts: %s", cp_err)
        except Exception as dur_err:
            failed_durations.append(dur)
            log.error("Rendering the %ss cut failed; continuing with remaining durations: %s", dur, dur_err)

    if len(failed_durations) == len(durations):
        # Nothing rendered at all — this is a real failure the user must see.
        raise RuntimeError(
            f"All standard cuts failed to render (durations {failed_durations}). "
            "Check that the source video is readable and FFmpeg is installed."
        )

    # Render custom reordered cut if requested
    if order:
        # 1. Subtitled Custom Cut
        custom_filename = f"project_{project_id}_custom_reordered.mp4"
        custom_path = os.path.join(CUTS_DIR, custom_filename)
        try:
            render_custom_reordered_cut(
                original_video_path,
                segments_map,
                order,
                segments,
                style_preset,
                custom_path,
                font_family=font_family,
                zoom_effect=zoom_effect,
                bg_music_path=bg_music_path,
                transition=transition_style,
                transition_duration=transition_duration,
                animation=animation,
                fade_ms=fade_ms,
                framing=framing
            )
            cut_paths["custom"] = custom_path

            cursor.execute("""
                INSERT INTO cuts (project_id, cut_type, start_time, end_time, filepath)
                VALUES (?, ?, ?, ?, ?)
            """, (project_id, "custom", 0.0, get_video_duration(custom_path), custom_path))
            conn.commit()
            render_engine.generate_thumbnail(custom_path)
        except Exception as e:
            log.error("Error rendering custom reordered cut: %s", e)

        # 2. Raw Custom Cut (No Subtitles)
        custom_filename_raw = f"project_{project_id}_custom_reordered_raw.mp4"
        custom_path_raw = os.path.join(CUTS_DIR, custom_filename_raw)
        try:
            render_custom_reordered_cut(
                original_video_path,
                segments_map,
                order,
                None,
                style_preset,
                custom_path_raw,
                font_family=font_family,
                zoom_effect=zoom_effect,
                bg_music_path=bg_music_path,
                transition=transition_style,
                transition_duration=transition_duration,
                animation=animation,
                fade_ms=fade_ms,
                framing=framing
            )
            cut_paths["custom_raw"] = custom_path_raw

            cursor.execute("""
                INSERT INTO cuts (project_id, cut_type, start_time, end_time, filepath)
                VALUES (?, ?, ?, ?, ?)
            """, (project_id, "custom_raw", 0.0, get_video_duration(custom_path_raw), custom_path_raw))
            conn.commit()
            render_engine.generate_thumbnail(custom_path_raw)

            # Auto-copy custom raw cut
            dest_custom_name = f"{base_name}_custom_reordered_raw.mp4"
            dest_custom_path = os.path.join(auto_cuts_dir, dest_custom_name)
            try:
                shutil.copy2(custom_path_raw, dest_custom_path)
            except Exception as cp_err:
                log.warning("Error copying custom raw cut to auto_cuts: %s", cp_err)
        except Exception as e:
            log.error("Error rendering custom reordered raw cut: %s", e)

    # Clean up DB connection
    conn.commit()

    return cut_paths
