# Scholarship Scraper — Supabase Import Guide

This guide documents the end-to-end steps to take a scraped scholarship CSV and get it correctly imported into the Supabase `scholarships` table for the Caat_V2 project.

The target Supabase project is `caat-frontend` (URL: `https://qgbdirrobbtfrwbwtvjm.supabase.co`).

---

## Target schema

Each row of the CSV maps 1:1 to a row in `public.scholarships`. The relevant tables are:

- `public.scholarship_sources` — provider/source metadata (one row per scraper, e.g. UNSW, UniMelb)
- `public.scholarships` — the scholarship records
- `public.schools` — institutions (universities/colleges); pre-existing
- `public.scholarship_schools` — junction table linking scholarships to schools

The authoritative TypeScript schema lives at:
`/Users/kevinhu/Caat_V2/caat-frontend/types/scholarships.ts` (`ScholarshipRow` interface).

---

## CSV format requirements

Headers must match the `public.scholarships` columns exactly. Required headers:

```
id, source_id, external_id, external_url, slug, title, provider_name, description,
amount_value, amount_currency, amount_display, awards_count, frequency,
study_level, funding_type, eligible_countries, excluded_countries, eligible_genders,
minimum_gpa, requires_essay, need_based, merit_based, school_name, country, state_region,
application_open_at, deadline_at, start_term, is_recurring, is_active, is_featured,
last_verified_at, source_last_synced_at, tags, eligibility_summary,
application_requirements, contact_info, raw_payload, created_at, updated_at
```

### Field formatting rules

| Type | Format | Example |
|------|--------|---------|
| UUID | Standard hyphenated form | `c2d4e6f8-1a3b-5c7d-9e0f-2b4c6d8e0f1a` |
| Boolean | `True` / `False` (Postgres accepts these case-insensitively) | `True` |
| Numeric (nullable) | Plain number or empty string for null | `10000.0` or `` |
| Text array (`text[]`) | JSON array string | `["postgraduate"]`, `[]` |
| JSONB | JSON object string | `{"key": "value"}` |
| Timestamp | ISO with timezone | `2025-11-03 00:00:00.000000+00` |
| Empty/null | Empty string (not the literal text `null`) | `,,` |

### Columns NOT in the DB schema (do not include)

- `citizenships` — not part of `public.scholarships`. Strip from CSV before import.

### `application_requirements` shape — caveats

The scholarship detail page (`app/(main)/scholarships/[id]/page.tsx`) only knows how to render two specific keys in this JSONB column:

```json
{
  "application_mode": "manual" | "automatic",
  "separate_application_required": true | false,
  "must_meet": ["bullet 1", "bullet 2", "..."]
}
```

The UniMelb seed uses this shape successfully. **However**, do NOT blindly convert every scrape into this shape — it only works when each DB row represents **a single award with a clean list of eligibility bullets**.

Failure mode observed on UNSW: some scraped rows bundle multiple sub-awards into one `eligibility_text` (e.g. "Exchange Awards" with 8 separate awards listed in the same field, each with their own name, dollar amount, and bullets). Splitting that text on newlines into `must_meet` produces a 50-line checklist where titles and prices get green checkmarks too — visually broken.

Two correct approaches for messy/bundled sources:

1. **Split into multiple rows at scrape time** — each sub-award becomes its own `public.scholarships` row with a clean `must_meet` array. Preferred when sub-awards have distinct titles and amounts.
2. **Leave `application_requirements` empty (or use a different key like `eligibility_text`)** — the card will render an empty state, but the eligibility info is still visible in the Eligibility card above (via `eligibility_summary`). Acceptable as a stopgap; the detail page would need new rendering logic to display `eligibility_text` as preformatted free text if you want it shown.

If the source page presents eligibility as a clean intro + bullets like:
> *"To be eligible, applicants must:*
> *Be commencing studies in ...*
> *Have completed a Bachelor of Laws ..."*

…then `must_meet` works: strip the intro line (ending in `:`) and emit the remaining lines as bullets.

---

## Bundle detection — do it at scrape time, not in SQL afterwards

Multiple uni sites publish "index" pages where one URL describes 2+ distinct sub-awards (e.g. UNSW's "Exchange Awards" page lists 8 separate awards each with their own name, code, and dollar amount). Importing such a page as one row produces a visually broken card (long checklist of mixed bullets + titles + prices) and makes filtering, bookmarks, and detail URLs incoherent.

**Rule**: every scraper must detect bundled pages before constructing a row, and either:
- split into N rows (one per sub-award), or
- skip + log the URL for a later split pass.

Never silently emit one row for a bundled page.

### Structural signals that generalise across unis

These are what `detect_bundle()` in `unsw_scraper.py` uses. The signals are deliberately structural (counts, not phrasing), so they transplant cleanly to other uni scrapers:

| Signal | How to detect | Notes |
|---|---|---|
| Multiple distinct award codes in eligibility text | `len(set(AWARD_CODE_RE.findall(elig))) >= 2` | Only meaningful with a tight, site-specific award-code regex — see below |
| Multiple distinct dollar amounts in eligibility text | `len(set(re.findall(r'\$[0-9][0-9,]*', elig))) >= 2` | One real award almost always quotes one headline amount |
| Repeated heading→amount→bullets DOM blocks | Count of matching sibling sections | Strongest signal when available; varies per site's markup |

### Signals that look universal but aren't

- **"N Scholarships available" → N>1 is NOT a bundle signal.** UNSW (and several other unis) use this phrasing to mean "N recipients of the same award", not "N distinct awards". Don't wire it into the bundle check.
- **Any `[A-Z]{2,}\d+` regex on body text** will catch course codes (`LAWS3361`, `COMP1511`) mentioned in eligibility prose, producing false positives. Award-code regexes must be specific to the uni's award-code shape — see next section.

### Per-scraper award-code regex (the one piece that genuinely varies)

Each uni mints its own award-code format, so each scraper declares its own `AWARD_CODE_RE` at module top. Examples:

```python
# unsw_scraper.py
AWARD_CODE_RE = re.compile(r'\b((?:UG|PG|PU)[A-Z]{2}\d{3,5})\b')
# UGCA1392, PGCE2017, PUCA1029 — but NOT LAWS3361, COMP1511, MFAC5101

# unimelb_scraper.py (example)
AWARD_CODE_RE = re.compile(r'\b\d{6,7}\b')
# UniMelb uses numeric scholarship IDs
```

When adding a new uni:
1. Inspect 10–20 sample pages, write down the award-code shapes you see.
2. Build a regex that matches those shapes and rejects everything else (especially course/program codes — those have their own faculty-letter prefixes).
3. Use the same regex for both `external_id` extraction and bundle detection.
4. After the first scrape run, sanity-check with `SELECT DISTINCT substring(external_id from '^[A-Z]+'), count(*) ...` — every prefix in the result should be a legitimate award prefix for that uni. Stray course-code prefixes (`LAWS`, `COMP`, etc.) mean the regex is too loose.

### What to do with the bundled-pages log

`unsw_scraper.py` writes skipped bundles to `australia/unsw_bundled_pages.csv`. Treat that file as a backlog:
- For pages worth importing as multiple rows, write a per-site splitter that fetches each URL, chunks the eligibility text on the `Title (CODE)` pattern, and emits N rows.
- For pages not worth splitting (informational hubs, no funded sub-awards), leave them skipped.

---

## Per-CSV import workflow

For each new scraper CSV (UNSW, UniMelb, USyd, Monash, etc.):

### Step 1 — Pick a stable source UUID and slug

Decide on a unique `source_id` (UUID v4) and `slug` for the scraper. Reuse the same UUID across all CSV rows for that source. Record it in the scraper code so re-scrapes produce the same source_id.

Example for UNSW: `c2d4e6f8-1a3b-5c7d-9e0f-2b4c6d8e0f1a`, slug `unsw-official`.

### Step 2 — Insert the source row in Supabase

In the Supabase **SQL Editor**, run (replacing values for the new source):

```sql
INSERT INTO public.scholarship_sources (
    id, slug, name, base_url, source_type, is_active
) VALUES (
    '<source_uuid>',
    '<source-slug>',
    '<Display Name>',
    '<scraper base URL>',
    'official_site',
    true
) ON CONFLICT (id) DO NOTHING;
```

This **must** run before the scholarships import — `scholarships.source_id` has a foreign key constraint on `scholarship_sources.id`. Skipping this gives:
```
ERROR: 23503: insert or update on table "scholarships"
violates foreign key constraint "scholarships_source_id_fkey"
```

### Step 3 — Import the CSV

Open the Supabase dashboard → **Table Editor** → `scholarships` table → **Insert** → **Import data from CSV**.

Upload the CSV. The preview will show the actual row count being imported. If the preview shows fewer rows than expected, check for:
- Unmatched headers (typos vs. table columns)
- Missing source row (Step 2 not run)
- Invalid types in specific rows (e.g. `text[]` columns receiving non-array text)

> **Note**: `wc -l` on the CSV is misleading — fields with embedded newlines (descriptions, JSON payloads) inflate the line count. Trust the dashboard's row count over `wc -l`.

### Step 4 — Link scholarships to the school

After the rows are inserted, link them to the corresponding `schools` row via the junction table:

```sql
INSERT INTO public.scholarship_schools (scholarship_id, school_id)
SELECT s.id, sc.id
FROM public.scholarships s
JOIN public.schools sc ON sc.name ILIKE '%<School Name Pattern>%'
WHERE s.source_id = '<source_uuid>'
ON CONFLICT DO NOTHING;
```

Example for UNSW:
```sql
INSERT INTO public.scholarship_schools (scholarship_id, school_id)
SELECT s.id, sc.id
FROM public.scholarships s
JOIN public.schools sc ON sc.name ILIKE '%University of New South Wales%'
WHERE s.source_id = 'c2d4e6f8-1a3b-5c7d-9e0f-2b4c6d8e0f1a'
ON CONFLICT DO NOTHING;
```

If the school doesn't exist in `public.schools`, insert it first or this step inserts zero rows.

---

## Common import errors

| Error | Cause | Fix |
|-------|-------|-----|
| `FK violation on source_id` | Source not in `scholarship_sources` | Run Step 2 |
| `INSERT has more target columns than expressions` | Column count mismatch in manual SQL | Recount columns and values |
| `invalid input syntax for type text[]` | Array column in DB is `text[]` but CSV has JSON | Convert `["x","y"]` → `{x,y}` in CSV, or change column to `jsonb` |
| `invalid input syntax for type numeric` | Empty string in numeric column | CSV has `0` or non-empty placeholder; ensure truly empty |
| Preview shows fewer rows than CSV has | Silent validation failures | Check dashboard logs; usually array/numeric/timestamp issues |

---

## Re-imports and updates

The dashboard CSV import does **not** upsert — re-running on the same data hits unique-constraint errors on `id` or `(source_id, external_id)`.

To re-import:
1. Delete prior rows: `DELETE FROM public.scholarships WHERE source_id = '<source_uuid>';`
2. Re-run the CSV import.

For incremental updates without deleting, use the SQL Editor with explicit `INSERT ... ON CONFLICT (source_id, external_id) DO UPDATE SET ...` instead of the dashboard UI.

---

## Reference: existing seeds

A working SQL-seed example for one scholarship lives at:
`/Users/kevinhu/Caat_V2/caat-frontend/supabase/seeds/scholarships_unimelb.sql`

It demonstrates:
- Source upsert by slug
- Resolving `school_id` via `ilike` lookup
- Scholarship insert with `on conflict (source_id, external_id) do update`
- Junction table linkage in a single `do $$ ... $$` block

Use this as the template if you need to generate SQL inserts (instead of CSV import) — necessary when:
- The CSV exceeds the dashboard's silent validation limits
- You need bulk upsert behaviour
- You want array values written as native `array['x','y']` rather than JSON strings
