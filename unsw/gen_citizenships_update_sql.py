#!/usr/bin/env python3
"""
Generate a single SQL UPDATE that aligns the citizenships column with
DeepSeek's international-eligibility verdicts.

For each row:
- DeepSeek True  -> ensure 'INTERNATIONAL' is in citizenships
- DeepSeek False -> ensure 'INTERNATIONAL' is NOT in citizenships
Existing AU / AU-PR values are preserved.

Reads:  australia/unsw/deepseek_international_eligibility.csv
Writes: australia/unsw/update_citizenships.sql

Paste the SQL into the Supabase SQL Editor. The statement runs in one
transaction across all 147 rows, so it's safe to run repeatedly.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate citizenships UPDATE SQL from a verify_international output.")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("australia/unsw/deepseek_international_eligibility.csv"),
        help="DeepSeek verdict CSV (default: australia/unsw/...)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output SQL path (default: <csv-dir>/update_citizenships.sql)",
    )
    parser.add_argument(
        "--domestic-code",
        default="AU",
        help="Code stamped on rows that DeepSeek says are NOT international but currently have empty citizenships. "
             "Use the school's country code (AU for UNSW/USyd, UK for Oxford, etc.). Default: AU",
    )
    parser.add_argument(
        "--verdict-label",
        default="DeepSeek",
        help="Human-readable source of the CSV verdicts used in SQL comments. Default: DeepSeek",
    )
    args = parser.parse_args()

    input_path = args.csv
    output_path = args.out or input_path.parent / "update_citizenships.sql"
    domestic_code = args.domestic_code.upper()
    verdict_label = args.verdict_label

    if not input_path.exists():
        print(f"missing: {input_path}", file=sys.stderr)
        return 1

    with input_path.open() as f:
        rows = list(csv.DictReader(f))

    intl_count = sum(1 for r in rows if r["is_international"].strip().lower() == "true")
    dom_count = len(rows) - intl_count

    values_lines: list[str] = []
    for r in rows:
        sid = r["id"].strip()
        is_intl = r["is_international"].strip().lower() == "true"
        values_lines.append(f"  ('{sid}'::uuid, {str(is_intl).lower()})")
    values_block = ",\n".join(values_lines)

    sql = f"""-- Generated from {input_path}
-- Adds 'INTERNATIONAL' to citizenships for {intl_count} rows that {verdict_label}
-- judged open to international students, and removes it from the other
-- {dom_count} rows. Then stamps '{domestic_code}' on any row {verdict_label} said
-- false that still has an empty citizenships array — so empty rows don't
-- accidentally match the International filter on the frontend.
-- Existing AU / AU-PR values are preserved.
-- Safe to re-run.
--
-- Uses jsonb operators (?, ||, -) because the citizenships column was
-- created as jsonb (values show as ["AU"], not {{AU}}). If your column is
-- actually text[], swap to: 'X' = any(arr), array_append, array_remove.

-- Step 1 — toggle INTERNATIONAL based on {verdict_label}'s verdict.
with verdicts(id, is_international) as (
  values
{values_block}
)
update public.scholarships s
set citizenships = case
  when v.is_international and not (s.citizenships ? 'INTERNATIONAL')
    then s.citizenships || '["INTERNATIONAL"]'::jsonb
  when not v.is_international and (s.citizenships ? 'INTERNATIONAL')
    then s.citizenships - 'INTERNATIONAL'
  else s.citizenships
end
from verdicts v
where v.id = s.id;

-- Step 2 — empty arrays still match International on the frontend
-- (which treats [] as 'open to all'). Anything {verdict_label} classified as
-- not-international AND that has no other code should be stamped with
-- the school's country code so the filter behaves correctly.
update public.scholarships s
set citizenships = '["{domestic_code}"]'::jsonb
from (
  values
{values_block}
) as v(id, is_international)
where v.id = s.id
  and not v.is_international
  and s.citizenships = '[]'::jsonb;

-- Sanity check after running (uncomment + edit source_id):
-- select citizenships, count(*) from public.scholarships
-- where source_id = '<source_id_for_this_uni>'
-- group by citizenships order by count(*) desc;
"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(sql)

    print(f"Wrote SQL update for {len(rows)} rows ({intl_count} intl, {dom_count} domestic-only)")
    print(f"Domestic stamp:    {domestic_code}")
    print(f"Output: {output_path}")
    print()
    print("Two statements (toggle + empty-stamp). Run both in Supabase SQL Editor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
