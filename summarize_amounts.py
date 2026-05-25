#!/usr/bin/env python3
"""
Summarise long `amount_display` strings into short card headlines using
DeepSeek, and emit an UPDATE SQL the user can paste into Supabase.

Input CSV (export from Supabase SQL editor). For USyd, normalise EVERY row
because the source page formatting is inconsistent:
    select id, title, amount_display
    from public.scholarships
    where provider_name = 'University of Sydney'
      and amount_display is not null
    order by title;
    -- Download as CSV → save as e.g. australia/usyd/usyd_amount_display.csv

Outputs (written next to the input CSV):
    deepseek_amount_summary.csv   id, original, summary, amount_value, frequency
    update_amount_display.sql     single transactional UPDATE for Supabase

Cost: each call is tiny (~few hundred tokens). Cached on disk under
australia/cache/amount_summary/ so re-runs are free.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import amount_summarizer as strict_mod
import amount_summarizer_simple as simple_mod


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarise scholarship amount_display via DeepSeek.")
    parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="Input CSV with columns: id, title, amount_display.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: same dir as --csv).",
    )
    parser.add_argument(
        "--min-length",
        type=int,
        default=0,
        help=(
            "Skip rows whose amount_display is shorter than this. Default: 0 "
            "(process every row in the input CSV — appropriate when the source "
            "is inconsistent and you want every row normalised to the same shape)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N rows (handy for dry runs).",
    )
    parser.add_argument(
        "--simple",
        action="store_true",
        help=(
            "Use the loose-prompt summariser (amount_summarizer_simple). "
            "Writes to *_simple.csv / *_simple.sql so it doesn't clobber the "
            "strict run's outputs. Use both and diff for an A/B comparison."
        ),
    )
    args = parser.parse_args()

    mod = simple_mod if args.simple else strict_mod
    summarize_amount = mod.summarize_amount
    _cache_key = mod._cache_key
    _read_cache = mod._read_cache
    suffix = "_simple" if args.simple else ""

    if not args.csv.exists():
        print(f"missing: {args.csv}", file=sys.stderr)
        return 1

    out_dir = args.out_dir or args.csv.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    verdict_csv = out_dir / f"deepseek_amount_summary{suffix}.csv"
    failures_csv = out_dir / f"deepseek_amount_failures{suffix}.csv"
    sql_path = out_dir / f"update_amount_display{suffix}.sql"

    with args.csv.open() as f:
        rows = list(csv.DictReader(f))

    required = {"id", "amount_display"}
    if not required.issubset(rows[0].keys() if rows else set()):
        print(f"input CSV must have columns: {sorted(required)}", file=sys.stderr)
        return 1

    api_calls = 0
    cache_hits = 0
    skipped = 0
    failures = 0
    results: list[dict] = []
    failed_rows: list[dict] = []

    processed = 0
    for i, r in enumerate(rows, 1):
        if args.limit and processed >= args.limit:
            break

        sid = (r.get("id") or "").strip()
        title = (r.get("title") or "").strip()
        source = (r.get("amount_display") or "").strip()

        if not sid or not source:
            skipped += 1
            continue
        if len(source) < args.min_length:
            skipped += 1
            continue

        processed += 1
        was_cached = _read_cache(_cache_key(source)) is not None
        verdict = summarize_amount(source)
        if was_cached:
            cache_hits += 1
        else:
            api_calls += 1
            time.sleep(0.1)

        if verdict is None or not (verdict.get("amount_display") or "").strip():
            failures += 1
            reason = "no verdict" if verdict is None else "empty amount_display"
            print(f"  [{i:>3}/{len(rows)}] FALLBACK ({reason})  {title[:60]}", file=sys.stderr)
            failed_rows.append({
                "id": sid, "title": title, "original": source, "reason": reason,
            })
            # Don't drop the row — emit a safe fallback so every input ends up
            # in the SQL. The failures CSV still records what hit the fallback
            # so you can hand-edit those in Supabase later if needed.
            verdict = {"amount_display": "See details"}

        summary = verdict["amount_display"].strip()
        print(f"  [{i:>3}/{len(rows)}] {summary:<24}  ← {source[:60]}")
        results.append({
            "id": sid,
            "title": title,
            "original": source,
            "summary": summary,
            "amount_value": verdict.get("amount_value", ""),
            "frequency": verdict.get("frequency", ""),
        })

    # ── Write verdict CSV ───────────────────────────────────────────────
    with verdict_csv.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["id", "title", "original", "summary", "amount_value", "frequency"],
        )
        writer.writeheader()
        writer.writerows(results)

    # ── Write UPDATE SQL ────────────────────────────────────────────────
    # One CTE of (id, new_amount_display) values, single UPDATE. Keeps the
    # original sentence in description (only when description is empty) so
    # the prose isn't lost. Idempotent: re-running on already-short
    # amount_display rows is a no-op because we filter on the source value
    # matching `original`.
    if results:
        values_lines: list[str] = []
        for r in results:
            values_lines.append(
                f"  ('{r['id']}'::uuid, "
                f"'{_sql_escape(r['summary'])}', "
                f"'{_sql_escape(r['original'])}')"
            )
        values_block = ",\n".join(values_lines)

        sql = f"""-- Generated by summarize_amounts.py from {args.csv.name}
-- Replaces verbose amount_display values with DeepSeek-summarised headlines
-- for {len(results)} scholarship row(s). Preserves the original sentence by
-- moving it into description when description is null/empty.
--
-- Safe to re-run: the UPDATE is gated on amount_display still matching the
-- original sentence, so rows already shortened are skipped.

with new_values(id, summary, original) as (
  values
{values_block}
)
update public.scholarships s
set
  amount_display = v.summary,
  description = case
    when s.description is null or btrim(s.description) = '' then v.original
    else s.description
  end
from new_values v
where s.id = v.id
  and s.amount_display = v.original;

-- Sanity check after running:
-- select id, amount_display, length(amount_display) as len
-- from public.scholarships
-- where id in ({', '.join(f"'{r['id']}'::uuid" for r in results[:5])})
-- order by id;
"""
        sql_path.write_text(sql)

    if failed_rows:
        with failures_csv.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "title", "original", "reason"])
            writer.writeheader()
            writer.writerows(failed_rows)

    print()
    print(f"Processed:   {processed}  (skipped {skipped})")
    print(f"API calls:   {api_calls}  cache hits: {cache_hits}  failures: {failures}")
    print(f"Verdict CSV: {verdict_csv}")
    if failed_rows:
        print(f"Failures:    {failures_csv}")
    if results:
        print(f"Update SQL:  {sql_path}")
        print()
        print("Paste update_amount_display.sql into the Supabase SQL Editor.")
    else:
        print("No rows to update — SQL not written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
