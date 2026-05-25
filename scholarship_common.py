"""Shared helpers for scholarship scraper CSV generation.

This module contains code that is not specific to one university:

- the Supabase CSV column order
- slug generation for derived rows
- regex-assisted bundle detection used as an offline/audit fallback
- sub-award row construction after the LLM splitter runs

University-specific scrapers still own their page parsing, source IDs, award
code regexes, and field inference rules.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Pattern

from requirements_extractor import split_bundle

OUTPUT_FIELDNAMES = [
    "id", "source_id", "external_id", "external_url", "slug", "title",
    "provider_name", "description", "amount_value", "amount_currency",
    "amount_display", "awards_count", "frequency", "study_level",
    "funding_type", "citizenships", "eligible_countries", "excluded_countries",
    "eligible_genders", "minimum_gpa", "requires_essay", "need_based",
    "merit_based", "school_name", "country", "state_region",
    "application_open_at", "deadline_at", "start_term", "is_recurring",
    "is_active", "is_featured", "last_verified_at", "source_last_synced_at",
    "tags", "eligibility_summary", "application_requirements", "contact_info",
    "raw_payload", "created_at", "updated_at",
]

# Phrases that strongly indicate a page lists multiple sub-awards. The keyword
# must sit near scholarships/awards/external so generic wording like
# "considered for the scholarship" does not trigger.
BUNDLE_PHRASE_RE = re.compile(
    r'(?:the\s+following|the\s+below|a\s+number\s+of|wide\s+range\s+of|several|various)\s+'
    r'(?:scholarships|awards|external\s+scholarships)',
    re.IGNORECASE,
)


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:80]


def detect_bundle(
    award_code_re: Pattern[str],
    outline: str,
    eligibility: str,
    selection: str,
) -> str | None:
    """Return a reason string when regex signals suggest a bundled page.

    The LLM classifier is still the primary bundle verdict. This function is
    intentionally conservative and exists for audit logging plus offline
    fallback when the classifier cannot run.
    """
    text = " ".join(filter(None, [outline, eligibility, selection]))
    if not text:
        return None

    codes = set(award_code_re.findall(text))
    if len(codes) >= 2:
        return f"multiple award codes: {sorted(codes)}"

    amounts = set(re.findall(r'\$[0-9][0-9,]*', text))
    if len(amounts) >= 3:
        return f"multiple distinct amounts: {sorted(amounts)}"

    phrase = BUNDLE_PHRASE_RE.search(text)
    if phrase:
        return f"list-of-awards phrase: '{phrase.group(0)}'"

    return None


def build_sub_rows(
    parent_row: dict,
    title: str,
    outline: str,
    eligibility: str,
    selection: str,
) -> list[dict] | None:
    """Return child rows for a bundled page, or None if it cannot be split."""
    split = split_bundle(title, outline, eligibility, selection)
    sub_awards = (split or {}).get("sub_awards") or []
    if not sub_awards:
        return None

    application_mode = (split or {}).get("application_mode", "automatic")
    sep_required = (split or {}).get("separate_application_required", False)
    full_url = parent_row.get("external_url", "")

    sub_rows: list[dict] = []
    for i, sub_award in enumerate(sub_awards):
        code = (sub_award.get("code") or "").strip() or None
        sub_title = (sub_award.get("title") or title).strip()
        must_meet = [
            item.strip()
            for item in (sub_award.get("must_meet") or [])
            if item and item.strip()
        ]

        row = dict(parent_row)
        row["id"] = str(uuid.uuid4())
        row["external_id"] = code or f"{parent_row['external_id']}-{i + 1}"
        row["external_url"] = f"{full_url}#{code or i + 1}"
        row["slug"] = slugify(sub_title) or f"{parent_row['slug']}-{i + 1}"
        row["title"] = sub_title
        if sub_award.get("amount_value") is not None:
            try:
                row["amount_value"] = float(sub_award["amount_value"])
            except (TypeError, ValueError):
                pass
        if sub_award.get("amount_display"):
            row["amount_display"] = sub_award["amount_display"]
        row["eligibility_summary"] = "\n".join(must_meet) if must_meet else None
        row["application_requirements"] = json.dumps({
            "application_mode": application_mode,
            "separate_application_required": sep_required,
            "must_meet": must_meet,
            "parent_url": full_url,
            "parent_title": title,
        })
        sub_rows.append(row)

    return sub_rows
