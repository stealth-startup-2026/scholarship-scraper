"""
DeepSeek-backed summariser that turns a long scholarship `amount_display`
sentence into a short, card-friendly headline.

Why: some scrapers (notably USyd) store the full source sentence in
amount_display, e.g.
    "This scholarship is valued at $8,500 per annum and is tenable for..."
The frontend renders amount_display as a 2xl bold headline, so long
sentences overflow the fixed-height card. We want something like
"$8,500 / year" instead.

Reuses the Anthropic-SDK-against-DeepSeek pattern from
requirements_extractor.py: same env vars, same base URL, same caching shape.
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

CACHE_DIR = REPO_ROOT / "australia" / "cache" / "amount_summary"

SYSTEM_PROMPT = (
    "You normalise scholarship amount blurbs into a short, consistent headline "
    "for a card UI. Target format: '$<amount> available for <duration>'. When "
    "duration is unclear or absent, just emit '$<amount>'. The headline must "
    "never be empty and must never invent figures.\n\n"
    "Reference examples (modelled on UNSW's clean entries):\n"
    "  '$5,000 available for 1 year'\n"
    "  '$40,000 available for Duration of program (minimum)'\n"
    "  '$10,000 available for 2 years'\n"
    "  '$2,000 available for 1 semester'\n"
    "  '$5,000 available for 1 year + $2,000 stipend'\n"
    "  'Up to $10,000'\n"
    "  'Tuition + $8,500 stipend'\n"
    "  'Fully Funded'\n"
    "  'RTP Stipend'\n"
    "  'See details'\n\n"
    "─── DECISION LADDER (apply in order, stop at the first match) ───\n"
    "1. SINGLE DOLLAR FIGURE + clear duration → '$X available for <duration>'.\n"
    "   Duration cues: 'per annum'/'p.a.'/'per year' → '1 year' (or 'N years' if a "
    "   total span is given); 'per semester' → '1 semester'; 'per month' → '1 month'; "
    "   'for the duration of the program' / 'tenable for the program' → 'Duration of program'.\n"
    "2. RANGE (e.g. '$5,000 to $10,000', 'up to $10,000') → use the maximum and prefix "
    "   with 'Up to': 'Up to $10,000 available for 1 year', or just 'Up to $10,000' if "
    "   duration is missing.\n"
    "3. TIERED / MULTI-COMPONENT (e.g. tuition + stipend, principal + book allowance, "
    "   '$5,000 plus $2,000 living allowance') → combine the two most prominent "
    "   components with '+': 'Tuition + $8,500 stipend', '$5,000 + $2,000 stipend'. "
    "   If there are 3+, pick the largest + 'and more', e.g. '$30,000 stipend and more'.\n"
    "4. ONE-OFF / LUMP SUM (cues: 'one-off', 'once only', 'lump sum', 'single payment', "
    "   'paid as a single instalment') → emit just '$X' with NO duration clause.\n"
    "5. AMOUNT QUOTED BUT DURATION UNSPECIFIED / VAGUE ('valued at $X, period to be "
    "   advised in offer letter', 'amount and term determined by committee') → emit "
    "   just '$X' (or 'Up to $X' for ranges). Never return empty.\n"
    "6. MULTIPLE RECIPIENTS (e.g. '5 awards of $1,000 each', '$20,000 divided among "
    "   4 students') → use the per-recipient figure: '$1,000', '$5,000'.\n"
    "7. NON-DOLLAR / IN-KIND ONLY (no $ figure in source) → short label, no duration "
    "   clause: 'Fully Funded', 'Tuition waiver', 'Stipend', 'Travel grant', "
    "   'Accommodation', 'Mentorship', 'Industry placement'. Combine if needed: "
    "   'Tuition + Stipend'.\n"
    "8. PhD / HDR STIPEND REFERENCED BY RATE NAME (e.g. 'standard RTP rate', "
    "   'Australian Government RTP stipend', 'iMQRES rate') → 'RTP Stipend' (or the "
    "   stated rate name + 'Stipend'). Do NOT invent the rate's current dollar value. "
    "   If the source ALSO states a duration ('for up to 3 years', 'for 3.5 years'), "
    "   APPEND the duration: 'RTP Stipend available for 3.5 years'. Do NOT bail to "
    "   'See details' just because the source pairs a named rate with a duration — "
    "   that's a normal, informative case.\n"
    "8b. TUITION-ONLY AWARD with a stated duration ('covers tuition fees for up to "
    "    four years') → 'Tuition available for N years'. With no stated duration → "
    "    just 'Tuition'. Do NOT drop the duration when it's stated.\n"
    "8c. RANGE + ONE-OFF combo ('valued up to $X and is paid as a one-off payment') "
    "    → 'Up to $X' (no duration clause). The 'one-off' just means no recurrence; "
    "    it doesn't make the row unrepresentable.\n"
    "8d. FULL-TIME / PART-TIME TIERS ('$5,000 for full-time students and $2,500 for "
    "    part-time') → '$5,000 (FT) / $2,500 (PT)'. Keep both figures.\n"
    "9. NON-AUD CURRENCY (e.g. '£6,000', '€8,000', 'USD 10,000') → preserve the "
    "   currency symbol/prefix as printed: '£6,000 available for 1 year'.\n"
    "10. NUMBER WRITTEN AS WORDS ('five thousand dollars') → convert to digits "
    "    with $ prefix: '$5,000'.\n"
    "11. PURELY NON-INFORMATIVE ('TBA', 'to be confirmed', 'refer to handbook', "
    "    'varies', completely empty, or just an application URL) → 'See details'.\n\n"
    "─── HARD RULES ───\n"
    "- NEVER return an empty amount_display. The last-resort fallback is 'See details'.\n"
    "- NEVER invent a number, duration, currency, or rate not present in the source.\n"
    "- NEVER include prose like 'is valued at', 'is tenable for', 'is awarded to', "
    "  recipient eligibility, selection criteria, application instructions, or "
    "  university/donor names.\n"
    "- Strip parenthetical commentary unless it's load-bearing for the amount itself "
    "  (e.g. 'Duration of program (minimum)' is fine; '$5,000 (subject to satisfactory "
    "  progress)' should become just '$5,000').\n"
    "- IGNORE subordinate / conditional clauses when extracting amount + duration. "
    "  Phrases like 'plus an additional year for completion of the Bachelor of "
    "  Advanced Studies', 'subject to satisfactory academic performance', 'where "
    "  applicable', 'with possibility to extend', 'on the basis the student remains "
    "  enrolled full-time' are NOT reasons to bail to 'See details'. Extract the "
    "  lead figure + lead duration and drop the conditional clause. Example: "
    "  '$9,000 per year, tenable for up to three years, plus an additional year for "
    "  completion of the Bachelor of Advanced Studies (where applicable)' → "
    "  '$9,000 available for 3 years'.\n"
    "- BAILING TO 'See details' IS A LAST RESORT. Use it only when the source "
    "  genuinely doesn't quote a figure, rate name, or non-cash benefit you can "
    "  extract (e.g. 'value and duration determined by the dean'). If the source "
    "  quotes ANY dollar figure or named stipend rate, extract it — even if the "
    "  surrounding text is verbose, multi-paragraph, or has conditional clauses.\n"
    "- Keep the headline under ~50 characters; under ~40 when possible.\n"
    "- Only set amount_value when a single dollar figure clearly represents the award "
    "  (skip it for tiered/multi-component cases where one number would mislead).\n"
    "- Only set frequency when explicitly stated in the source."
)

SUMMARISE_TOOL = {
    "name": "record_amount_summary",
    "description": (
        "Record a short, card-friendly amount headline derived from the "
        "scholarship's amount_display source text. Always call this tool exactly once."
    ),
    "input_schema": {
        "type": "object",
        "required": ["amount_display"],
        "properties": {
            "amount_display": {
                "type": "string",
                "description": (
                    "Short, non-empty card headline. Preferred shape: "
                    "'$<amount> available for <duration>'. Drop the duration "
                    "clause when unknown ('$5,000' / 'Up to $10,000'). For "
                    "tiered awards combine with '+' ('Tuition + $8,500 stipend'). "
                    "For in-kind awards use a label ('Fully Funded', 'RTP Stipend', "
                    "'Tuition waiver'). Last-resort fallback: 'See details'. "
                    "MUST NOT be empty."
                ),
            },
            "amount_value": {
                "type": "number",
                "description": (
                    "Numeric value of the headline in the source currency, "
                    "if a single dollar figure is quoted. For ranges, use "
                    "the maximum. Omit when the source has no dollar figure."
                ),
            },
            "frequency": {
                "type": "string",
                "enum": ["one_time", "yearly", "semester", "monthly", "custom"],
                "description": (
                    "Frequency inferred from phrases like 'per annum' (yearly), "
                    "'per semester' (semester), 'lump sum' (one_time). Omit if "
                    "the source is silent."
                ),
            },
        },
    },
}


def _cache_key(text: str) -> str:
    h = hashlib.sha256()
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
    """
    Returns {"amount_display": str, "amount_value"?: float, "frequency"?: str}
    or None when extraction fails. Caller should fall back to leaving the row
    unchanged on None.
    """
    source = (source or "").strip()
    if not source:
        return None

    key = _cache_key(source)
    cached = _read_cache(key)
    if cached is not None:
        return cached

    if anthropic is None:
        print("  amount_summarizer: anthropic SDK not installed", file=sys.stderr)
        return None
    if not API_KEY:
        print("  amount_summarizer: no DEEPSEEK_API_KEY/ANTHROPIC_API_KEY", file=sys.stderr)
        return None

    client = anthropic.Anthropic(
        api_key=API_KEY, base_url=BASE_URL, timeout=60.0, max_retries=2,
    )

    base_message = (
        f"<source_amount_display>\n{source}\n</source_amount_display>\n\n"
        "Call record_amount_summary with the compressed headline."
    )
    # deepseek-chat occasionally returns plain text instead of calling the
    # tool. Retry once with a more forceful instruction before giving up.
    forceful_message = (
        f"<source_amount_display>\n{source}\n</source_amount_display>\n\n"
        "You MUST call the record_amount_summary tool. Do not reply with "
        "plain text. If the source has no dollar figure, still call the tool "
        "with amount_display set to 'See details' or another short phrase."
    )

    for attempt, user_message in enumerate((base_message, forceful_message), 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=256,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=[SUMMARISE_TOOL],
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            print(f"  amount_summarizer: API error (attempt {attempt}): {e}", file=sys.stderr)
            return None

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_amount_summary":
                value = dict(block.input)
                # Don't cache empty results — re-running with a better prompt
                # should be allowed to retry these without manual cache busting.
                if (value.get("amount_display") or "").strip():
                    _write_cache(key, value)
                return value

        if attempt == 1:
            print("  amount_summarizer: no tool call on first try, retrying with stricter prompt", file=sys.stderr)

    print("  amount_summarizer: model refused to call record_amount_summary after retry", file=sys.stderr)
    return None
