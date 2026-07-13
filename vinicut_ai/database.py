"""
SQLite schema, migrations and settings helpers for Vinicut AI.

All paths come from config.py so the storage layout can be relocated with
environment variables. The schema uses additive ALTERs guarded by a pragma
check so existing databases upgrade in place on boot.
"""
import os
import sqlite3
from datetime import datetime

import config
from config import get_logger

log = get_logger("database")

# Re-exported for modules that historically imported paths from here.
BASE_DIR = config.BASE_DIR
RAW_DIR = config.RAW_DIR
CUTS_DIR = config.CUTS_DIR
DB_DIR = config.DB_DIR
DB_PATH = config.DB_PATH
WATCH_DIR = config.WATCH_DIR
AUTO_CUTS_DIR = config.AUTO_CUTS_DIR


def get_db_connection():
    """
    Opens a SQLite connection configured for safe concurrent access.

    The watch-folder thread, the queue-worker thread and FastAPI BackgroundTasks
    all write to the same DB. WAL lets readers and a writer coexist, and the busy
    timeout makes writers wait for a lock instead of immediately raising
    "database is locked".
    """
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _existing_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _ensure_columns(cursor, table, columns):
    """Adds any missing columns. `columns` is {name: 'TYPE DEFAULT ...'}."""
    have = _existing_columns(cursor, table)
    for name, decl in columns.items():
        if name not in have:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            log.info("Migrated %s: added column %s", table, name)


def init_db():
    """Initializes the required directories and SQLite database tables."""
    for directory in [RAW_DIR, CUTS_DIR, DB_DIR, WATCH_DIR, AUTO_CUTS_DIR, config.FONTS_DIR, config.HOOKS_DIR]:
        os.makedirs(directory, exist_ok=True)

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending',
        segments_map_json TEXT,
        transcript_json TEXT,
        style_preset TEXT DEFAULT 'Bold Yellow'
    )
    """)

    _ensure_columns(cursor, "projects", {
        "segments_map_json": "TEXT",
        "transcript_json": "TEXT",
        "style_preset": "TEXT DEFAULT 'Bold Yellow'",
        "bg_music_path": "TEXT",
        "font_family": "TEXT DEFAULT 'Montserrat'",
        "zoom_effect": "INTEGER DEFAULT 1",
        "custom_preset_json": "TEXT",
        "error_message": "TEXT",
        "progress": "INTEGER DEFAULT 0",
        # Cooperative cancellation flag checked by the workers; replaces the old
        # scheme of abusing status='failed' as a stop signal.
        "stop_requested": "INTEGER DEFAULT 0",
        # 9:16 framing mode for this source: auto | crop | fit_blur | fit_black.
        "framing": "TEXT DEFAULT 'auto'",
        # AI-generated marketing pack (hooks, captions, hashtags, CTA lines).
        "marketing_json": "TEXT",
    })

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cuts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        cut_type TEXT NOT NULL,
        start_time REAL,
        end_time REAL,
        filepath TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (project_id) REFERENCES projects(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS custom_fonts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        family_name TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS subtitle_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        segment_index INTEGER,
        start_time REAL,
        end_time REAL,
        original_text TEXT,
        corrected_text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (project_id) REFERENCES projects(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        component TEXT UNIQUE NOT NULL,
        prompt_text TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS style_preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT UNIQUE NOT NULL,
        preset_name TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_montages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        name TEXT NOT NULL,
        description TEXT,
        order_json TEXT NOT NULL,
        filepath_subbed TEXT,
        filepath_raw TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (project_id) REFERENCES projects(id)
    )
    """)
    _ensure_columns(cursor, "ai_montages", {
        # AI retention/virality estimate (0-100) for this variation's structure.
        "score": "INTEGER",
    })

    # Library of replacement hook clips uploaded by creators. transcript_json
    # is filled asynchronously by Whisper so swapped videos get full captions.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS hooks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        label TEXT,
        duration REAL DEFAULT 0,
        transcript_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    _ensure_columns(cursor, "hooks", {
        # AI scroll-stopping estimate (0-100) + a short critique in the
        # hook's own language, filled after transcription.
        "score": "INTEGER",
        "score_reason": "TEXT",
    })

    # Rendered hook-swap deliverables: one row per (project, hook) render.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS hook_swaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        hook_id INTEGER,
        hook_label TEXT,
        filepath_subbed TEXT,
        filepath_raw TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (project_id) REFERENCES projects(id),
        FOREIGN KEY (hook_id) REFERENCES hooks(id)
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_hookswaps_project ON hook_swaps(project_id)")

    # Indexes: the queue worker polls by status, and detail pages join by
    # project_id constantly.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cuts_project ON cuts(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_montages_project ON ai_montages(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_corrections_project ON subtitle_corrections(project_id)")

    # Boot defaults for runtime-tunable settings. INSERT OR IGNORE keeps any
    # value the user already changed from the dashboard.
    default_settings = [
        ("vision_model", config.OLLAMA_VISION_MODEL),
        ("edit_model", config.OLLAMA_EDIT_MODEL),
        ("whisper_model", config.WHISPER_MODEL_DEFAULT),
        ("llm_provider", config.LLM_PROVIDER_DEFAULT),
        ("anthropic_model", config.ANTHROPIC_MODEL),
        ("openai_model", config.OPENAI_MODEL),
        ("transition_style", "fade"),
        ("transition_duration", "0.4"),
        ("subtitle_animation", "none"),
        ("subtitle_fade_ms", "150"),
        # High-end pipeline defaults
        ("silence_removal", "1"),
        ("silence_min_gap", "0.45"),
        ("audio_normalize", "1"),
        # All UGC footage for this brand is Portuguese; forcing the language
        # beats auto-detect on noisy audio. Editable in Settings.
        ("whisper_language", "pt"),
        # 0 = release Whisper's VRAM after each transcription so the local
        # Gemma model has room on single-GPU machines (1 on Apple Silicon,
        # where memory is unified).
        ("whisper_keep_loaded", config.WHISPER_KEEP_LOADED_DEFAULT),
        # Durations rendered for each AI variation. The full matrix
        # ("5,15,30,60") costs up to 24 encodes per project.
        ("variation_durations", "15,30"),
        # Which standard cuts get rendered per project (subset of 5,15,20,30,60).
        ("cut_durations", "5,15,30,60"),
    ]
    for key, val in default_settings:
        cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)", (key, val))

    cursor.execute("INSERT OR IGNORE INTO system_prompts (component, prompt_text) VALUES (?, ?)", (
        "whisper",
        "Transcreva o áudio em português com precisão. Atenção à pontuação, capitalização e "
        "grafia correta de termos técnicos e nomes de marca como Hidratei."
    ))

    cursor.execute("INSERT OR IGNORE INTO system_prompts (component, prompt_text) VALUES (?, ?)", (
        "vision",
        "Analyze this video to identify key structural parts: the Hook (first 1-5 seconds that "
        "grab attention), the Product Demonstration (showing the product features or in action), "
        "and the CTA (Call to Action at the end). Return a valid JSON with keys 'hook', 'demo', "
        "and 'cta', each having 'start' and 'end' numeric timestamp values in seconds. Example: "
        "{'hook': {'start': 0, 'end': 3}, 'demo': {'start': 3, 'end': 25}, 'cta': {'start': 25, 'end': 30}}"
    ))

    default_styles = [
        ("car", "Minimalist"),
        ("auto", "Minimalist"),
        ("luxury", "Minimalist"),
        ("skin", "Clean White"),
        ("cream", "Clean White"),
        ("serum", "Clean White"),
        ("wellness", "Clean White"),
        ("cosmetics", "Clean White"),
    ]
    for keyword, style in default_styles:
        cursor.execute("INSERT OR IGNORE INTO style_preferences (keyword, preset_name) VALUES (?, ?)", (keyword, style))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Settings / prompts
# ---------------------------------------------------------------------------

def get_setting(key, default_value=None):
    """Retrieves the value of a system setting."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    return default_value


def update_setting(key, value):
    """Updates or inserts a system setting."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO system_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))
    conn.commit()
    conn.close()


def get_system_prompt(component):
    """Retrieves the system prompt for a specific component."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT prompt_text FROM system_prompts WHERE component = ?", (component,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    return ""


def update_system_prompt(component, prompt_text):
    """Updates the system prompt for a component."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO system_prompts (component, prompt_text, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET prompt_text=excluded.prompt_text, updated_at=excluded.updated_at
    """, (component, prompt_text, datetime.now().isoformat()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Style preferences / fonts / corrections
# ---------------------------------------------------------------------------

def get_style_preset_for_file(filename):
    """Determines the style preset based on filename keywords."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT keyword, preset_name FROM style_preferences")
    rules = cursor.fetchall()
    conn.close()

    filename_lower = filename.lower()
    for keyword, preset in rules:
        kw = keyword.lower()
        if kw in filename_lower:
            # Avoid matching "car" when it is only part of "care"
            if kw == "car" and "care" in filename_lower:
                if filename_lower.count("car") == filename_lower.count("care"):
                    continue
            return preset
    return "Bold Yellow"


def log_subtitle_correction(project_id, segment_index, start_time, end_time, original_text, corrected_text):
    """Logs a subtitle correction to the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO subtitle_corrections (project_id, segment_index, start_time, end_time, original_text, corrected_text)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (project_id, segment_index, start_time, end_time, original_text, corrected_text))
    conn.commit()
    conn.close()


def register_custom_font(filename, family_name):
    """Registers a custom uploaded font in the DB."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO custom_fonts (filename, family_name) VALUES (?, ?)", (filename, family_name))
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # duplicate family registered before
    conn.close()


def get_custom_fonts():
    """Returns a list of all registered custom fonts."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename, family_name FROM custom_fonts ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [{"filename": r[0], "family_name": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Self-improvement loop
# ---------------------------------------------------------------------------

def run_self_improvement_loop(project_id):
    """
    Feeds the editor's manual subtitle corrections back into the Whisper system
    prompt so the same mistakes are less likely next time. Uses whichever LLM
    provider is configured (local Ollama or a cloud API) via llm.chat().
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT original_text, corrected_text FROM subtitle_corrections
        WHERE project_id = ?
    """, (project_id,))
    corrections = cursor.fetchall()
    conn.close()

    if not corrections:
        return "No corrections logged. System prompt unchanged."

    current_prompt = get_system_prompt("whisper")
    corrections_str = "\n".join(
        f"- Original: '{orig}' -> Corrected: '{corr}'" for orig, corr in corrections
    )

    prompt = f"""You are the Self-Improvement Optimizer for Vinicut AI, a professional video editing system.
Your job is to analyze the spelling and grammar corrections made by the editor, and update the transcription system prompt for Whisper to avoid these errors next time.

Here are the manual corrections the editor just made:
{corrections_str}

Here is the current system prompt for Whisper:
"{current_prompt}"

Write a revised system prompt for Whisper that incorporates instructions on how to handle these specific cases (e.g. adding specific brand name spellings, correcting common acronyms, formatting styles).
The revised prompt must still be a concise set of transcription instructions.
Return ONLY the new system prompt text. Do not include any introductory text, markdown code blocks, or explanations. Only the text of the prompt."""

    try:
        import llm
        new_prompt = llm.strip_code_fences(llm.chat(prompt).strip())
        if new_prompt:
            update_system_prompt("whisper", new_prompt)
            log.info("Whisper system prompt updated from %d corrections.", len(corrections))
            return f"System prompt successfully updated. New prompt:\n{new_prompt}"
        return "LLM returned an empty response. System prompt unchanged."
    except Exception as e:
        log.warning("Self-improvement loop failed: %s", e)
        return f"Failed to run self-improvement loop: {e}"
