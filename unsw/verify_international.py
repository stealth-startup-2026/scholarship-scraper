#!/usr/bin/env python3
"""
Verifies which UNSW scholarships are open to international students.

Reads the existing scrape's CSV (no re-curling — raw_payload already
contains the page text). For each row, asks DeepSeek a single binary
classification: is this scholarship open to international students?

Outputs `australia/unsw/deepseek_international_eligibility.csv` with the
same columns as the Codex baseline (id, name, is_international) so the
two files diff cleanly.

Cost on UNSW's 147 rows: ~$0.05 first run (deepseek-chat, ~2k input
tokens per call). Cached on disk under australia/cache/international/
so re-runs are free.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Reuse env loading + endpoint config from the extractor module.
from requirements_extractor import API_KEY, BASE_URL, MODEL  # noqa: F401

CACHE_DIR = REPO_ROOT / "australia" / "cache" / "international"

SYSTEM_PROMPT = (
    "You classify whether a scholarship is open to international students "
    "based on the scholarship's eligibility / residency / criteria text. "
    "International students = students who are NOT citizens or permanent "
    "residents of the country where the scholarship's institution is located.\n\n"
    "Return is_international=true if:\n"
    "- The text explicitly includes international students, OR\n"
    "- The text states no citizenship / residency restriction.\n\n"
    "Return is_international=false if:\n"
    "- The text explicitly limits the scholarship to domestic citizens, "
    "permanent residents, or specific national groups that exclude "
    "international students.\n\n"
    "Bias toward the literal page text — do not infer beyond what is stated."
)

VERIFY_TOOL = {
    "name": "verify_international",
    "description": (
        "Record whether the scholarship is open to international students "
        "and a one-sentence reason citing the page text."
    ),
    "input_schema": {
        "type": "object",
        "required": ["is_international", "reasoning"],
        "properties": {
            "is_international": {"type": "boolean"},
            "reasoning": {
                "type": "string",
                "description": (
                    "One short sentence citing the page text that supports "
                    "the decision."
                ),
            },
        },
    },
}


def _cache_key(name: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(MODEL.encode())
    h.update(b"\0")
    h.update(name.encode("utf-8", errors="replace"))
    h.update(b"\0")
    h.update(text.encode("utf-8", errors="replace"))
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


def extract_relevant_text(row: dict) -> str:
    """Pull the bits of the scraped row most relevant to international
    eligibility. Mirrors what a human would read on the page."""
    try:
        rp = json.loads(row.get("raw_payload") or "{}")
    except json.JSONDecodeError:
        rp = {}
    parts: list[str] = []
    for key in ("residency", "criteria", "education_level", "eligibility", "outline", "selection"):
        v = rp.get(key)
        if v:
            parts.append(f"<{key}>\n{v}\n</{key}>")
    if not parts and row.get("eligibility_summary"):
        parts.append(f"<eligibility_summary>\n{row['eligibility_summary']}\n</eligibility_summary>")
    return "\n\n".join(parts)


def verify(name: str, text: str) -> dict | None:
    """Returns {'is_international': bool, 'reasoning': str} or None on failure."""
    key = _cache_key(name, text)
    cached = _read_cache(key)
    if cached is not None:
        return cached

    if not API_KEY:
        print("  no API key set", file=sys.stderr)
        return None

    client = anthropic.Anthropic(
        api_key=API_KEY, base_url=BASE_URL, timeout=60.0, max_retries=2,
    )
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[VERIFY_TOOL],
            messages=[{
                "role": "user",
                "content": (
                    f"<scholarship_name>{name}</scholarship_name>\n\n"
                    f"{text}\n\n"
                    "Call verify_international with your decision."
                ),
            }],
        )
    except Exception as e:
        print(f"  API error: {e}", file=sys.stderr)
        return None

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "verify_international":
            value = dict(block.input)
            _write_cache(key, value)
            return value

    print("  model did not call verify_international", file=sys.stderr)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("australia/unsw/unsw_scholarships.csv"),
        help="Input scraper CSV (default: australia/unsw/unsw_scholarships.csv)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: same dir as --csv)",
    )
    args = parser.parse_args()

    input_path = args.csv
    output_dir = args.out_dir or input_path.parent
    output_path = output_dir / "deepseek_international_eligibility.csv"
    reason_log_path = output_dir / "deepseek_international_reasons.csv"

    if not input_path.exists():
        print(f"missing: {input_path}", file=sys.stderr)
        return 1

    with input_path.open() as f:
        rows = list(csv.DictReader(f))

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    api_calls = 0
    cache_hits = 0
    no_text = 0
    failures = 0
    intl_count = 0

    for i, r in enumerate(rows, 1):
        sid = r.get("id", "")
        name = r.get("title", "")
        text = extract_relevant_text(r)

        if not text:
            no_text += 1
            print(f"  [{i:>3}/{len(rows)}] (no text)   {name[:60]}")
            results.append({"id": sid, "name": name, "is_international": False,
                             "reasoning": "no eligibility text on page"})
            continue

        was_cached = _read_cache(_cache_key(name, text)) is not None
        verdict = verify(name, text)
        if was_cached:
            cache_hits += 1
        else:
            api_calls += 1
            time.sleep(0.1)  # gentle pacing for non-cached calls

        if verdict is None:
            failures += 1
            print(f"  [{i:>3}/{len(rows)}] FAIL        {name[:60]}")
            results.append({"id": sid, "name": name, "is_international": False,
                             "reasoning": "API failed"})
            continue

        is_intl = bool(verdict.get("is_international"))
        reasoning = (verdict.get("reasoning") or "").strip()
        if is_intl:
            intl_count += 1
        marker = "INT " if is_intl else "DOM "
        print(f"  [{i:>3}/{len(rows)}] {marker}        {name[:60]}")
        results.append({"id": sid, "name": name, "is_international": is_intl,
                         "reasoning": reasoning})

    # Codex-compatible CSV — id, name, is_international (capitalised True/False
    # to match Codex's output verbatim).
    with output_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "name", "is_international"])
        w.writeheader()
        for row in results:
            w.writerow({
                "id": row["id"],
                "name": row["name"],
                "is_international": "True" if row["is_international"] else "False",
            })

    # Debug log with the per-row reasoning — separate file so the primary
    # output stays diffable against the Codex baseline.
    with reason_log_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "name", "is_international", "reasoning"])
        w.writeheader()
        for row in results:
            w.writerow({**row, "is_international": "True" if row["is_international"] else "False"})

    print()
    print(f"Total rows:        {len(results)}")
    print(f"International:     {intl_count}")
    print(f"Domestic / other:  {len(results) - intl_count}")
    print(f"API calls:         {api_calls}")
    print(f"Cache hits:        {cache_hits}")
    print(f"No-text rows:      {no_text}")
    print(f"API failures:      {failures}")
    print(f"Output (Codex shape):  {output_path}")
    print(f"Output (with reasons): {reason_log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
