"""
Pluggable LLM client for Vinicut AI.

One `chat()` entry point that routes to the configured provider:

  - "ollama"     local models via the Ollama HTTP API (the original setup)
  - "anthropic"  Claude models via the Anthropic Messages API (cloud)
  - "openai"     any OpenAI-compatible endpoint (OpenAI, Groq, Together,
                 OpenRouter, vLLM, LM Studio, ...)

The active provider is the `llm_provider` row in system_settings (editable from
the dashboard), defaulting to config.LLM_PROVIDER_DEFAULT. API keys are read
from the environment only — they are never stored in the database.

`chat_json()` adds strict JSON extraction with retries, which every AI feature
in the pipeline (segmentation, montage planning, subtitle presets) relies on.
"""
import json
import re
import time

import requests

import config
import database
from config import get_logger

log = get_logger("llm")


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

def active_provider():
    """
    Returns the provider name to use.

    "auto" prefers the Claude API whenever ANTHROPIC_API_KEY is present and
    falls back to local Ollama otherwise. An explicitly selected cloud provider
    downgrades gracefully to Ollama when its API key is missing, so the
    pipeline never hard-fails on configuration.
    """
    provider = (database.get_setting("llm_provider", config.LLM_PROVIDER_DEFAULT) or "auto").strip().lower()
    if provider == "auto":
        return "anthropic" if config.ANTHROPIC_API_KEY else "ollama"
    if provider == "anthropic" and not config.ANTHROPIC_API_KEY:
        log.warning("llm_provider=anthropic but ANTHROPIC_API_KEY is not set; falling back to ollama.")
        return "ollama"
    if provider == "openai" and not config.OPENAI_API_KEY:
        log.warning("llm_provider=openai but OPENAI_API_KEY is not set; falling back to ollama.")
        return "ollama"
    if provider not in ("ollama", "anthropic", "openai"):
        log.warning("Unknown llm_provider '%s'; falling back to ollama.", provider)
        return "ollama"
    return provider


def provider_status():
    """Summary for the dashboard: what's configured and what's usable."""
    return {
        "active": active_provider(),
        "configured": database.get_setting("llm_provider", config.LLM_PROVIDER_DEFAULT),
        "ollama_host": config.OLLAMA_HOST,
        "anthropic_available": bool(config.ANTHROPIC_API_KEY),
        "openai_available": bool(config.OPENAI_API_KEY),
        "models": {
            "ollama": database.get_setting("edit_model", config.OLLAMA_EDIT_MODEL),
            "anthropic": database.get_setting("anthropic_model", config.ANTHROPIC_MODEL),
            "openai": database.get_setting("openai_model", config.OPENAI_MODEL),
        },
    }


def test_connection(provider=None):
    """
    Sends a tiny round-trip prompt to verify the provider is reachable and the
    key/model are valid. Returns {"ok", "provider", "model", "reply"|"error"}.
    """
    provider = (provider or active_provider()).strip().lower()
    if provider == "auto":
        provider = "anthropic" if config.ANTHROPIC_API_KEY else "ollama"
    model = provider_status()["models"].get(provider)
    try:
        backend = _BACKENDS[provider]
        reply = backend("Reply with exactly: OK", None, 0.0, model)
        return {"ok": True, "provider": provider, "model": model, "reply": (reply or "").strip()[:80]}
    except Exception as e:
        return {"ok": False, "provider": provider, "model": model, "error": str(e)}


# ---------------------------------------------------------------------------
# Provider backends
# ---------------------------------------------------------------------------

def _chat_ollama(prompt, system, temperature, model):
    model = model or database.get_setting("edit_model", config.OLLAMA_EDIT_MODEL)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = requests.post(
        f"{config.OLLAMA_HOST}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            # Keep the model resident between the pipeline's back-to-back calls
            # (segmentation -> marketing pack -> hook scores) so only the first
            # call pays the VRAM load cost.
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {"temperature": temperature, "num_ctx": config.OLLAMA_NUM_CTX},
        },
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def list_ollama_models():
    """
    Returns the models actually installed in the local Ollama
    ([{name, size_gb}], sorted smallest first) or [] when unreachable.
    """
    try:
        resp = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=4)
        resp.raise_for_status()
        models = []
        for m in resp.json().get("models", []) or []:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            size = m.get("size") or 0
            models.append({"name": name, "size_gb": round(size / 1e9, 1) if size else None})
        models.sort(key=lambda x: (x["size_gb"] is None, x["size_gb"] or 0, x["name"]))
        return models
    except Exception as e:
        log.debug("Could not list Ollama models: %s", e)
        return []


def _chat_anthropic(prompt, system, temperature, model):
    model = model or database.get_setting("anthropic_model", config.ANTHROPIC_MODEL)
    payload = {
        "model": model,
        "max_tokens": 4096,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    resp = requests.post(
        f"{config.ANTHROPIC_BASE_URL}/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    blocks = resp.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _chat_openai(prompt, system, temperature, model):
    model = model or database.get_setting("openai_model", config.OPENAI_MODEL)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = requests.post(
        f"{config.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "content-type": "application/json",
        },
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


_BACKENDS = {
    "ollama": _chat_ollama,
    "anthropic": _chat_anthropic,
    "openai": _chat_openai,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chat(prompt, system=None, temperature=0.2, model=None, provider=None):
    """
    Sends one prompt to the active provider and returns the assistant text.
    Raises LLMError after exhausting retries so callers can apply their own
    fallbacks (every pipeline stage has a deterministic one).
    """
    provider = provider or active_provider()
    backend = _BACKENDS[provider]
    last_err = None
    for attempt in range(1 + config.LLM_MAX_RETRIES):
        try:
            return backend(prompt, system, temperature, model)
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            log.warning("LLM call failed via %s (attempt %d): %s. Retrying in %ss...",
                        provider, attempt + 1, e, wait)
            time.sleep(wait)

    msg = f"LLM chat failed via {provider}: {last_err}"
    if provider == "ollama" and "timed out" in str(last_err).lower():
        msg += (
            " — the local model looks too heavy/slow for this machine. "
            "In Settings > System pick a smaller Ollama model (a 12B is a good "
            "speed/quality balance), or raise VINICUT_LLM_TIMEOUT in .env."
        )
    raise LLMError(msg)


def strip_code_fences(text):
    """Removes surrounding ``` fences that models sometimes add despite instructions."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def extract_json(text):
    """
    Best-effort extraction of the first JSON object/array embedded in `text`.
    Handles fenced blocks and leading/trailing prose.
    """
    text = strip_code_fences(text)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    # Fall back to the outermost {...} or [...] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start:end + 1]
            try:
                return json.loads(candidate)
            except ValueError:
                # Some models emit trailing commas; strip and retry once.
                cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
                try:
                    return json.loads(cleaned)
                except ValueError:
                    continue
    raise LLMError(f"Could not extract JSON from model output: {text[:300]!r}")


def chat_json(prompt, system=None, temperature=0.1, model=None, provider=None):
    """
    chat() + JSON extraction, re-asking once with an explicit correction prompt
    if the first response isn't parseable.
    """
    raw = chat(prompt, system=system, temperature=temperature, model=model, provider=provider)
    try:
        return extract_json(raw)
    except LLMError:
        log.warning("Model returned non-JSON output; re-asking once for strict JSON.")
        retry_prompt = (
            f"{prompt}\n\nYour previous reply was not valid JSON. "
            "Reply again with ONLY the JSON object, no prose, no markdown fences."
        )
        raw = chat(retry_prompt, system=system, temperature=0.0, model=model, provider=provider)
        return extract_json(raw)
