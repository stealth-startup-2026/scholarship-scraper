#!/usr/bin/env python3
"""
Build a clean (id, title, amount_display) CSV for summarize_amounts.py by
pulling the FULL benefits text from raw_payload, bypassing the scraper's
historical 120-char truncation in parse_amount() (usyd_scraper.py:74).

277 of USyd's 1059 rows currently store amount_display truncated at 117 +
'...', which is too lossy for the DeepSeek summariser to work cleanly on.
raw_payload.benefits has the untruncated source text — use that instead.

Input:  australia/usyd/usyd_scholarships.csv  (scraper output)
Output: australia/usyd/usyd_amount_display.csv (input to summarize_amounts.py)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("australia/usyd/usyd_scholarships.csv"),
        help="Scraper CSV with raw_payload column.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("australia/usyd/usyd_amount_display.csv"),
        help="Output CSV for summarize_amounts.py.",
    )
    parser.add_argument(
        "--field",
        default="benefits",
        help="raw_payload key to pull source text from. Default: benefits.",
    )
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"missing: {args.csv}", file=sys.stderr)
        return 1

    with args.csv.open() as f:
        rows = list(csv.DictReader(f))

    out_rows: list[dict] = []
    fallback_count = 0
    missing_count = 0

    for r in rows:
        try:
            rp = json.loads(r.get("raw_payload") or "{}")
        except json.JSONDecodeError:
            rp = {}
        source = (rp.get(args.field) or "").strip()
        if not source:
            # Fall back to the (possibly-truncated) amount_display column.
            # Better than dropping the row — the summariser will do what it can.
            source = (r.get("amount_display") or "").strip()
            if source:
                fallback_count += 1
            else:
                missing_count += 1
                continue

        out_rows.append({
            "id": r["id"],
            "title": r.get("title", ""),
            "amount_display": source,
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "title", "amount_display"])
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"Wrote {len(out_rows)} rows to {args.out}")
    print(f"  from raw_payload.{args.field}: {len(out_rows) - fallback_count}")
    print(f"  fell back to amount_display:    {fallback_count}")
    print(f"  skipped (no source at all):     {missing_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
