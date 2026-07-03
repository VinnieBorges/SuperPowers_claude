import os
import re
import subprocess
import threading

import config
from config import get_logger

log = get_logger("render_engine")

try:
    import winreg
except ImportError:  # non-Windows: module still importable for tooling/tests
    winreg = None

# ---------------------------------------------------------------------------
# Binary configuration (portability)
# ---------------------------------------------------------------------------
FFMPEG_BIN = config.FFMPEG_BIN
FFPROBE_BIN = config.FFPROBE_BIN

# ---------------------------------------------------------------------------
# Per-project process tracking
# ---------------------------------------------------------------------------
# Each worker thread declares which project it is rendering; every FFmpeg
# process it spawns is registered under that project id. Stopping a project
# then only terminates *its* processes — previously a single global handle
# meant "stop" could kill whichever project happened to be encoding.
_process_registry = {}  # project_id (or None) -> set of Popen
_registry_lock = threading.Lock()
_thread_ctx = threading.local()


def set_current_project(project_id):
    """Binds subsequent FFmpeg spawns on this thread to a project id."""
    _thread_ctx.project_id = project_id


def _register(process):
    pid = getattr(_thread_ctx, "project_id", None)
    with _registry_lock:
        _process_registry.setdefault(pid, set()).add(process)
    return pid


def _unregister(process):
    pid = getattr(_thread_ctx, "project_id", None)
    with _registry_lock:
        procs = _process_registry.get(pid)
        if procs:
            procs.discard(process)
            if not procs:
                _process_registry.pop(pid, None)


def terminate_project_processes(project_id):
    """Terminates every FFmpeg process spawned for the given project."""
    with _registry_lock:
        procs = list(_process_registry.get(project_id, ()))
    for p in procs:
        if p.poll() is None:
            try:
                p.terminate()
                log.info("Terminated FFmpeg process for project %s.", project_id)
            except Exception as e:
                log.warning("Error terminating FFmpeg for project %s: %s", project_id, e)


def terminate_active_process():
    """Legacy hard stop: terminates every tracked FFmpeg process."""
    with _registry_lock:
        procs = [p for group in _process_registry.values() for p in group]
    for p in procs:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception as e:
                log.warning("Error terminating FFmpeg: %s", e)


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def _run_process(args, progress_callback=None, duration=None):
    """
    Spawns one tracked process and waits for it. When a progress callback and a
    target duration are provided, FFmpeg's stderr is followed line-by-line and
    `time=HH:MM:SS.cs` stamps are converted into a 0-100 percentage.
    """
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=get_ffmpeg_env())
    except FileNotFoundError:
        # Binary missing: surface it like any failed run so check=False callers
        # (ffprobe checks, best-effort cuts) degrade instead of crashing.
        msg = (
            f"'{args[0]}' was not found. Install FFmpeg and make sure it is on PATH, "
            "or point VINICUT_FFMPEG / VINICUT_FFPROBE at the binaries."
        )
        log.error(msg)
        return subprocess.CompletedProcess(args, 127, "", msg)
    _register(p)
    try:
        if progress_callback and duration:
            stderr_lines = []
            while True:
                line = p.stderr.readline()
                if not line and p.poll() is not None:
                    break
                if line:
                    stderr_lines.append(line)
                    match = _TIME_RE.search(line)
                    if match:
                        hours, mins, secs = match.groups()
                        current_sec = int(hours) * 3600 + int(mins) * 60 + float(secs)
                        progress_callback(min(100, int((current_sec / duration) * 100)))
            stdout = p.stdout.read() if p.stdout else ""
            stderr = "".join(stderr_lines)
        else:
            stdout, stderr = p.communicate()
    finally:
        _unregister(p)
    return subprocess.CompletedProcess(args, p.returncode, stdout, stderr)


def run_command(args, desc="ffmpeg", check=True, progress_callback=None, duration=None):
    """
    Runs an external command as an argument list (shell=False) so that file
    paths containing spaces or shell metacharacters (%, &, (), !, ...) are
    passed literally and can never be reinterpreted by the shell.

    If the command uses h264_nvenc hardware acceleration and fails, we automatically
    fall back to CPU-based libx264 encoding to support all machine configurations.
    """
    result = _run_process(args, progress_callback, duration)

    if result.returncode != 0 and "h264_nvenc" in args:
        log.warning("NVENC failed for '%s'. Retrying with CPU libx264 fallback...", desc)
        cpu_args = list(args)
        for idx, val in enumerate(cpu_args):
            if val == "h264_nvenc":
                cpu_args[idx] = "libx264"
            elif val == "-preset" and idx + 1 < len(cpu_args):
                if cpu_args[idx + 1] in ("p1", "p2", "p3", "p4", "p5", "p6", "p7"):
                    cpu_args[idx + 1] = "medium"
        result = _run_process(cpu_args, progress_callback, duration)
        desc = f"{desc} (CPU fallback)"

    if check and result.returncode != 0:
        raise RuntimeError(
            f"{desc} failed (exit {result.returncode}).\n"
            f"stderr:\n{(result.stderr or '').strip()}"
        )
    return result


def check_binaries():
    """Reports whether the configured ffmpeg/ffprobe binaries actually run."""
    status = {}
    for name, binary in (("ffmpeg", FFMPEG_BIN), ("ffprobe", FFPROBE_BIN)):
        res = _run_process([binary, "-version"])
        status[name] = res.returncode == 0
    return status



def format_ass_time(seconds):
    """Formats seconds into ASS time format H:MM:SS.cs"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centiseconds = int(round((seconds - int(seconds)) * 100))
    if centiseconds >= 100:
        centiseconds = 99
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"

# ---------------------------------------------------------------------------
# Boundary transition support (Hook -> Demo -> CTA cross-fades)
# ---------------------------------------------------------------------------
# Friendly transition names mapped to FFmpeg `xfade` transition identifiers.
# Anything not listed here resolves to None == hard cut (plain concat).
XFADE_TRANSITIONS = {
    "none": None,
    "hard": None,
    "cut": None,
    # Cross dissolve / fade
    "fade": "fade",
    "crossfade": "fade",
    "dissolve": "dissolve",
    # Dip to black / white
    "fade_black": "fadeblack",
    "dip_to_black": "fadeblack",
    "fadeblack": "fadeblack",
    "fade_white": "fadewhite",
    "fadewhite": "fadewhite",
    # Zoom / blur
    "zoom_blur": "zoomin",
    "zoomin": "zoomin",
    "hblur": "hblur",
    # Slides / wipes
    "slide": "slideleft",
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
    "wipe": "wipeleft",
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    # Shapes
    "circleopen": "circleopen",
    "circleclose": "circleclose",
    "radial": "radial",
    "pixelize": "pixelize",
    "smoothleft": "smoothleft",
    "smoothright": "smoothright",
}

# Defaults used when the caller asks for transitions but does not specify a
# duration. Kept conservative so we never eat meaningful spoken content.
DEFAULT_TRANSITION = "fade"
DEFAULT_TRANSITION_DURATION = 0.4
MIN_TRANSITION_DURATION = 0.1


def resolve_transition(name):
    """Maps a friendly transition name to an FFmpeg xfade id, or None for a hard cut."""
    if not name:
        return None
    return XFADE_TRANSITIONS.get(str(name).strip().lower(), None)


def _clamp_transition_duration(trans_dur, clip_durations):
    """
    Clamps the transition duration so every clip is comfortably longer than the
    crossfade. xfade overlaps the two clips, so a transition longer than (half)
    the shortest clip would consume the whole segment. Returns 0.0 when no clean
    transition is possible (caller should fall back to a hard cut).
    """
    if not clip_durations:
        return 0.0
    shortest = min(clip_durations)
    # Cap at half of the shortest clip so neither side is fully consumed.
    max_allowed = max(0.0, shortest * 0.5)
    d = min(float(trans_dur), max_allowed)
    return d if d >= MIN_TRANSITION_DURATION else 0.0


def plan_transition(transition, transition_duration, clip_durations):
    """
    Decides whether a cross-fade chain can be applied to the given clips.

    Returns a tuple (use_xfade, duration, xfade_id). When use_xfade is False the
    caller should use a plain concat. This single source of truth keeps the
    video/audio assembly and the subtitle timeline in agreement.
    """
    xf = resolve_transition(transition)
    if not xf or len(clip_durations) < 2:
        return False, 0.0, None
    if transition_duration is None:
        transition_duration = DEFAULT_TRANSITION_DURATION
    d = _clamp_transition_duration(transition_duration, clip_durations)
    if d <= 0.0:
        return False, 0.0, None
    return True, d, xf


def append_xfade_chain(filter_parts, n, clip_durations, xfade_id, duration,
                       v_in="v", a_in="a", v_out="v_concat", a_out="a_concat"):
    """
    Appends an xfade (video) + acrossfade (audio) chain that joins the labelled
    streams [v0..v{n-1}] / [a0..a{n-1}] into [v_out] / [a_out].

    xfade is pairwise and overlaps the two inputs by `duration`, so the running
    merged stream shrinks by `duration` at every boundary. The offset for each
    transition is therefore the length of everything merged so far minus the
    overlap. Returns the final total duration of the merged stream.
    """
    prev_v = f"[{v_in}0]"
    prev_a = f"[{a_in}0]"
    merged_dur = clip_durations[0]

    for i in range(1, n):
        offset = merged_dur - duration
        cur_v = f"[{v_in}{i}]"
        cur_a = f"[{a_in}{i}]"
        # Last hop writes to the public output labels expected downstream.
        out_v = f"[vx{i}]" if i < n - 1 else f"[{v_out}]"
        out_a = f"[ax{i}]" if i < n - 1 else f"[{a_out}]"

        filter_parts.append(
            f"{prev_v}{cur_v}xfade=transition={xfade_id}:duration={duration:.3f}:offset={offset:.3f}{out_v}"
        )
        # Triangular crossfade curves keep perceived loudness roughly constant.
        filter_parts.append(
            f"{prev_a}{cur_a}acrossfade=d={duration:.3f}:c1=tri:c2=tri{out_a}"
        )

        prev_v = out_v
        prev_a = out_a
        merged_dur = merged_dur + clip_durations[i] - duration

    return merged_dur

STYLE_CONFIGS = {
    "TikTok Bold": {
        "fontsize": 75, "bold": -1, "border_style": 1, "outline": 5.0, "shadow": 0.0, "alignment": 2, "margin_v": 75,
        "primary": "&H00FFFFFF", "highlight": "&H0000FF00", "outline_col": "&H00000000", "shadow_col": "&H00000000"
    },
    "Cyberpunk Neon": {
        "fontsize": 72, "bold": -1, "border_style": 1, "outline": 1.5, "shadow": 4.0, "alignment": 2, "margin_v": 70,
        "primary": "&H00FF00FF", "highlight": "&H00FFFF00", "outline_col": "&H00FFFFFF", "shadow_col": "&H70FF00FF"
    },
    "Netflix Pop": {
        "fontsize": 60, "bold": -1, "border_style": 3, "outline": 0.0, "shadow": 0.0, "alignment": 2, "margin_v": 65,
        "primary": "&H00FFFFFF", "highlight": "&H0000FFFF", "outline_col": "&H00000000", "shadow_col": "&H90000000"
    },
    "Retro Yellow": {
        "fontsize": 68, "bold": -1, "border_style": 1, "outline": 3.0, "shadow": 3.0, "alignment": 2, "margin_v": 70,
        "primary": "&H0000FFFF", "highlight": "&H00FFFFFF", "outline_col": "&H00000000", "shadow_col": "&H00000000"
    },
    "Bold Yellow": {
        "fontsize": 70, "bold": -1, "border_style": 1, "outline": 4.0, "shadow": 0.0, "alignment": 2, "margin_v": 65,
        "primary": "&H00FFFFFF", "highlight": "&H0000FFFF", "outline_col": "&H00000000", "shadow_col": "&H00000000"
    },
    "Clean White": {
        "fontsize": 60, "bold": 0, "border_style": 1, "outline": 0.5, "shadow": 3.0, "alignment": 2, "margin_v": 65,
        "primary": "&H00FFFFFF", "highlight": "&H0000FFFF", "outline_col": "&H00000000", "shadow_col": "&H90000000"
    },
    "Minimalist": {
        "fontsize": 38, "bold": 0, "border_style": 1, "outline": 0.0, "shadow": 0.0, "alignment": 2, "margin_v": 35,
        "primary": "&H00FFFFFF", "highlight": "&H0000FFFF", "outline_col": "&H00000000", "shadow_col": "&H00000000"
    }
}

def get_ass_header(style_preset, font_family="Montserrat"):
    """Generates ASS header with styling presets."""
    header = """[Script Info]
Title: EiBeleza Auto cut - AI Subtitles
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
"""
    if isinstance(style_preset, dict):
        cfg = style_preset
    else:
        cfg = STYLE_CONFIGS.get(style_preset, STYLE_CONFIGS["Bold Yellow"])

    fontsize = cfg["fontsize"]
    bold = cfg["bold"]
    border_style = cfg["border_style"]
    outline = cfg["outline"]
    shadow = cfg["shadow"]
    alignment = cfg["alignment"]
    margin_v = cfg["margin_v"]
    primary = cfg["primary"]
    highlight = cfg["highlight"]
    outline_col = cfg["outline_col"]
    shadow_col = cfg["shadow_col"]

    header += f"Style: Default,{font_family},{fontsize},{primary},{highlight},{outline_col},{shadow_col},{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},{alignment},10,10,{margin_v},1\n"
    header += """
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return header

def _anim_prefix(animation, fade_ms, is_first, is_last):
    """
    Builds an ASS override block for caption entrance/exit animation.

    Captions are rendered as one Dialogue event per word (for the moving word
    highlight), so a per-line fade would flicker every word. Instead we fade IN
    only on a group's first word and OUT only on its last, and apply any scale
    "pop"/"bounce" entrance only on the first word.

    Supported: "none", "fade", "pop", "bounce". (Scale-based entrances are
    center-anchored so they don't disturb the configured alignment/margins.)
    """
    animation = (animation or "none").strip().lower()
    if animation == "none":
        return ""
    try:
        fm = max(0, int(fade_ms or 0))
    except (TypeError, ValueError):
        fm = 0

    tags = []
    fade_in = fm if is_first else 0
    fade_out = fm if is_last else 0
    if fm > 0 and (fade_in or fade_out):
        tags.append(f"\\fad({fade_in},{fade_out})")
    if is_first:
        if animation == "pop":
            tags.append("\\fscx70\\fscy70\\t(0,130,\\fscx100\\fscy100)")
        elif animation == "bounce":
            tags.append("\\fscx55\\fscy55\\t(0,90,\\fscx112\\fscy112)\\t(90,180,\\fscx100\\fscy100)")

    return "{" + "".join(tags) + "}" if tags else ""


def generate_ass_file(segments, style_preset, output_path, font_family="Montserrat", animation="none", fade_ms=0, slices=None, use_xfade=False, trans_d=0.0):
    """
    Generates an ASS subtitle file with dynamic word highlight styling and an
    optional caption entrance/exit animation (see _anim_prefix).
    """
    content = get_ass_header(style_preset, font_family=font_family)

    if isinstance(style_preset, dict):
        cfg = style_preset
    else:
        cfg = STYLE_CONFIGS.get(style_preset, STYLE_CONFIGS["Bold Yellow"])

    primary_color = cfg.get("primary", "&H00FFFFFF")
    highlight_color = cfg.get("highlight", "&H0000FFFF")

    # Check if custom formatting/animation tags exist
    active_word_tags = cfg.get("active_word_tags")
    inactive_word_tags = cfg.get("inactive_word_tags")

    if active_word_tags:
        highlight_tag = f"{{{active_word_tags}}}"
    else:
        highlight_tag = f"{{\\c{highlight_color}&}}"

    if inactive_word_tags:
        base_tag = f"{{{inactive_word_tags}}}"
    else:
        base_tag = f"{{\\c{primary_color}&}}"

    for idx, seg in enumerate(segments):
        words = seg.get("words", [])
        if words:
            # Generate dialogue entry for each word highlight span
            for w_idx, active_word in enumerate(words):
                start_t = format_ass_time(active_word["start"])
                end_t = format_ass_time(active_word["end"])

                # Rebuild text highlighting the active word
                text_parts = []
                for curr_w_idx, w in enumerate(words):
                    word_text = w["word"].strip()
                    if curr_w_idx == w_idx:
                        text_parts.append(f"{highlight_tag}{word_text}{base_tag}")
                    else:
                        text_parts.append(word_text)

                text_line = " ".join(text_parts).strip()
                anim = _anim_prefix(animation, fade_ms, w_idx == 0, w_idx == len(words) - 1)
                content += f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{anim}{text_line}\n"
        else:
            # Fallback if word-level data is missing
            start = format_ass_time(seg["start"])
            end = format_ass_time(seg["end"])
            text = seg["text"].strip().replace("\n", " ")
            anim = _anim_prefix(animation, fade_ms, True, True)
            content += f"Dialogue: 0,{start},{end},Default,,0,0,0,,{anim}{text}\n"

    # Append visual progress bar layers if slices are provided
    if slices:
        output_slices = []
        curr_time = 0.0
        for idx, (start, end, seg_type) in enumerate(slices):
            dur = end - start
            out_start = curr_time
            if use_xfade and idx < len(slices) - 1:
                out_end = curr_time + dur
                curr_time += dur - trans_d
            else:
                out_end = curr_time + dur
                curr_time += dur
            output_slices.append((out_start, out_end, seg_type))
        total_duration = curr_time

        if total_duration > 0:
            bar_y1 = 1896
            bar_y2 = 1904
            
            # 1. Draw background rectangles for Hook, Demo, CTA
            for out_start, out_end, seg_type in output_slices:
                x1 = int(round((out_start / total_duration) * 1080))
                x2 = int(round((out_end / total_duration) * 1080))
                if x2 <= x1:
                    continue
                x1 = max(0, min(x1, 1080))
                x2 = max(0, min(x2, 1080))
                
                # Colors matching the UI (hook: #2d6a73, demo: #3f4a6b, cta: #6e4555)
                # ASS colors are BGR:
                # #2d6a73 -> BGR: 736a2d
                # #3f4a6b -> BGR: 6b4a3f
                # #6e4555 -> BGR: 55456e
                color_bgr = "736A2D"
                if seg_type == "demo":
                    color_bgr = "6B4A3F"
                elif seg_type == "cta":
                    color_bgr = "55456E"
                    
                start_t = format_ass_time(0.0)
                end_t = format_ass_time(total_duration)
                
                # Draw the colored base segment
                content += f"Dialogue: 1,{start_t},{end_t},Default,,0,0,0,,{{\\p1\\c&H{color_bgr}&\\pos(0,0)}}m {x1} {bar_y1} l {x2} {bar_y1} l {x2} {bar_y2} l {x1} {bar_y2}{{\\p0}}\n"
            
            # 2. Draw a moving vertical playhead (white rectangle, width 4)
            #    and a white/highlight filling overlay trail.
            #    We do this in steps of 0.1s for high smoothness.
            step = 0.1
            t = 0.0
            while t < total_duration:
                next_t = min(t + step, total_duration)
                start_t = format_ass_time(t)
                end_t = format_ass_time(next_t)
                
                t_mid = (t + next_t) / 2.0
                x = int(round((t_mid / total_duration) * 1080))
                x = max(0, min(x, 1080))
                
                # Draw white trail from 0 to current x
                content += f"Dialogue: 1,{start_t},{end_t},Default,,0,0,0,,{{\\p1\\c&HFFFFFF&\\pos(0,0)}}m 0 {bar_y1 + 2} l {x} {bar_y1 + 2} l {x} {bar_y2 - 2} l 0 {bar_y2 - 2}{{\\p0}}\n"
                
                # Draw playhead indicator (from Y-2 to Y+2, width 4)
                px1 = max(0, x - 2)
                px2 = min(1080, x + 2)
                content += f"Dialogue: 2,{start_t},{end_t},Default,,0,0,0,,{{\\p1\\c&HFFFFFF&\\pos(0,0)}}m {px1} {bar_y1 - 2} l {px2} {bar_y1 - 2} l {px2} {bar_y2 + 2} l {px1} {bar_y2 + 2}{{\\p0}}\n"
                
                t = next_t

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return output_path

def get_ffmpeg_env():
    """
    Builds a clean environment with a refreshed PATH for FFmpeg.

    On Windows it merges the machine + user PATH from the registry (so freshly
    installed tools are visible without a reboot) and appends the WinGet links
    dir. On other platforms it returns the current environment unchanged.
    """
    env = os.environ.copy()

    # Only the Windows registry dance applies on nt; elsewhere just use PATH.
    if os.name != "nt" or winreg is None:
        return env

    winget_links = os.path.join(
        os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"),
        "Microsoft", "WinGet", "Links"
    )

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
            sys_path, _ = winreg.QueryValueEx(key, "Path")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            usr_path, _ = winreg.QueryValueEx(key, "Path")

        # Expand environment variables like %USERPROFILE% or %SystemRoot%
        sys_path = os.path.expandvars(sys_path)
        usr_path = os.path.expandvars(usr_path)

        new_path = sys_path + ";" + usr_path + ";" + winget_links

        # Update Path in all possible casings
        path_keys = [k for k in env.keys() if k.upper() == "PATH"]
        if path_keys:
            for k in path_keys:
                env[k] = new_path
        else:
            env["PATH"] = new_path
    except Exception:
        # Fallback if registry query fails: just append to existing PATH
        path_keys = [k for k in env.keys() if k.upper() == "PATH"]
        if path_keys:
            for k in path_keys:
                env[k] = env[k] + ";" + winget_links
        else:
            env["PATH"] = winget_links

    return env

def escape_ass_path(path):
    """Escapes ASS file path for the FFmpeg subtitles filter (filtergraph syntax)."""
    path = path.replace("\\", "/")
    if ":" in path:
        path = path.replace(":", "\\:")
    return path

def render_subtitles(video_path, ass_path, output_path, progress_callback=None, duration=None):
    """Burns ASS subtitles into the video using h264_nvenc hardware acceleration."""
    video_path, is_temp = ensure_audio_stream(video_path)
    temp_ass = None
    try:
        # The subtitles filter wraps its path in single quotes; a literal quote
        # inside the path (e.g. C:/Users/Vinnie's PC/...) would break parsing.
        # Burn from a quote-free temp copy in that case.
        if "'" in ass_path:
            import tempfile
            import uuid
            temp_ass = os.path.join(tempfile.gettempdir(), f"vinicut_{uuid.uuid4().hex}.ass")
            import shutil as _shutil
            _shutil.copy2(ass_path, temp_ass)
            escaped_ass = escape_ass_path(temp_ass)
        else:
            escaped_ass = escape_ass_path(ass_path)

        # Locate custom fonts folder
        fonts_dir = config.FONTS_DIR.replace("\\", "/")

        if os.path.exists(fonts_dir) and os.listdir(fonts_dir) and "'" not in fonts_dir:
            escaped_fonts = fonts_dir.replace(":", "\\:")
            vf = f"subtitles='{escaped_ass}':fontsdir='{escaped_fonts}'"
        else:
            if "'" in fonts_dir:
                log.warning("Fonts dir path contains a quote; burning without fontsdir "
                            "(custom fonts unavailable): %s", fonts_dir)
            vf = f"subtitles='{escaped_ass}'"

        args = [
            FFMPEG_BIN, "-i", video_path,
            "-vf", vf,
            "-c:v", "h264_nvenc", "-preset", "p4",
            "-c:a", "aac", "-b:a", "192k",
            "-y", output_path,
        ]
        run_command(args, desc="Subtitle burn", progress_callback=progress_callback, duration=duration)  # raises with stderr on failure
    finally:
        if is_temp:
            try:
                os.remove(video_path)
            except Exception:
                pass
        if temp_ass:
            try:
                os.remove(temp_ass)
            except Exception:
                pass
    return output_path

def get_video_duration(video_path):
    """Helper to get video duration using ffprobe."""
    args = [
        FFPROBE_BIN, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    result = run_command(args, desc="ffprobe duration", check=False)
    if result.returncode == 0:
        try:
            return float(result.stdout.strip())
        except ValueError:
            pass
    return 0.0

def has_audio_stream(video_path):
    """Helper to check if a video file contains an audio stream using ffprobe."""
    args = [
        FFPROBE_BIN, "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    result = run_command(args, desc="ffprobe audio stream check", check=False)
    if result.returncode == 0:
        return "audio" in result.stdout.lower()
    return False

def ensure_audio_stream(video_path):
    """
    Checks if the video has an audio stream. If not, creates a temporary file
    with a silent audio stream added, and returns the path to that temporary file.
    Otherwise, returns the original path.
    """
    if has_audio_stream(video_path):
        return video_path, False

    log.info("Video '%s' has no audio stream. Adding silent audio track...", video_path)
    import tempfile
    import uuid
    temp_dir = tempfile.gettempdir()
    temp_out = os.path.join(temp_dir, f"eibeleza_{uuid.uuid4().hex}.mp4")
    
    args = [
        FFMPEG_BIN,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-i", video_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        "-y", temp_out
    ]
    try:
        run_command(args, desc="Add silent audio track")
        return temp_out, True
    except Exception as e:
        log.warning("Failed to add silent audio track: %s", e)
        return video_path, False

def get_smart_cut_point(start_time, target_end_time, segments, total_duration):
    """
    Finds the closest logical word or segment boundary to target_end_time
    using the transcription segments to avoid cutting mid-word or mid-sentence.
    """
    if not segments:
        return min(target_end_time, total_duration)

    # Gather all words
    words = []
    for seg in segments:
        if "words" in seg and seg["words"]:
            words.extend(seg["words"])
        else:
            words.append({"word": seg.get("text", ""), "start": seg["start"], "end": seg["end"]})

    if not words:
        return min(target_end_time, total_duration)

    candidates = []
    for i, w in enumerate(words):
        word_text = w.get("word", "")
        has_punctuation = any(char in word_text for char in [".", ",", "!", "?", ";", "-"])

        candidates.append({
            "time": float(w["end"]),
            "type": "word_end",
            "word_index": i,
            "has_punctuation": has_punctuation
        })
        candidates.append({
            "time": float(w["start"]),
            "type": "word_start",
            "word_index": i,
            "has_punctuation": False
        })

    valid_candidates = [c for c in candidates if start_time < c["time"] <= total_duration]
    if not valid_candidates:
        return min(target_end_time, total_duration)

    best_candidate = None
    best_score = float('inf')

    for c in valid_candidates:
        dist = abs(c["time"] - target_end_time)
        score = dist

        if c["type"] == "word_end" and c["has_punctuation"]:
            score -= 0.6

        if c["type"] == "word_end":
            idx = c["word_index"]
            if idx + 1 < len(words):
                next_start = float(words[idx+1]["start"])
                gap = next_start - c["time"]
                if gap > 0.08:
                    c["time"] = c["time"] + (gap / 2.0)
                    score -= min(0.8, gap)
        elif c["type"] == "word_start":
            idx = c["word_index"]
            if idx > 0:
                prev_end = float(words[idx-1]["end"])
                gap = c["time"] - prev_end
                if gap > 0.08:
                    c["time"] = prev_end + (gap / 2.0)
                    score -= min(0.8, gap)

        if score < best_score:
            best_score = score
            best_candidate = c

    if best_candidate:
        return max(start_time + 0.5, min(best_candidate["time"], total_duration))

    return min(target_end_time, total_duration)

def shift_subtitles_for_slices(subtitle_segments, slices, use_xfade=False, trans_d=0.0):
    """
    Maps subtitles from the SOURCE timeline onto the OUTPUT timeline of a set of
    concatenated slices, so captions can be burned onto an already-cut clip
    instead of onto the full-length source.

    `slices` is a list of (start, end, type) on the source timeline, in output
    order. When use_xfade is True each boundary overlaps the next slice by
    trans_d, pulling later slices earlier (matching append_xfade_chain). Word
    timestamps are clipped to each slice and shifted too.
    """
    if not subtitle_segments or not slices:
        return []
    shifted = []
    out_offset = 0.0
    n = len(slices)
    for i, (start, end, _seg_type) in enumerate(slices):
        for seg in subtitle_segments:
            ov_start = max(start, float(seg["start"]))
            ov_end = min(end, float(seg["end"]))
            if ov_end <= ov_start:
                continue
            new_seg = {
                "start": out_offset + (ov_start - start),
                "end": out_offset + (ov_end - start),
                "text": seg["text"],
            }
            if "words" in seg and seg["words"]:
                new_words = []
                for w in seg["words"]:
                    ws = max(ov_start, float(w["start"]))
                    we = min(ov_end, float(w["end"]))
                    if we > ws:
                        new_words.append({
                            "word": w["word"],
                            "start": out_offset + (ws - start),
                            "end": out_offset + (we - start),
                        })
                new_seg["words"] = new_words
            shifted.append(new_seg)
        # Advance the output cursor; an xfade overlaps the next slice by trans_d.
        if use_xfade and i < n - 1:
            out_offset += (end - start) - trans_d
        else:
            out_offset += (end - start)
    return shifted


def make_semantic_cut(video_path, segments_map, target_duration, output_path, segments=None, zoom_effect=1, bg_music_path=None, transition=None, transition_duration=None, total_dur=None, return_slices=False, progress_callback=None):
    video_path, is_temp = ensure_audio_stream(video_path)
    try:
        return _make_semantic_cut_impl(video_path, segments_map, target_duration, output_path, segments, zoom_effect, bg_music_path, transition, transition_duration, total_dur, return_slices, progress_callback)
    finally:
        if is_temp:
            try:
                os.remove(video_path)
            except Exception:
                pass

def _make_semantic_cut_impl(video_path, segments_map, target_duration, output_path, segments=None, zoom_effect=1, bg_music_path=None, transition=None, transition_duration=None, total_dur=None, return_slices=False, progress_callback=None):
    """
    Cuts and merges segments from video_path according to semantic parts to match target_duration.
    segments_map = {
        'hook': (start, end),
        'demo': (start, end),
        'cta': (start, end)
    }

    total_dur:
        Optional pre-computed source duration. When provided we skip an ffprobe
        call (the caller usually already knows it).

    transition / transition_duration:
        When `transition` resolves to a real xfade effect (e.g. "fade",
        "fade_black", "zoom_blur") and there is more than one slice, consecutive
        segments are joined with a cross-fade (video) + acrossfade (audio)
        instead of a hard cut. Pass None / "none" to keep the original hard cut.
        Note: xfade overlaps clips, so the output is shorter than the nominal
        target by roughly (num_slices - 1) * transition_duration.
    """
    if total_dur is None:
        total_dur = get_video_duration(video_path)
    use_bg = bool(bg_music_path and os.path.exists(bg_music_path))
    if total_dur <= target_duration:
        # Video is shorter than target, just output it directly with optional bg music mixing
        if use_bg:
            filter_parts = [
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[v_scale]",
                f"[1:a]atrim=0:{total_dur},asetpts=PTS-STARTPTS[bg_raw]",
                "[bg_raw][0:a]sidechaincompress=threshold=0.15:ratio=4:attack=50:release=300,volume=0.15[bg_ducked]",
                "[0:a][bg_ducked]amix=inputs=2:duration=first:dropout_transition=2[a]"
            ]
            filter_graph = "; ".join(filter_parts)
            args = [
                FFMPEG_BIN, "-i", video_path, "-stream_loop", "-1", "-i", bg_music_path,
                "-filter_complex", filter_graph, "-map", "[v_scale]", "-map", "[a]",
                "-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "aac", "-y", output_path,
            ]
        else:
            args = [
                FFMPEG_BIN, "-i", video_path,
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
                "-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "aac", "-y", output_path,
            ]
        res = run_command(args, desc=f"Direct transcode ({target_duration}s)", check=False)
        if res.returncode != 0:
            log.error("Direct transcode failed for %s:\n%s", output_path, (res.stderr or "").strip())
        if return_slices:
            # Whole clip passes through unchanged (no cutting), so subtitles map 1:1.
            return output_path, {"slices": [(0.0, total_dur, "full")], "use_xfade": False, "trans_d": 0.0}
        return output_path

    hook = segments_map.get("hook", (0.0, min(5.0, total_dur)))
    demo = segments_map.get("demo", (hook[1], max(hook[1], total_dur - 5.0)))
    cta = segments_map.get("cta", (total_dur - 5.0, total_dur))

    slices = []
    ai_slices = None
    if isinstance(segments_map, dict) and "standard_cuts" in segments_map:
        key = f"{target_duration}s"
        if key in segments_map["standard_cuts"]:
            ai_slices = segments_map["standard_cuts"][key]

    if ai_slices:
        for idx, (start, end) in enumerate(ai_slices):
            if end <= hook[1]:
                segment_type = "hook"
            elif start >= cta[0]:
                segment_type = "cta"
            else:
                segment_type = "demo"

            smart_start = get_smart_cut_point(max(0.0, start - 0.5), start, segments, total_dur)
            smart_end = get_smart_cut_point(start, end, segments, total_dur)
            if smart_end > smart_start:
                slices.append((smart_start, smart_end, segment_type))
    else:
        # Calculate crop durations based on targets with smart endpoints (fallback)
        if target_duration == 5:
            smart_end = get_smart_cut_point(hook[0], hook[0] + 5.0, segments, total_dur)
            slices.append((hook[0], smart_end, "hook"))
        else:
            # Hook contribution: up to 5s
            hook_target_dur = min(hook[1] - hook[0], 5.0)
            smart_hook_end = get_smart_cut_point(hook[0], hook[0] + hook_target_dur, segments, total_dur)
            hook_dur = smart_hook_end - hook[0]
            if hook_dur > 0:
                slices.append((hook[0], smart_hook_end, "hook"))

            # CTA contribution: up to 5s
            cta_target_dur = min(cta[1] - cta[0], 5.0)
            target_cta_start = max(cta[0], cta[1] - cta_target_dur)
            smart_cta_start = get_smart_cut_point(cta[0], target_cta_start, segments, total_dur)
            cta_dur = cta[1] - smart_cta_start

            # Demo contribution: the rest
            demo_target_dur = target_duration - (hook_dur if hook_dur > 0 else 0) - (cta_dur if cta_dur > 0 else 0)
            demo_dur = min(demo[1] - demo[0], demo_target_dur)

            if demo_dur > 0:
                smart_demo_end = get_smart_cut_point(demo[0], demo[0] + demo_dur, segments, total_dur)
                slices.append((demo[0], smart_demo_end, "demo"))

            if cta_dur > 0:
                slices.append((smart_cta_start, cta[1], "cta"))

    # Compile per-segment trim filters
    filter_parts = []
    inputs = []
    clip_durations = []
    for idx, (start, end, segment_type) in enumerate(slices):
        clip_durations.append(end - start)
        # Apply 1.15x scale/crop zoom for Hook and CTA to enhance retention
        if zoom_effect == 1 and segment_type in ("hook", "cta"):
            filter_parts.append(
                f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
                f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                f"crop=w=iw/1.15:h=ih/1.15:x=(in_w-out_w)/2:y=(in_h-out_h)/2,scale=1080:1920,setsar=1[v{idx}]"
            )
        else:
            filter_parts.append(
                f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
                f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v{idx}]"
            )

        filter_parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{idx}]")
        inputs.append(f"[v{idx}][a{idx}]")

    # Assemble segments: cross-fade transitions when requested, else a hard concat.
    use_xfade, trans_d, xfade_id = plan_transition(transition, transition_duration, clip_durations)
    if use_xfade:
        actual_slices_duration = append_xfade_chain(
            filter_parts, len(slices), clip_durations, xfade_id, trans_d,
            v_out="v_concat", a_out="a_concat"
        )
    else:
        actual_slices_duration = sum(clip_durations)
        concat_str = "".join(inputs) + f"concat=n={len(slices)}:v=1:a=1[v_concat][a_concat]"
        filter_parts.append(concat_str)

    # Handle background audio ducking if present
    if use_bg:
        filter_parts.append(f"[1:a]atrim=0:{actual_slices_duration},asetpts=PTS-STARTPTS[bg_raw]")
        filter_parts.append(f"[bg_raw][a_concat]sidechaincompress=threshold=0.15:ratio=4:attack=50:release=300,volume=0.15[bg_ducked]")
        filter_parts.append(f"[a_concat][bg_ducked]amix=inputs=2:duration=first:dropout_transition=2[a]")

        filter_graph = "; ".join(filter_parts)
        args = [
            FFMPEG_BIN, "-i", video_path, "-stream_loop", "-1", "-i", bg_music_path,
            "-filter_complex", filter_graph, "-map", "[v_concat]", "-map", "[a]",
            "-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "aac", "-y", output_path,
        ]
    else:
        filter_graph = "; ".join(filter_parts)
        args = [
            FFMPEG_BIN, "-i", video_path,
            "-filter_complex", filter_graph, "-map", "[v_concat]", "-map", "[a_concat]",
            "-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "aac", "-y", output_path,
        ]

    result = run_command(args, desc=f"Semantic cut ({target_duration}s)", check=False, progress_callback=progress_callback, duration=target_duration)
    if result.returncode != 0:
        # Surface the real failure (previously this was silent), then fall back
        # to a head clip so the pipeline still produces *something*.
        log.error(
            "Semantic cut failed for %ss; falling back to a head clip. FFmpeg stderr:\n%s",
            target_duration, (result.stderr or "").strip()
        )
        if use_bg:
            fallback_args = [
                FFMPEG_BIN, "-ss", "0", "-i", video_path, "-stream_loop", "-1", "-i", bg_music_path,
                "-t", str(target_duration),
                "-filter_complex", "[1:a]volume=0.1[bg];[0:a][bg]amix=inputs=2:duration=first[a]",
                "-map", "0:v", "-map", "[a]",
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
                "-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "aac", "-y", output_path,
            ]
        else:
            fallback_args = [
                FFMPEG_BIN, "-ss", "0", "-i", video_path, "-t", str(target_duration),
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
                "-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "aac", "-y", output_path,
            ]
        fb = run_command(fallback_args, desc=f"Fallback head clip ({target_duration}s)", check=False)
        if fb.returncode != 0:
            log.error(
                "Fallback head clip ALSO failed for %ss. FFmpeg stderr:\n%s",
                target_duration, (fb.stderr or "").strip()
            )

    if return_slices:
        return output_path, {"slices": slices, "use_xfade": use_xfade, "trans_d": trans_d}
    return output_path

def render_custom_reordered_cut(video_path, segments_map, order, subtitle_segments, style_preset, output_path, font_family="Montserrat", zoom_effect=1, bg_music_path=None, target_duration=None, transition=None, transition_duration=None, total_dur=None, animation="none", fade_ms=0, return_slices=False, burn=True):
    video_path, is_temp = ensure_audio_stream(video_path)
    try:
        return _render_custom_reordered_cut_impl(video_path, segments_map, order, subtitle_segments, style_preset, output_path, font_family, zoom_effect, bg_music_path, target_duration, transition, transition_duration, total_dur, animation, fade_ms, return_slices, burn)
    finally:
        if is_temp:
            try:
                os.remove(video_path)
            except Exception:
                pass

def _render_custom_reordered_cut_impl(video_path, segments_map, order, subtitle_segments, style_preset, output_path, font_family="Montserrat", zoom_effect=1, bg_music_path=None, target_duration=None, transition=None, transition_duration=None, total_dur=None, animation="none", fade_ms=0, return_slices=False, burn=True):
    """
    Slices segments, concatenates them in custom order, shifts subtitles (including word timestamps), and burns them.

    transition / transition_duration:
        Optional boundary cross-fade between reordered parts (see make_semantic_cut).
        When a transition is applied, the subtitle timeline is recomputed using the
        same per-boundary overlap so burned captions stay in sync after the shorter,
        overlapped concatenation.
    """
    if total_dur is None:
        total_dur = get_video_duration(video_path)
    if total_dur <= 0:
        total_dur = 30.0
    use_bg = bool(bg_music_path and os.path.exists(bg_music_path))

    hook = segments_map.get("hook", [0.0, min(5.0, total_dur)])
    demo = segments_map.get("demo", [hook[1], max(hook[1], total_dur - 5.0)])
    cta = segments_map.get("cta", [total_dur - 5.0, total_dur])

    orig_hook_dur = max(0.0, hook[1] - hook[0])
    orig_demo_dur = max(0.0, demo[1] - demo[0])
    orig_cta_dur = max(0.0, cta[1] - cta[0])

    # Calculate budgets for Hook, Demo, CTA based on target_duration
    if target_duration is not None:
        has_hook = any(p.lower().strip() == "hook" for p in order)
        has_demo = any(p.lower().strip() == "demo" for p in order)
        has_cta = any(p.lower().strip() == "cta" for p in order)

        if target_duration <= 5.0:
            hook_target = 1.5 if has_hook else 0.0
            cta_target = 1.5 if has_cta else 0.0
            demo_target = max(0.0, target_duration - hook_target - cta_target) if has_demo else 0.0
        elif target_duration <= 15.0:
            hook_target = min(orig_hook_dur, 3.0) if has_hook else 0.0
            cta_target = min(orig_cta_dur, 3.0) if has_cta else 0.0
            demo_target = max(0.0, target_duration - hook_target - cta_target) if has_demo else 0.0
        elif target_duration <= 30.0:
            hook_target = min(orig_hook_dur, 4.0) if has_hook else 0.0
            cta_target = min(orig_cta_dur, 4.0) if has_cta else 0.0
            demo_target = max(0.0, target_duration - hook_target - cta_target) if has_demo else 0.0
        else: # >= 60.0
            hook_target = min(orig_hook_dur, 5.0) if has_hook else 0.0
            cta_target = min(orig_cta_dur, 5.0) if has_cta else 0.0
            demo_target = max(0.0, target_duration - hook_target - cta_target) if has_demo else 0.0

        hook_budget = min(orig_hook_dur, hook_target)
        cta_budget = min(orig_cta_dur, cta_target)
        demo_budget = min(orig_demo_dur, demo_target)

        sum_budgets = hook_budget + cta_budget + demo_budget
        if sum_budgets < target_duration:
            if has_demo and orig_demo_dur > demo_budget:
                demo_budget = min(orig_demo_dur, demo_budget + (target_duration - sum_budgets))
                sum_budgets = hook_budget + cta_budget + demo_budget
            if sum_budgets < target_duration and has_hook and orig_hook_dur > hook_budget:
                hook_budget = min(orig_hook_dur, hook_budget + (target_duration - sum_budgets))
                sum_budgets = hook_budget + cta_budget + demo_budget
            if sum_budgets < target_duration and has_cta and orig_cta_dur > cta_budget:
                cta_budget = min(orig_cta_dur, cta_budget + (target_duration - sum_budgets))
    else:
        hook_budget = orig_hook_dur
        demo_budget = orig_demo_dur
        cta_budget = orig_cta_dur

    # Pass 1: resolve each part's source window and capture overlapping subtitles
    # with times relative to the start of that part (independent of the final
    # output placement, which depends on the transition overlap computed below).
    slices = []
    part_subs = []

    for part in order:
        part_cleaned = part.lower().strip()
        if part_cleaned == "hook":
            part_start = hook[0]
            part_end = hook[0] + hook_budget
            if subtitle_segments:
                smart_end = get_smart_cut_point(part_start, part_end, subtitle_segments, total_dur)
                if smart_end > part_start:
                    part_end = smart_end
        elif part_cleaned == "demo":
            part_start = demo[0]
            part_end = demo[0] + demo_budget
            if subtitle_segments:
                smart_end = get_smart_cut_point(part_start, part_end, subtitle_segments, total_dur)
                if smart_end > part_start:
                    part_end = smart_end
        elif part_cleaned == "cta":
            part_end = cta[1]
            part_start = max(cta[0], cta[1] - cta_budget)
            if subtitle_segments:
                smart_start = get_smart_cut_point(cta[0], part_start, subtitle_segments, total_dur)
                if smart_start < part_end:
                    part_start = smart_start
        else:
            continue

        part_dur = part_end - part_start
        if part_dur <= 0:
            continue

        slices.append((part_start, part_end, part_cleaned))

        # Capture overlapping subtitles with times relative to this part's start.
        subs_here = []
        if subtitle_segments:
            for seg in subtitle_segments:
                overlap_start = max(part_start, float(seg["start"]))
                overlap_end = min(part_end, float(seg["end"]))

                if overlap_end > overlap_start:
                    rel_words = []
                    has_words = "words" in seg
                    if has_words:
                        for w in seg["words"]:
                            w_start = max(overlap_start, float(w["start"]))
                            w_end = min(overlap_end, float(w["end"]))
                            if w_end > w_start:
                                rel_words.append({
                                    "word": w["word"],
                                    "start": w_start - part_start,
                                    "end": w_end - part_start
                                })
                    subs_here.append({
                        "rel_start": overlap_start - part_start,
                        "rel_end": overlap_end - part_start,
                        "text": seg["text"],
                        "has_words": has_words,
                        "rel_words": rel_words
                    })
        part_subs.append(subs_here)

    if not slices:
        # Fallback to direct copy
        ass_path = output_path + ".ass"
        generate_ass_file(subtitle_segments, style_preset, ass_path, font_family=font_family, animation=animation, fade_ms=fade_ms)
        render_subtitles(video_path, ass_path, output_path)
        if return_slices:
            return output_path, {"slices": [], "use_xfade": False, "trans_d": 0.0}
        return output_path

    # Decide the transition plan from the final clip durations. This single
    # decision drives BOTH the subtitle timeline and the filter graph so they
    # never drift apart.
    clip_durations = [end - start for (start, end, _t) in slices]
    use_xfade, trans_d, xfade_id = plan_transition(transition, transition_duration, clip_durations)

    # Pass 2: place subtitles on the output timeline. Each xfade boundary pulls
    # the following part earlier by the transition duration (the overlap).
    shifted_subtitles = []
    out_offset = 0.0
    for i, (subs_here, dur) in enumerate(zip(part_subs, clip_durations)):
        for s in subs_here:
            new_seg = {
                "start": out_offset + s["rel_start"],
                "end": out_offset + s["rel_end"],
                "text": s["text"]
            }
            if s["has_words"]:
                new_seg["words"] = [
                    {
                        "word": w["word"],
                        "start": out_offset + w["start"],
                        "end": out_offset + w["end"]
                    }
                    for w in s["rel_words"]
                ]
            shifted_subtitles.append(new_seg)

        if use_xfade and i < len(clip_durations) - 1:
            out_offset += dur - trans_d
        else:
            out_offset += dur

    # Compile per-segment trim filters for reordering
    filter_parts = []
    inputs = []
    for idx, (start, end, segment_type) in enumerate(slices):
        if zoom_effect == 1 and segment_type in ("hook", "cta"):
            filter_parts.append(
                f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
                f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                f"crop=w=iw/1.15:h=ih/1.15:x=(in_w-out_w)/2:y=(in_h-out_h)/2,scale=1080:1920,setsar=1[v{idx}]"
            )
        else:
            filter_parts.append(
                f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
                f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v{idx}]"
            )

        filter_parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{idx}]")
        inputs.append(f"[v{idx}][a{idx}]")

    # Assemble segments: cross-fade transitions when requested, else a hard concat.
    if use_xfade:
        actual_slices_duration = append_xfade_chain(
            filter_parts, len(slices), clip_durations, xfade_id, trans_d,
            v_out="v_concat", a_out="a_concat"
        )
    else:
        actual_slices_duration = sum(clip_durations)
        concat_str = "".join(inputs) + f"concat=n={len(slices)}:v=1:a=1[v_concat][a_concat]"
        filter_parts.append(concat_str)

    # Render concatenated video to a temp file
    temp_dir = os.path.dirname(output_path)
    temp_video = os.path.join(temp_dir, f"temp_reorder_{os.path.basename(output_path)}")

    # Integrate sidechain background music ducking on reordered segments
    if use_bg:
        filter_parts.append(f"[1:a]atrim=0:{actual_slices_duration},asetpts=PTS-STARTPTS[bg_raw]")
        filter_parts.append(f"[bg_raw][a_concat]sidechaincompress=threshold=0.15:ratio=4:attack=50:release=300,volume=0.15[bg_ducked]")
        filter_parts.append(f"[a_concat][bg_ducked]amix=inputs=2:duration=first:dropout_transition=2[a]")

        filter_graph = "; ".join(filter_parts)
        args = [
            FFMPEG_BIN, "-i", video_path, "-stream_loop", "-1", "-i", bg_music_path,
            "-filter_complex", filter_graph, "-map", "[v_concat]", "-map", "[a]",
            "-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "aac", "-y", temp_video,
        ]
    else:
        filter_graph = "; ".join(filter_parts)
        args = [
            FFMPEG_BIN, "-i", video_path,
            "-filter_complex", filter_graph, "-map", "[v_concat]", "-map", "[a_concat]",
            "-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "aac", "-y", temp_video,
        ]

    run_command(args, desc="Reorder concat")  # raises with stderr on failure

    # Generate shifted subtitles ASS file. `burn=False` lets callers reuse this
    # to produce just the (word-snapped) raw concat + slice plan, then burn the
    # subtitled version separately onto the short clip without re-concatenating.
    if subtitle_segments and burn:
        ass_path = output_path + ".ass"
        generate_ass_file(shifted_subtitles, style_preset, ass_path, font_family=font_family, animation=animation, fade_ms=fade_ms, slices=slices, use_xfade=use_xfade, trans_d=trans_d)

        # Burn subtitles onto the reordered video
        try:
            render_subtitles(temp_video, ass_path, output_path)
        finally:
            # Clean up temp files
            if os.path.exists(temp_video):
                try:
                    os.remove(temp_video)
                except Exception:
                    pass
    else:
        # No subtitles to burn, just move temp_video to output_path
        import shutil
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        shutil.move(temp_video, output_path)

    if return_slices:
        return output_path, {"slices": slices, "use_xfade": use_xfade, "trans_d": trans_d}
    return output_path
