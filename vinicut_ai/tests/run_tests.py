#!/usr/bin/env python3
"""
Vinicut AI self-test suite.

Run from the project root (or via `run.bat test`):

    python tests/run_tests.py

Two layers:

1. Unit tests — pure logic (AI plan validation, JSON extraction, subtitle
   timeline math, transition planning). Always run; no FFmpeg needed.

2. Render tests — generate real synthetic videos covering the shapes users
   actually upload (landscape with audio, portrait with no audio track, a 2s
   micro-clip, a square video with a hostile filename) and push them through
   the ACTUAL cutting pipeline, asserting every 5/15/30/60s cut renders and
   plays. Skipped with a notice when FFmpeg/FFprobe are unavailable.

Everything runs against a throwaway temp workspace — your real database and
media folders are never touched.
"""
import os
import sys
import json
import shutil
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Isolate ALL runtime storage in a temp workspace BEFORE importing config.
WORK = tempfile.mkdtemp(prefix="vinicut_selftest_")
for var, sub in [
    ("VINICUT_RAW_DIR", "raw"), ("VINICUT_CUTS_DIR", "cuts"),
    ("VINICUT_DB_DIR", "db"), ("VINICUT_WATCH_DIR", "watch"),
    ("VINICUT_AUTO_CUTS_DIR", "auto_cuts"), ("VINICUT_FONTS_DIR", "fonts"),
]:
    os.environ[var] = os.path.join(WORK, sub)
os.environ["VINICUT_DB_PATH"] = os.path.join(WORK, "db", "selftest.db")
os.environ.setdefault("VINICUT_LOG_LEVEL", "ERROR")

import config           # noqa: E402
config.setup_logging()
import database         # noqa: E402
import ai_editor        # noqa: E402
import llm              # noqa: E402
import render_engine    # noqa: E402
import processor        # noqa: E402

database.init_db()

PASSED, FAILED, SKIPPED = [], [], []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print(f"  ✓ {name}")
    except SkipTest as s:
        SKIPPED.append(name)
        print(f"  ~ {name} [SKIPPED: {s}]")
    except Exception:
        FAILED.append(name)
        print(f"  ✗ {name}")
        traceback.print_exc()


class SkipTest(Exception):
    pass


# ===========================================================================
# 1. Unit tests
# ===========================================================================

def t_fallback_segmentation_shapes():
    for dur in (2.5, 8.0, 30.0, 95.0):
        plan = ai_editor.get_fallback_segmentation(dur)
        total = max(3.0, dur)
        assert plan["hook"][0] == 0.0
        assert plan["cta"][1] == total
        assert plan["hook"][1] < plan["demo"][1] <= plan["cta"][0] + 1e-6 or plan["demo"][1] == plan["cta"][0]
        assert set(plan["standard_cuts"]) == {"5s", "15s", "30s", "60s"}
        for key, slices in plan["standard_cuts"].items():
            for s, e in slices:
                assert 0.0 <= s < e <= total + 1e-6, (key, s, e, total)
        assert len(plan["variations"]) == 3


def t_boundary_sanitizer_rejects_garbage():
    bad = {"hook": [0, 9999], "demo": "garbage", "cta": None}
    clean = ai_editor._sanitize_boundaries(bad, 30.0)
    assert clean["hook"][1] <= 28.0
    assert clean["cta"][1] == 30.0
    assert clean["hook"][1] == clean["demo"][0]
    assert clean["demo"][1] == clean["cta"][0]


def t_standard_cuts_sanitizer():
    data = {"standard_cuts": {
        "15s": [[0, 5], [4, 12], [900, 950]],   # overlap + out of range
        "5s": [[0, 60]],                        # way over budget -> dropped
        "30s": "not-a-list",
    }}
    clean = ai_editor._sanitize_standard_cuts(data, 60.0)
    assert "5s" not in clean          # 60s of material for a 5s cut is invalid
    assert "30s" not in clean
    combined = sum(e - s for s, e in clean.get("15s", []))
    assert 7.5 <= combined <= 19.5, combined


def t_variations_sanitizer():
    data = {"variations": [
        {"name": "Editor's Pick!", "description": "d", "order": ["hook", "Banana", "CTA", "hook"]},
        {"order": []},
        "junk",
    ]}
    clean = ai_editor._sanitize_variations(data)
    assert clean[0]["order"] == ["Hook", "CTA"]
    assert clean[0]["name"] == "Editor's Pick!"


def t_extract_json_variants():
    assert llm.extract_json('{"a": 1}') == {"a": 1}
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('Sure! {"order": ["Hook"],} done') == {"order": ["Hook"]}
    assert llm.extract_json('text [1, 2, 3] text') == [1, 2, 3]
    try:
        llm.extract_json("no json here at all")
        raise AssertionError("should have raised")
    except llm.LLMError:
        pass


def t_shift_subtitles_math():
    segments = [
        {"start": 0.0, "end": 2.0, "text": "hello",
         "words": [{"word": "hello", "start": 0.5, "end": 1.5}]},
        {"start": 10.0, "end": 12.0, "text": "world",
         "words": [{"word": "world", "start": 10.2, "end": 11.8}]},
    ]
    slices = [(9.0, 13.0, "cta"), (0.0, 3.0, "hook")]  # reordered: CTA first
    out = render_engine.shift_subtitles_for_slices(segments, slices)
    assert len(out) == 2
    # "world" lands first: source 10-12 inside slice 9-13 -> output 1.0-3.0
    assert abs(out[0]["start"] - 1.0) < 1e-6 and abs(out[0]["end"] - 3.0) < 1e-6
    # "hello" second: slice 2 starts at output offset 4.0 -> 4.0-6.0
    assert abs(out[1]["start"] - 4.0) < 1e-6 and abs(out[1]["end"] - 6.0) < 1e-6
    assert abs(out[1]["words"][0]["start"] - 4.5) < 1e-6


def t_transition_planning_clamps():
    # Two comfortable clips -> xfade allowed
    ok, d, xf = render_engine.plan_transition("fade", 0.4, [5.0, 5.0])
    assert ok and xf == "fade" and abs(d - 0.4) < 1e-9
    # A clip shorter than 2x the transition -> clamped down
    ok, d, _ = render_engine.plan_transition("fade", 2.0, [1.0, 8.0])
    assert ok and d <= 0.5
    # Absurdly short clips -> hard cut fallback
    ok, d, xf = render_engine.plan_transition("fade", 0.4, [0.15, 0.15])
    assert not ok and xf is None
    # Single clip or transition 'none' -> hard cut
    assert render_engine.plan_transition("fade", 0.4, [10.0])[0] is False
    assert render_engine.plan_transition("none", 0.4, [5.0, 5.0])[0] is False


def t_smart_cut_point_bounds():
    segs = [{"start": 0.0, "end": 4.0, "text": "x",
             "words": [{"word": "one.", "start": 0.2, "end": 1.0},
                        {"word": "two", "start": 1.4, "end": 2.2},
                        {"word": "three", "start": 3.0, "end": 3.9}]}]
    t = render_engine.get_smart_cut_point(0.0, 2.0, segs, 10.0)
    assert 0.5 <= t <= 10.0
    # No transcript at all -> just clamps to target/total
    assert render_engine.get_smart_cut_point(0.0, 5.0, [], 4.0) == 4.0


def t_ass_generation_edge_cases():
    out = os.path.join(WORK, "test.ass")
    # Segments WITHOUT word data + animation enabled must not crash.
    render_engine.generate_ass_file(
        [{"start": 0.0, "end": 1.0, "text": "sem palavras"}],
        "Bold Yellow", out, animation="pop", fade_ms=150,
        slices=[(0.0, 1.0, "hook")], use_xfade=False, trans_d=0.0,
    )
    content = open(out, encoding="utf-8").read()
    assert "sem palavras" in content and "[Events]" in content
    # Custom dict preset with active word tags.
    render_engine.generate_ass_file(
        [{"start": 0.0, "end": 1.0, "text": "hi",
          "words": [{"word": "hi", "start": 0.0, "end": 1.0}]}],
        {"fontsize": 70, "bold": -1, "border_style": 1, "outline": 4.0, "shadow": 0.0,
         "alignment": 2, "margin_v": 65, "primary": "&H00FFFFFF", "highlight": "&H0000FFFF",
         "outline_col": "&H00000000", "shadow_col": "&H00000000",
         "active_word_tags": "\\fscx120\\fscy120\\c&H0000FFFF&"},
        out,
    )
    assert "fscx120" in open(out, encoding="utf-8").read()


def t_db_settings_roundtrip():
    database.update_setting("llm_provider", "anthropic")
    assert database.get_setting("llm_provider") == "anthropic"
    database.update_setting("llm_provider", "auto")
    # Provider fallback logic: anthropic without key must degrade to ollama.
    database.update_setting("llm_provider", "anthropic")
    had = os.environ.pop("ANTHROPIC_API_KEY", None)
    config.ANTHROPIC_API_KEY = None
    try:
        assert llm.active_provider() == "ollama"
        config.ANTHROPIC_API_KEY = "k"
        assert llm.active_provider() == "anthropic"
        database.update_setting("llm_provider", "auto")
        assert llm.active_provider() == "anthropic"
        config.ANTHROPIC_API_KEY = None
        assert llm.active_provider() == "ollama"
    finally:
        config.ANTHROPIC_API_KEY = had
        if had:
            os.environ["ANTHROPIC_API_KEY"] = had
        database.update_setting("llm_provider", "auto")


# ===========================================================================
# 2. Render tests over real synthetic videos
# ===========================================================================

FIXTURES = {}


def make_fixture(name, vf_src, seconds, size, with_audio=True):
    """Creates a synthetic clip via lavfi (test pattern + optional sine tone)."""
    path = os.path.join(WORK, "fixtures", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    args = [render_engine.FFMPEG_BIN,
            "-f", "lavfi", "-i", f"{vf_src}=size={size}:rate=30:duration={seconds}"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                 "-c:a", "aac", "-shortest"]
    else:
        args += ["-an"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-y", path]
    res = render_engine.run_command(args, desc=f"fixture {name}", check=False)
    if res.returncode != 0 or not os.path.exists(path):
        raise RuntimeError(f"Could not create fixture {name}: {res.stderr[-400:]}")
    return path


def fake_transcript(total, phrase="essa é a melhor promoção da Hidratei"):
    """Builds a word-timestamped transcript covering ~the whole clip."""
    words = phrase.split()
    segs = []
    t = 0.2
    step = max(0.25, (total - 0.4) / max(1, len(words) * 3))
    group = []
    for i in range(len(words) * 3):
        w = words[i % len(words)]
        start, end = t, min(total - 0.05, t + step * 0.8)
        if end <= start:
            break
        group.append({"word": w, "start": round(start, 2), "end": round(end, 2)})
        t += step
        if len(group) == 3:
            segs.append({"start": group[0]["start"], "end": group[-1]["end"],
                         "text": " ".join(g["word"] for g in group), "words": group})
            group = []
    if group:
        segs.append({"start": group[0]["start"], "end": group[-1]["end"],
                     "text": " ".join(g["word"] for g in group), "words": group})
    return segs


def insert_project(filename):
    conn = database.get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO projects (filename, status) VALUES (?, 'rendering')", (filename,))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def require_ffmpeg():
    bins = render_engine.check_binaries()
    if not (bins.get("ffmpeg") and bins.get("ffprobe")):
        raise SkipTest("FFmpeg/FFprobe not installed on this machine")


def assert_playable(path, min_dur=0.5):
    assert os.path.exists(path), f"missing output: {path}"
    dur = render_engine.get_video_duration(path)
    assert dur >= min_dur, f"{os.path.basename(path)} too short: {dur}s"
    return dur


def run_full_pipeline(fixture_path, transcript, label):
    """Mirrors the queue worker: fallback AI plan -> render_final_cuts."""
    filename = os.path.basename(fixture_path)
    dest = os.path.join(database.RAW_DIR, filename)
    shutil.copy2(fixture_path, dest)
    pid = insert_project(filename)

    total = render_engine.get_video_duration(dest)
    assert total > 0, f"fixture unreadable: {filename}"
    plan = ai_editor.get_fallback_segmentation(total)
    segments_map = {"hook": plan["hook"], "demo": plan["demo"], "cta": plan["cta"],
                    "standard_cuts": plan["standard_cuts"]}

    cut_paths = processor.render_final_cuts(
        project_id=pid, filename=filename, original_video_path=dest,
        segments=transcript, style_preset="Bold Yellow",
        segments_map=segments_map, order=["Hook", "Demo", "CTA"],
    )

    for dur in (5, 15, 30, 60):
        raw = cut_paths.get(f"{dur}s_raw")
        sub = cut_paths.get(dur)
        assert raw and sub, f"{label}: {dur}s cut missing from results"
        d_raw = assert_playable(raw)
        d_sub = assert_playable(sub)
        # Cuts must never exceed the target by more than a word-snap margin,
        # and never exceed the source length.
        assert d_raw <= min(dur + 3.0, total + 1.0), f"{label} {dur}s raw too long: {d_raw}"
        assert abs(d_sub - d_raw) < 1.5, f"{label} {dur}s subbed/raw duration drift"
    return pid, dest, total, segments_map


def t_render_landscape_with_audio():
    require_ffmpeg()
    path = FIXTURES["landscape"]
    transcript = fake_transcript(12.0)
    run_full_pipeline(path, transcript, "landscape")


def t_render_portrait_silent():
    require_ffmpeg()
    path = FIXTURES["portrait_silent"]
    # No audio track AND empty transcript (nothing was spoken).
    run_full_pipeline(path, [], "portrait-silent")


def t_render_tiny_2s_clip():
    require_ffmpeg()
    path = FIXTURES["tiny"]
    run_full_pipeline(path, fake_transcript(2.0, "curto demais"), "tiny-2s")


def t_render_hostile_filename():
    require_ffmpeg()
    path = FIXTURES["hostile"]
    run_full_pipeline(path, fake_transcript(6.0), "hostile-name")


def t_render_ai_variation_reorder():
    require_ffmpeg()
    src = FIXTURES["landscape"]
    dest = os.path.join(database.RAW_DIR, "variation_src.mp4")
    shutil.copy2(src, dest)
    total = render_engine.get_video_duration(dest)
    transcript = fake_transcript(total)
    plan = ai_editor.get_fallback_segmentation(total)
    segments_map = {"hook": plan["hook"], "demo": plan["demo"], "cta": plan["cta"]}

    raw_out = os.path.join(database.CUTS_DIR, "variation_raw.mp4")
    _, slice_plan = render_engine.render_custom_reordered_cut(
        dest, segments_map, ["CTA", "Hook", "Demo"], transcript, "Bold Yellow",
        raw_out, target_duration=15, total_dur=total,
        transition="fade", transition_duration=0.4,
        burn=False, return_slices=True,
    )
    assert_playable(raw_out)
    assert slice_plan["slices"], "no slices in variation plan"

    shifted = render_engine.shift_subtitles_for_slices(
        transcript, slice_plan["slices"], slice_plan["use_xfade"], slice_plan["trans_d"])
    assert shifted, "reordered subtitles vanished"
    sub_out = os.path.join(database.CUTS_DIR, "variation_subbed.mp4")
    ass = sub_out + ".ass"
    render_engine.generate_ass_file(shifted, "Bold Yellow", ass,
                                    slices=slice_plan["slices"],
                                    use_xfade=slice_plan["use_xfade"],
                                    trans_d=slice_plan["trans_d"])
    render_engine.render_subtitles(raw_out, ass, sub_out)
    assert_playable(sub_out)


def t_corrupt_file_fails_cleanly():
    require_ffmpeg()
    bad = os.path.join(database.RAW_DIR, "corrupt.mp4")
    with open(bad, "wb") as f:
        f.write(b"this is not a video at all" * 100)
    assert render_engine.get_video_duration(bad) == 0.0
    # ensure_audio_stream must not blow up on garbage either.
    p, is_temp = render_engine.ensure_audio_stream(bad)
    assert p == bad and not is_temp


# ===========================================================================

def main():
    print("Vinicut AI self-test\n====================")
    print(f"workspace: {WORK}\n")

    print("[1/2] Unit tests")
    check("fallback segmentation shapes (2.5s/8s/30s/95s)", t_fallback_segmentation_shapes)
    check("boundary sanitizer rejects garbage", t_boundary_sanitizer_rejects_garbage)
    check("standard-cuts sanitizer", t_standard_cuts_sanitizer)
    check("variations sanitizer", t_variations_sanitizer)
    check("LLM JSON extraction variants", t_extract_json_variants)
    check("subtitle reorder timeline math", t_shift_subtitles_math)
    check("transition planning clamps", t_transition_planning_clamps)
    check("smart cut point bounds", t_smart_cut_point_bounds)
    check("ASS generation edge cases", t_ass_generation_edge_cases)
    check("settings + provider fallback logic", t_db_settings_roundtrip)

    print("\n[2/2] Render tests (real FFmpeg pipeline)")
    bins = render_engine.check_binaries()
    if bins.get("ffmpeg") and bins.get("ffprobe"):
        try:
            FIXTURES["landscape"] = make_fixture("landscape_1280x720.mp4", "testsrc2", 12, "1280x720")
            FIXTURES["portrait_silent"] = make_fixture("portrait_silent_720x1280.mp4", "testsrc2", 8, "720x1280", with_audio=False)
            FIXTURES["tiny"] = make_fixture("tiny_2s_640x480.mp4", "testsrc2", 2, "640x480")
            FIXTURES["hostile"] = make_fixture("Editor's cut & final (v2) ép.mp4", "testsrc2", 6, "960x960")
        except Exception as e:
            print(f"  ! fixture generation failed: {e}")
    check("landscape 16:9 with audio -> all cuts", t_render_landscape_with_audio)
    check("portrait 9:16, NO audio track, empty transcript -> all cuts", t_render_portrait_silent)
    check("2-second micro clip -> all cuts", t_render_tiny_2s_clip)
    check("filename with apostrophe/&/unicode -> all cuts", t_render_hostile_filename)
    check("AI variation reorder (CTA-Hook-Demo, xfade, subs)", t_render_ai_variation_reorder)
    check("corrupt file detected cleanly", t_corrupt_file_fails_cleanly)

    print("\n====================")
    print(f"PASSED: {len(PASSED)}   FAILED: {len(FAILED)}   SKIPPED: {len(SKIPPED)}")
    if FAILED:
        print("Failed:", ", ".join(FAILED))
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
