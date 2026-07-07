"""
AI editorial brain for Vinicut AI.

Takes the Whisper transcript and decides, like a human short-form editor would:

  1. Where the Hook / Demo / CTA boundaries are (semantic, not positional).
  2. Which exact source ranges make the best 5s / 15s / 30s / 60s cuts
     ("standard_cuts") — the strongest lines, not just the first N seconds.
  3. A set of creative re-ordered montage variations to A/B test.

All model output is validated and clamped before it reaches FFmpeg; if the LLM
is unreachable or returns garbage, get_fallback_segmentation() produces a sane
deterministic plan so the pipeline never stalls.
"""
import llm
from config import get_logger

log = get_logger("ai_editor")

STANDARD_DURATIONS = (5, 15, 30, 60)

DEFAULT_VARIATIONS = [
    {"name": "Classic Story", "description": "Hook grabs attention, demo builds trust, CTA closes.",
     "order": ["Hook", "Demo", "CTA"]},
    {"name": "Curiosity Loop", "description": "Open with the CTA promise to spark curiosity, then pay it off.",
     "order": ["CTA", "Hook", "Demo"]},
    {"name": "Proof First", "description": "Lead with the product in action for instant credibility.",
     "order": ["Demo", "Hook", "CTA"]},
]

_SYSTEM_PROMPT = (
    "You are a senior short-form video editor for direct-response UGC ads "
    "(TikTok / Reels / Shorts). You cut for retention: strong opening hooks, "
    "tight pacing, no dead air, and endings that convert. You always reply "
    "with strict JSON and never invent timestamps outside the video."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_transcript(transcription, max_chars=6000):
    """
    Renders the transcript as timestamped lines the model can reason over,
    annotating silent gaps > 0.6s — pauses are where cuts belong, and telling
    the model where they are measurably improves its boundary choices.
    """
    lines = []
    prev_end = None
    for seg in transcription or []:
        try:
            start, end = float(seg["start"]), float(seg["end"])
            if prev_end is not None and start - prev_end > 0.6:
                lines.append(f"[silence {start - prev_end:.1f}s]")
            lines.append(f"[{start:.2f}-{end:.2f}] {str(seg.get('text', '')).strip()}")
            prev_end = end
        except (KeyError, TypeError, ValueError):
            continue
    text = "\n".join(lines)
    if len(text) > max_chars:
        # Keep the head and tail; the middle is summarized by omission. Hooks
        # and CTAs live at the edges, which is what boundary detection needs.
        head = text[: max_chars // 2]
        tail = text[-max_chars // 2:]
        text = head + "\n... [transcript truncated] ...\n" + tail
    return text


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _coerce_range(value, total_dur):
    """Accepts [s, e] or {'start': s, 'end': e}; returns a clamped [s, e] or None."""
    if isinstance(value, dict):
        start, end = _num(value.get("start"), -1), _num(value.get("end"), -1)
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        start, end = _num(value[0], -1), _num(value[1], -1)
    else:
        return None
    if start < 0 or end < 0:
        return None
    start = _clamp(start, 0.0, total_dur)
    end = _clamp(end, 0.0, total_dur)
    if end - start < 0.2:
        return None
    return [start, end]


def _sanitize_boundaries(data, total_dur):
    """Validates hook/demo/cta and enforces contiguous, ordered coverage."""
    fallback = get_fallback_segmentation(total_dur)
    hook = _coerce_range(data.get("hook"), total_dur) or fallback["hook"]
    demo = _coerce_range(data.get("demo"), total_dur) or fallback["demo"]
    cta = _coerce_range(data.get("cta"), total_dur) or fallback["cta"]

    hook_end = _clamp(hook[1], 1.0, max(1.0, total_dur - 2.0))
    demo_end = _clamp(demo[1], hook_end + 0.5, max(hook_end + 0.5, total_dur - 0.5))
    return {
        "hook": [0.0, hook_end],
        "demo": [hook_end, demo_end],
        "cta": [demo_end, total_dur],
    }


def _sanitize_standard_cuts(data, total_dur):
    """
    Validates the per-duration slice lists. Each entry must be a list of
    non-overlapping, chronological [start, end] source ranges whose combined
    length is close to the target (0.5x–1.3x tolerance; the renderer word-snaps
    and trims afterwards).
    """
    result = {}
    raw = data.get("standard_cuts") if isinstance(data.get("standard_cuts"), dict) else {}
    for dur in STANDARD_DURATIONS:
        key = f"{dur}s"
        slices = []
        for item in raw.get(key, []) or []:
            rng = _coerce_range(item, total_dur)
            if rng:
                slices.append(rng)
        slices.sort(key=lambda r: r[0])
        # Drop overlaps, keeping earlier slices.
        cleaned = []
        for rng in slices:
            if cleaned and rng[0] < cleaned[-1][1]:
                rng[0] = cleaned[-1][1]
                if rng[1] - rng[0] < 0.2:
                    continue
            cleaned.append(rng)
        combined = sum(e - s for s, e in cleaned)
        if cleaned and (0.5 * dur) <= combined <= (1.3 * dur):
            result[key] = cleaned
        # else: omit — the renderer falls back to its own hook/demo/cta budget plan.
    return result


def _sanitize_variations(data):
    valid_parts = {"hook": "Hook", "demo": "Demo", "cta": "CTA"}
    variations = []
    for var in data.get("variations", []) or []:
        if not isinstance(var, dict):
            continue
        order = []
        for part in var.get("order", []) or []:
            canonical = valid_parts.get(str(part).strip().lower())
            if canonical and canonical not in order:
                order.append(canonical)
        if not order:
            continue
        name = str(var.get("name", "")).strip() or f"Variation {len(variations) + 1}"
        score = var.get("score")
        try:
            score = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            score = None
        variations.append({
            "name": name[:60],
            "description": str(var.get("description", "")).strip()[:200],
            "order": order,
            "score": score,
        })
        if len(variations) >= 4:
            break
    return variations or [dict(v) for v in DEFAULT_VARIATIONS]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_fallback_segmentation(total_dur):
    """
    Deterministic segmentation used when AI analysis is unavailable: hook is
    the first ~15% (max 5s), CTA the last ~15% (max 5s), demo the middle.
    Standard cuts pull from the strongest structural positions.
    """
    total_dur = max(3.0, _num(total_dur, 30.0))
    hook_end = min(5.0, max(1.5, total_dur * 0.15))
    cta_start = max(hook_end + 0.5, total_dur - min(5.0, max(1.5, total_dur * 0.15)))

    standard_cuts = {}
    for dur in STANDARD_DURATIONS:
        if total_dur <= dur:
            standard_cuts[f"{dur}s"] = [[0.0, total_dur]]
            continue
        if dur == 5:
            standard_cuts["5s"] = [[0.0, min(5.0, hook_end + 2.0)]]
            continue
        hook_take = min(hook_end, 5.0)
        cta_take = min(total_dur - cta_start, 5.0)
        demo_take = max(0.0, dur - hook_take - cta_take)
        demo_take = min(demo_take, cta_start - hook_end)
        slices = [[0.0, hook_take]]
        if demo_take > 0.3:
            slices.append([hook_end, hook_end + demo_take])
        if cta_take > 0.3:
            slices.append([cta_start, total_dur])
        standard_cuts[f"{dur}s"] = slices

    return {
        "hook": [0.0, hook_end],
        "demo": [hook_end, cta_start],
        "cta": [cta_start, total_dur],
        "standard_cuts": standard_cuts,
        "variations": [dict(v) for v in DEFAULT_VARIATIONS],
    }


def analyze_transcript_and_segment(transcription, total_dur):
    """
    Runs the full editorial analysis over the transcript. Returns:

        {
          "hook": [start, end],
          "demo": [start, end],
          "cta":  [start, end],
          "standard_cuts": {"5s": [[s,e],...], "15s": ..., "30s": ..., "60s": ...},
          "variations": [{"name", "description", "order"}, ...]
        }

    Raises nothing on model failure — callers get the deterministic fallback.
    """
    total_dur = max(3.0, _num(total_dur, 30.0))
    transcript_text = _fmt_transcript(transcription)
    if not transcript_text.strip():
        log.info("Empty transcript; using fallback segmentation.")
        return get_fallback_segmentation(total_dur)

    # Local models (Gemma via Ollama — the primary engine) do far better on
    # three small, focused questions than on one giant nested-JSON request.
    if llm.active_provider() == "ollama":
        return _analyze_staged_local(transcription, total_dur)

    prompt = f"""Analyze this UGC ad transcript. The video is {total_dur:.2f} seconds long.

TRANSCRIPT (each line is [start-end] text, in seconds):
{transcript_text}

Tasks:
1. Find the semantic boundaries of the three parts:
   - hook: the attention-grabbing opening claim/problem
   - demo: the product demonstration / proof / benefits
   - cta: the closing call to action
   The three parts must be contiguous and cover 0 to {total_dur:.2f}.
2. For each target duration (5s, 15s, 30s, 60s), pick the EXACT source time
   ranges that make the most persuasive cut of that length. Prefer complete
   sentences, keep chronological order, and make combined range lengths land
   close to the target. Always include some hook material first and CTA
   material last (except 5s, which is hook-only).
3. Propose 3 creative montage variations that re-order the parts (using only
   "Hook", "Demo", "CTA") with a short marketing rationale for each, plus an
   honest retention/virality score from 0-100 (how likely this structure is to
   hold viewers and convert, given THIS script).

Write every name, description and rationale in the SAME LANGUAGE as the
transcript (e.g. Portuguese for a Portuguese script).

Return ONLY this JSON shape (numbers in seconds):
{{
  "hook": [0.0, 4.2],
  "demo": [4.2, 21.7],
  "cta": [21.7, {total_dur:.2f}],
  "standard_cuts": {{
    "5s": [[0.0, 4.8]],
    "15s": [[0.0, 4.2], [8.5, 15.1], [21.7, {total_dur:.2f}]],
    "30s": [[0.0, 4.2], [4.2, 20.0], [21.7, {total_dur:.2f}]],
    "60s": [[0.0, {total_dur:.2f}]]
  }},
  "variations": [
    {{"name": "Curiosity Loop", "description": "Why it works.", "order": ["CTA", "Hook", "Demo"], "score": 78}}
  ]
}}"""

    try:
        data = llm.chat_json(prompt, system=_SYSTEM_PROMPT)
    except Exception as e:
        log.warning("AI segmentation failed (%s); using fallback plan.", e)
        return get_fallback_segmentation(total_dur)

    if not isinstance(data, dict):
        log.warning("AI segmentation returned non-object JSON; using fallback plan.")
        return get_fallback_segmentation(total_dur)

    boundaries = _sanitize_boundaries(data, total_dur)
    result = {
        **boundaries,
        "standard_cuts": _sanitize_standard_cuts(data, total_dur),
        "variations": _sanitize_variations(data),
    }
    log.info(
        "AI plan: hook=%.1f-%.1f demo=%.1f-%.1f cta=%.1f-%.1f, %d ai standard cuts, %d variations",
        *boundaries["hook"], *boundaries["demo"], *boundaries["cta"],
        len(result["standard_cuts"]), len(result["variations"]),
    )
    return result


def _analyze_staged_local(transcription, total_dur):
    """
    Gemma-friendly analysis: three small calls instead of one giant one, each
    with an independent fallback. A 12B answering one narrow question with a
    flat JSON shape is dramatically more reliable than the combined request,
    and a single bad stage no longer throws away the whole plan.
    """
    fallback = get_fallback_segmentation(total_dur)
    # Smaller transcript budget: fits comfortably in the local context window
    # with room for the answer, and keeps generation fast.
    transcript_text = _fmt_transcript(transcription, max_chars=3500)

    # Stage 1 — semantic boundaries (the decision everything else builds on).
    boundaries = {k: fallback[k] for k in ("hook", "demo", "cta")}
    try:
        data = llm.chat_json(
            f"""This UGC ad transcript is {total_dur:.2f} seconds long:

{transcript_text}

Find the semantic boundaries of the three parts (contiguous, covering 0 to {total_dur:.2f}):
- hook: the attention-grabbing opening
- demo: the product demonstration / benefits
- cta: the closing call to action

Return ONLY JSON: {{"hook": [0.0, 4.2], "demo": [4.2, 21.7], "cta": [21.7, {total_dur:.2f}]}}""",
            system=_SYSTEM_PROMPT,
        )
        boundaries = _sanitize_boundaries(data if isinstance(data, dict) else {}, total_dur)
    except Exception as e:
        log.warning("Local analysis stage 1 (boundaries) failed (%s); using fallback boundaries.", e)

    # Stage 2 — best source ranges per duration. Optional: when it fails, the
    # renderer's own budget planner takes over per duration.
    standard_cuts = {}
    try:
        data = llm.chat_json(
            f"""This UGC ad transcript is {total_dur:.2f} seconds long. The hook is
{boundaries['hook'][0]:.1f}-{boundaries['hook'][1]:.1f}s, demo {boundaries['demo'][0]:.1f}-{boundaries['demo'][1]:.1f}s, cta {boundaries['cta'][0]:.1f}-{boundaries['cta'][1]:.1f}s.

{transcript_text}

For each target duration, pick the EXACT source time ranges (chronological,
complete sentences, combined length close to the target; hook material first
and CTA material last, except 5s which is hook-only).

Return ONLY JSON like:
{{"5s": [[0.0, 4.8]], "15s": [[0.0, 4.2], [8.5, 15.1]], "30s": [[0.0, 4.2], [4.2, 20.0], [21.7, {total_dur:.2f}]], "60s": [[0.0, {total_dur:.2f}]]}}""",
            system=_SYSTEM_PROMPT,
        )
        standard_cuts = _sanitize_standard_cuts({"standard_cuts": data if isinstance(data, dict) else {}}, total_dur)
    except Exception as e:
        log.warning("Local analysis stage 2 (standard cuts) failed (%s); renderer will use its own plan.", e)

    # Stage 3 — creative variations with retention scores.
    variations = [dict(v) for v in DEFAULT_VARIATIONS]
    try:
        data = llm.chat_json(
            f"""This is the transcript of a UGC product ad:

{transcript_text}

Propose 3 montage variations re-ordering the parts (use only "Hook", "Demo",
"CTA"), each with a short marketing rationale and an honest 0-100 retention
score for THIS script. Write names and rationales in the transcript's language.

Return ONLY JSON:
{{"variations": [{{"name": "...", "description": "...", "order": ["CTA", "Hook", "Demo"], "score": 78}}]}}""",
            system=_SYSTEM_PROMPT, temperature=0.4,
        )
        variations = _sanitize_variations(data if isinstance(data, dict) else {})
    except Exception as e:
        log.warning("Local analysis stage 3 (variations) failed (%s); using default variations.", e)

    result = {**boundaries, "standard_cuts": standard_cuts, "variations": variations}
    log.info(
        "Local AI plan (staged): hook=%.1f-%.1f demo=%.1f-%.1f cta=%.1f-%.1f, %d ai cuts, %d variations",
        *boundaries["hook"], *boundaries["demo"], *boundaries["cta"],
        len(standard_cuts), len(variations),
    )
    return result


def _sanitize_hook_score(data):
    """Validates the hook-score payload; returns {'score', 'reason'} or None."""
    if not isinstance(data, dict):
        return None
    try:
        score = max(0, min(100, int(data.get("score"))))
    except (TypeError, ValueError):
        return None
    reason = str(data.get("reason", "")).strip()[:300]
    return {"score": score, "reason": reason}


def score_hook(transcription):
    """
    Rates one replacement hook clip's scroll-stopping power (0-100) with a
    short critique written in the hook's own language. Returns
    {"score": int, "reason": str} or None when the model is unavailable.
    """
    text = " ".join(str(seg.get("text", "")).strip() for seg in (transcription or [])).strip()
    if not text:
        return None

    prompt = f"""Rate this HOOK — the opening line(s) of a short-form UGC beauty ad — for
scroll-stopping power on TikTok/Reels/Shorts.

HOOK TRANSCRIPT:
"{text}"

Judge like a performance creative strategist: pattern interrupt, curiosity gap,
specificity, emotional trigger, clarity in the first second, and whether it
forces the viewer to stay for the payoff. Be honest and use the full 0-100
range — a generic greeting deserves a low score.

Return ONLY JSON, with the reason written in the SAME LANGUAGE as the hook
(e.g. Portuguese for a Portuguese hook):
{{"score": 74, "reason": "1-2 frases explicando a nota."}}"""
    try:
        data = llm.chat_json(prompt, system=_SYSTEM_PROMPT, temperature=0.3)
    except Exception as e:
        log.warning("Hook scoring failed: %s", e)
        return None
    return _sanitize_hook_score(data)


def generate_marketing_pack(transcription):
    """
    Generates ready-to-post marketing copy for the project: alternative hooks,
    platform captions, hashtags and CTA lines — in the transcript's language.
    Returns a sanitized dict, or None when the model is unavailable.
    """
    transcript_text = _fmt_transcript(transcription, max_chars=4000)
    if not transcript_text.strip():
        return None

    prompt = f"""This is the transcript of a short-form UGC product ad:

{transcript_text}

Write a marketing pack IN THE SAME LANGUAGE as the transcript. Return ONLY JSON:
{{
  "hooks": ["3-5 alternative opening hook lines, each under 12 words"],
  "captions": {{
    "tiktok": "post caption tuned for TikTok, with a strong first line",
    "instagram": "post caption tuned for Instagram Reels"
  }},
  "hashtags": ["8-12 relevant hashtags without the # sign"],
  "cta_lines": ["2-3 closing call-to-action lines"]
}}"""
    try:
        data = llm.chat_json(prompt, system=_SYSTEM_PROMPT, temperature=0.6)
    except Exception as e:
        log.warning("Marketing pack generation failed: %s", e)
        return None
    if not isinstance(data, dict):
        return None

    def _strs(value, limit, maxlen=220):
        out = []
        for item in (value or []) if isinstance(value, list) else []:
            s = str(item).strip().lstrip("#")
            if s:
                out.append(s[:maxlen])
            if len(out) >= limit:
                break
        return out

    captions = data.get("captions") if isinstance(data.get("captions"), dict) else {}
    pack = {
        "hooks": _strs(data.get("hooks"), 5),
        "captions": {
            "tiktok": str(captions.get("tiktok", "")).strip()[:500],
            "instagram": str(captions.get("instagram", "")).strip()[:500],
        },
        "hashtags": _strs(data.get("hashtags"), 12, maxlen=40),
        "cta_lines": _strs(data.get("cta_lines"), 3),
    }
    if not (pack["hooks"] or pack["hashtags"] or pack["captions"]["tiktok"]):
        return None
    return pack
