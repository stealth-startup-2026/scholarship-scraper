"""
LLM-backed extraction of structured "Application Requirements" from
scholarship page text.

We use the Anthropic Python SDK pointed at DeepSeek's Anthropic-compatible
endpoint (https://api.deepseek.com/anthropic). The same code works against
Anthropic's first-party API by just changing the env vars — the wire format
is identical.

Why a separate module: keeps the API dependency isolated, makes the scraper
work offline (no key -> graceful fallback to empty extraction), and lets
future uni scrapers reuse the same extractor unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]


def _load_dotenv() -> None:
    """Load .env from the scraper directory into os.environ if not already set.
    Uses python-dotenv when available, falls back to a minimal parser so the
    scraper works without the extra install."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

CACHE_DIR = Path(__file__).parent / "australia" / "cache" / "extraction"

# DeepSeek's Anthropic-compatible base URL. Override via env to point at the
# first-party Anthropic API or a different DeepSeek environment.
BASE_URL = os.environ.get("EXTRACTOR_BASE_URL", "https://api.deepseek.com/anthropic")
API_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("EXTRACTOR_MODEL", "deepseek-chat")

SYSTEM_PROMPT = (
    "You extract structured application-requirement data from scholarship page text. "
    "Return only what the page explicitly states. Do not infer requirements that are "
    "not written. If a piece of information is missing, omit it rather than guessing."
)

RECORD_TOOL = {
    "name": "record_requirements",
    "description": (
        "Record the application requirements for a scholarship in a fixed shape. "
        "Always call this tool exactly once."
    ),
    "input_schema": {
        "type": "object",
        "required": ["application_mode", "separate_application_required", "must_meet"],
        "properties": {
            "application_mode": {
                "type": "string",
                "enum": ["manual", "automatic"],
                "description": (
                    "'automatic' if eligible students are considered without a separate "
                    "application (often phrased 'no separate application required' or "
                    "'considered automatically'). 'manual' otherwise."
                ),
            },
            "separate_application_required": {
                "type": "boolean",
                "description": (
                    "True iff the student must submit a separate application beyond the "
                    "standard course/program application."
                ),
            },
            "must_meet": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Eligibility bullets the applicant must satisfy. Each item is a "
                    "single self-contained requirement, paraphrased concisely. Drop "
                    "headings like 'To be eligible, applicants must:' — keep only the "
                    "actual requirements."
                ),
            },
        },
    },
}


SPLIT_TOOL = {
    "name": "record_sub_awards",
    "description": (
        "Record every distinct sub-award listed on a bundled scholarship page. "
        "If the page is an aggregator that only links to scholarships hosted "
        "elsewhere (without enumerating their codes/amounts/eligibility on the "
        "UNSW page itself), return an empty sub_awards array."
    ),
    "input_schema": {
        "type": "object",
        "required": ["application_mode", "separate_application_required", "sub_awards"],
        "properties": {
            "application_mode": {
                "type": "string",
                "enum": ["manual", "automatic"],
                "description": (
                    "Same definition as in record_requirements — applies to the parent "
                    "bundle page (usually shared across all sub-awards)."
                ),
            },
            "separate_application_required": {"type": "boolean"},
            "sub_awards": {
                "type": "array",
                "description": (
                    "One entry per distinct sub-award visible on the page. Each entry "
                    "must come from explicit text — do not invent awards. Return an "
                    "empty array if the page is an aggregator with no enumerable "
                    "sub-awards."
                ),
                "items": {
                    "type": "object",
                    "required": ["title", "must_meet"],
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "The sub-award's full name as printed on the page.",
                        },
                        "code": {
                            "type": "string",
                            "description": (
                                "The sub-award's code if visible, e.g. PGCA1110. Omit "
                                "if the page doesn't print one for this sub-award."
                            ),
                        },
                        "amount_display": {
                            "type": "string",
                            "description": (
                                "Headline amount as printed, e.g. '$5,000 for 1 year'. "
                                "Omit if the page doesn't quote one for this sub-award."
                            ),
                        },
                        "amount_value": {
                            "type": "number",
                            "description": (
                                "Numeric value of the amount in AUD. Omit if not "
                                "stated. For 'up to $X' use X."
                            ),
                        },
                        "must_meet": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Bullets specific to THIS sub-award, paraphrased "
                                "concisely. Include shared eligibility from the parent "
                                "page only if it applies. Omit if not enumerable."
                            ),
                        },
                    },
                },
            },
        },
    },
}


def _cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    h.update(MODEL.encode())
    for p in parts:
        h.update(b"\0")
        h.update((p or "").encode("utf-8", errors="replace"))
    return h.hexdigest()


def _cache_path(kind: str, key: str) -> Path:
    return CACHE_DIR / kind / f"{key}.json"


def _read_cache(kind: str, key: str) -> dict | None:
    path = _cache_path(kind, key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(kind: str, key: str, value: dict) -> None:
    path = _cache_path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def _build_user_message(eligibility: str, application: str) -> str:
    parts = []
    if eligibility:
        parts.append(f"<eligibility>\n{eligibility.strip()}\n</eligibility>")
    if application:
        parts.append(f"<application>\n{application.strip()}\n</application>")
    parts.append(
        "Call record_requirements with the structured requirements. "
        "Include only requirements explicitly stated in the text above."
    )
    return "\n\n".join(parts)


def extract_requirements(eligibility: str, application: str = "") -> dict | None:
    """
    Return a dict shaped like the scholarship detail page's renderer expects:
      {"application_mode": str, "separate_application_required": bool, "must_meet": [str, ...]}
    Returns None when the API key is missing or extraction fails — the caller
    should fall back to leaving application_requirements empty.
    """
    eligibility = eligibility or ""
    application = application or ""
    if not eligibility and not application:
        return None

    key = _cache_key(eligibility, application)
    cached = _read_cache("extract", key)
    if cached is not None:
        return cached

    if anthropic is None:
        print("  extractor: anthropic SDK not installed; skipping LLM call",
              file=sys.stderr)
        return None
    if not API_KEY:
        print("  extractor: no DEEPSEEK_API_KEY/ANTHROPIC_API_KEY set; skipping LLM call",
              file=sys.stderr)
        return None

    client = anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL, timeout=60.0, max_retries=2)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[RECORD_TOOL],
            # We don't pin tool_choice to a specific tool — deepseek-reasoner
            # rejects that form. With only one tool defined, the model uses it
            # reliably when prompted to. {"type": "any"} would also work but is
            # likewise unsupported on reasoner; relying on the prompt is safest.
            messages=[{"role": "user", "content": _build_user_message(eligibility, application)}],
        )
    except Exception as e:
        print(f"  extractor: API error: {e}", file=sys.stderr)
        return None

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_requirements":
            value = dict(block.input)
            _write_cache("extract", key, value)
            return value

    print("  extractor: model did not call record_requirements", file=sys.stderr)
    return None


SPLIT_SYSTEM_PROMPT = (
    "You parse bundled scholarship pages into structured sub-award entries. "
    "Each sub-award is a distinct funded award listed on the same parent page "
    "with its own name (and usually its own code / amount / eligibility). "
    "Return only what the page explicitly states. Do not invent sub-awards or "
    "infer codes that aren't printed. If the page is an aggregator that only "
    "links to scholarships hosted elsewhere — no codes/amounts/eligibility "
    "enumerated on this page itself — return an empty sub_awards array."
)


def _build_split_message(title: str, outline: str, eligibility: str, selection: str) -> str:
    sections = []
    if title:
        sections.append(f"<page_title>\n{title.strip()}\n</page_title>")
    if outline:
        sections.append(f"<outline>\n{outline.strip()}\n</outline>")
    if eligibility:
        sections.append(f"<eligibility>\n{eligibility.strip()}\n</eligibility>")
    if selection:
        sections.append(f"<selection>\n{selection.strip()}\n</selection>")
    sections.append(
        "Call record_sub_awards with one entry per distinct sub-award visible on "
        "this page. Use empty sub_awards if no sub-awards are enumerated."
    )
    return "\n\n".join(sections)


def split_bundle(
    title: str,
    outline: str,
    eligibility: str = "",
    selection: str = "",
) -> dict | None:
    """
    Return {"application_mode", "separate_application_required", "sub_awards": [...]}
    for a bundled scholarship page, or None if extraction failed entirely.

    `sub_awards` is a list of dicts shaped like:
        {"title": str, "code"?: str, "amount_display"?: str,
         "amount_value"?: float, "must_meet": [str, ...]}

    An empty `sub_awards` list is a valid result — it means the page is an
    aggregator (links out only) and the caller should fall back to skipping it.
    """
    title = title or ""
    outline = outline or ""
    eligibility = eligibility or ""
    selection = selection or ""
    if not (outline or eligibility or selection):
        return None

    key = _cache_key("split", title, outline, eligibility, selection)
    cached = _read_cache("split", key)
    if cached is not None:
        return cached

    if anthropic is None or not API_KEY:
        print("  splitter: no anthropic SDK or API key; skipping split call",
              file=sys.stderr)
        return None

    client = anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL, timeout=60.0, max_retries=2)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": SPLIT_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[SPLIT_TOOL],
            messages=[{
                "role": "user",
                "content": _build_split_message(title, outline, eligibility, selection),
            }],
        )
    except Exception as e:
        print(f"  splitter: API error: {e}", file=sys.stderr)
        return None

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_sub_awards":
            value = dict(block.input)
            value.setdefault("sub_awards", [])
            _write_cache("split", key, value)
            return value

    print("  splitter: model did not call record_sub_awards", file=sys.stderr)
    return None


VERIFY_TOOL = {
    "name": "verify_row",
    "description": (
        "Verify whether a scholarship row is a single, well-differentiated award. "
        "Set is_single_award=false only when the row clearly contains multiple "
        "distinct awards mashed together. Use revised_must_meet sparingly — only "
        "when the current must_meet is identical to a sibling row's and the "
        "titles imply differentiation (e.g. (Refugee) vs (Indigenous))."
    ),
    "input_schema": {
        "type": "object",
        "required": ["is_single_award", "issues"],
        "properties": {
            "is_single_award": {
                "type": "boolean",
                "description": (
                    "True if this row represents one award (even if the eligibility "
                    "is imperfectly written). False only when the row's title or "
                    "must_meet refers to multiple distinct awards that should be "
                    "separate DB rows."
                ),
            },
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short notes on any problems found, max ~5 items.",
            },
            "revised_must_meet": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Only set when is_single_award=true and the eligibility needs "
                    "fixing: must_meet is identical to a sibling but the row titles "
                    "imply a differentiator (e.g. one says (Refugee), another says "
                    "(Indigenous)). Provide a revised list that includes the title "
                    "qualifier as an explicit bullet. Don't invent eligibility not "
                    "implied by the title or the source text. Omit if no change."
                ),
            },
        },
    },
}


VERIFY_SYSTEM_PROMPT = (
    "You QA scholarship rows extracted from bundle pages. You receive one row "
    "and its sibling rows from the same parent page. Decide whether the row is "
    "a single, well-differentiated award. Be conservative: most rows will be "
    "fine. Only suggest a revised must_meet when (a) the current must_meet is "
    "identical to a sibling's AND (b) the row titles imply differentiation. "
    "Never invent eligibility not stated or directly implied by the inputs."
)


def _build_verify_message(row: dict, siblings: list[dict]) -> str:
    def _row_block(r: dict, label: str) -> str:
        try:
            ar = json.loads(r.get("application_requirements") or "{}")
        except json.JSONDecodeError:
            ar = {}
        mm = ar.get("must_meet", [])
        return (
            f"<{label}>\n"
            f"title: {r.get('title','')}\n"
            f"code: {r.get('external_id','')}\n"
            f"amount: {r.get('amount_display','') or '(unknown)'}\n"
            f"must_meet: {json.dumps(mm)}\n"
            f"parent_title: {ar.get('parent_title','')}\n"
            f"</{label}>"
        )

    parts = [_row_block(row, "row")]
    for i, sib in enumerate(siblings[:8]):  # cap to keep prompt small
        parts.append(_row_block(sib, f"sibling_{i+1}"))
    parts.append("Call verify_row with your decision.")
    return "\n\n".join(parts)


def verify_row(row: dict, siblings: list[dict]) -> dict | None:
    """
    Ask the LLM to QA a single row against its siblings from the same parent
    page. Returns {is_single_award, issues, revised_must_meet?} or None on
    failure. Cached by SHA-256 of (row external_id, must_meet, sibling
    external_ids+must_meets) so repeated runs are free.
    """
    try:
        ar = json.loads(row.get("application_requirements") or "{}")
    except json.JSONDecodeError:
        ar = {}
    mm = ar.get("must_meet", [])

    cache_parts = [
        row.get("external_id", ""),
        row.get("title", ""),
        json.dumps(mm, sort_keys=True),
    ]
    for sib in sorted(siblings, key=lambda r: r.get("external_id", "")):
        try:
            sib_ar = json.loads(sib.get("application_requirements") or "{}")
        except json.JSONDecodeError:
            sib_ar = {}
        cache_parts.extend([
            sib.get("external_id", ""),
            sib.get("title", ""),
            json.dumps(sib_ar.get("must_meet", []), sort_keys=True),
        ])
    key = _cache_key(*cache_parts)
    cached = _read_cache("verify", key)
    if cached is not None:
        return cached

    if anthropic is None or not API_KEY:
        print("  verifier: no SDK or API key; skipping", file=sys.stderr)
        return None

    client = anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL, timeout=60.0, max_retries=2)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": VERIFY_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[VERIFY_TOOL],
            messages=[{"role": "user", "content": _build_verify_message(row, siblings)}],
        )
    except Exception as e:
        print(f"  verifier: API error: {e}", file=sys.stderr)
        return None

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "verify_row":
            value = dict(block.input)
            _write_cache("verify", key, value)
            return value

    print("  verifier: model did not call verify_row", file=sys.stderr)
    return None


CLASSIFY_TOOL = {
    "name": "classify_page",
    "description": (
        "Classify a scholarship page as single-scholarship or bundled. Bundled "
        "means the page describes 2+ distinct funded awards (each with its own "
        "name and usually its own code/amount/eligibility). Single means one "
        "scholarship, even if it has tiered amounts (e.g. principal + stipend) "
        "or multiple recipients per year. Aggregator pages that only link out "
        "to scholarships hosted elsewhere are single (there's nothing to "
        "split on this page)."
    ),
    "input_schema": {
        "type": "object",
        "required": ["is_bundle", "reason"],
        "properties": {
            "is_bundle": {
                "type": "boolean",
                "description": "True if the page enumerates 2+ distinct awards.",
            },
            "reason": {
                "type": "string",
                "description": "One short sentence explaining the decision.",
            },
        },
    },
}


CLASSIFY_SYSTEM_PROMPT = (
    "You classify scholarship pages as single-scholarship or bundled. "
    "A page is BUNDLED when it enumerates 2+ distinct awards on the same page "
    "— each with its own name, and usually its own code (e.g. PGCA1110, "
    "UGCE1522) and/or distinct dollar amount, listed as a table or repeated "
    "heading→amount→eligibility blocks. "
    "A page is SINGLE when it describes one scholarship, even if that "
    "scholarship has tiered amounts (principal + stipend, 'up to $X / "
    "minimum $Y'), multiple recipients per year, or mentions related "
    "scholarships as 'see also' references. Aggregator pages that only link "
    "out to scholarships hosted elsewhere — without enumerating codes / "
    "amounts / eligibility on this page itself — are SINGLE (there's nothing "
    "to split here). Be decisive."
)


def _build_classify_message(title: str, outline: str, eligibility: str, selection: str) -> str:
    parts = []
    if title:
        parts.append(f"<page_title>\n{title.strip()}\n</page_title>")
    if outline:
        parts.append(f"<outline>\n{outline.strip()}\n</outline>")
    if eligibility:
        parts.append(f"<eligibility>\n{eligibility.strip()}\n</eligibility>")
    if selection:
        parts.append(f"<selection>\n{selection.strip()}\n</selection>")
    parts.append("Call classify_page with your verdict.")
    return "\n\n".join(parts)


def classify_page(
    title: str,
    outline: str,
    eligibility: str = "",
    selection: str = "",
) -> dict | None:
    """
    Ask the LLM whether the page describes a single scholarship or a bundle.
    Returns {"is_bundle": bool, "reason": str} or None on failure.
    Cached by SHA-256 of (title, outline, eligibility, selection).
    """
    title = title or ""
    outline = outline or ""
    eligibility = eligibility or ""
    selection = selection or ""
    if not (title or outline or eligibility or selection):
        return None

    key = _cache_key("classify", title, outline, eligibility, selection)
    cached = _read_cache("classify", key)
    if cached is not None:
        return cached

    if anthropic is None or not API_KEY:
        print("  classifier: no SDK or API key; defaulting to single", file=sys.stderr)
        return None

    client = anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL, timeout=60.0, max_retries=2)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": CLASSIFY_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[CLASSIFY_TOOL],
            messages=[{
                "role": "user",
                "content": _build_classify_message(title, outline, eligibility, selection),
            }],
        )
    except Exception as e:
        print(f"  classifier: API error: {e}", file=sys.stderr)
        return None

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "classify_page":
            value = dict(block.input)
            _write_cache("classify", key, value)
            return value

    print("  classifier: model did not call classify_page", file=sys.stderr)
    return None
