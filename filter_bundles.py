#!/usr/bin/env python3
"""
Filter and split an already-scraped CSV against the current bundle rules.

Use this when the scraper has already run and you've tightened detection or
added splitting — re-applies both to the existing rows without re-fetching
pages. The page sections live in `raw_payload`, so no HTTP is needed; only
the LLM splitter makes API calls (and only for rows flagged as bundled).

Reads:  australia/unsw_scholarships.csv
Writes: australia/unsw_scholarships.filtered.csv
        australia/unsw_bundled_pages.filtered.csv
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from requirements_extractor import classify_page
from unsw_scraper import OUTPUT_FIELDNAMES, build_sub_rows, detect_bundle

IN_PATH = Path("australia/unsw_scholarships.csv")
OUT_PATH = Path("australia/unsw_scholarships.filtered.csv")
BUNDLE_PATH = Path("australia/unsw_bundled_pages.filtered.csv")


def main() -> int:
    if not IN_PATH.exists():
        print(f"missing: {IN_PATH}", file=sys.stderr)
        return 1

    with IN_PATH.open() as f:
        rows = list(csv.DictReader(f))

    kept: list[dict] = []
    split_count = 0
    bundled: list[dict] = []

    for r in rows:
        try:
            rp = json.loads(r["raw_payload"])
        except (json.JSONDecodeError, KeyError):
            rp = {}
        outline = rp.get("outline", "") or ""
        eligibility = rp.get("eligibility", "") or ""
        selection = rp.get("selection", "") or ""

        # LLM classifier on every row. Fall back to the regex if the API call
        # fails so this still works without a key.
        classification = classify_page(r["title"], outline, eligibility, selection)
        regex_reason = detect_bundle({}, outline, eligibility, selection)
        if classification is None:
            is_bundle = bool(regex_reason)
            decision_reason = regex_reason or "classifier unavailable"
        else:
            is_bundle = bool(classification.get("is_bundle"))
            decision_reason = classification.get("reason", "")

        if not is_bundle:
            kept.append(r)
            continue

        sub_rows = build_sub_rows(r, r["title"], outline, eligibility, selection)
        if sub_rows is None:
            bundled.append({"url": r["external_url"], "title": r["title"], "reason": decision_reason})
            print(f"  SKIP {r['title'][:60]} — {decision_reason[:60]}")
            continue

        kept.extend(sub_rows)
        split_count += 1
        print(f"  SPLIT {r['title'][:60]} → {len(sub_rows)} sub-awards")

    with OUT_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        w.writeheader()
        w.writerows(kept)

    with BUNDLE_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["url", "title", "reason"])
        w.writeheader()
        w.writerows(bundled)

    singles = len(kept) - sum(len(_dummy) for _dummy in [])  # placeholder for clarity
    print()
    print(f"in:      {len(rows)} rows ({IN_PATH})")
    print(f"kept:    {len(kept)} rows ({OUT_PATH}) — singles + sub-awards from {split_count} split pages")
    print(f"bundled: {len(bundled)} unsplittable rows ({BUNDLE_PATH})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
