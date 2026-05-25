"""
Loose-prompt variant of amount_summarizer. Same API (summarize_amount).
Asks the model for "a short headline under 50 chars" with minimal structure
so we can A/B against the strict-format version.

Separate cache dir (australia/cache/amount_summary_simple/) so it doesn't
collide with the strict variant.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from requirements_extractor import API_KEY, BASE_URL, MODEL  # noqa: F401

CACHE_DIR = REPO_ROOT / "australia" / "cache" / "amount_summary_simple"

SYSTEM_PROMPT = (
    "You compress a scholarship amount blurb into a short card headline. "
    "Maximum 50 characters. Include the dollar figure if there is one. "
    "Never invent numbers. Never return an empty headline — if there's no "
    "useful info, return 'See details'."
)

SUMMARISE_TOOL = {
    "name": "record_amount_summary",
    "description": "Record the short headline. Must call exactly once. Headline must be non-empty.",
    "input_schema": {
        "type": "object",
        "required": ["amount_display"],
        "properties": {
            "amount_display": {
                "type": "string",
                "description": "Short card headline, max 50 characters, non-empty.",
            },
        },
    },
}


def _cache_key(text: str) -> str:
    h = hashlib.sha256()
    h.update(b"simple\0")
    h.update(MODEL.encode())
    h.update(b"\0")
    h.update((text or "").encode("utf-8", errors="replace"))
    return h.hexdigest()


def _read_cache(key: str) -> dict | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(key: str, value: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(value, indent=2))


def summarize_amount(source: str) -> dict | None:
    source = (source or "").strip()
    if not source:
        return None

    key = _cache_key(source)
    cached = _read_cache(key)
    if cached is not None:
        return cached

    if anthropic is None or not API_KEY:
        print("  amount_summarizer_simple: no SDK or API key", file=sys.stderr)
        return None

    client = anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL, timeout=60.0, max_retries=2)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=128,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[SUMMARISE_TOOL],
            messages=[{
                "role": "user",
                "content": (
                    f"<source>\n{source}\n</source>\n\n"
                    "Call record_amount_summary with a short headline."
                ),
            }],
        )
    except Exception as e:
        print(f"  amount_summarizer_simple: API error: {e}", file=sys.stderr)
        return None

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_amount_summary":
            value = dict(block.input)
            if (value.get("amount_display") or "").strip():
                _write_cache(key, value)
            return value

    print("  amount_summarizer_simple: model did not call tool", file=sys.stderr)
    return None
