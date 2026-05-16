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

*(All scraper columns now map to a real DB column. Previously
`citizenships` was stripped, but it was added to `public.scholarships`
as `text[]` in migration `20260516120000_add_citizenships_to_scholarships.sql`
to power the frontend's Domestic / International filter — the scraper's
existing writes flow through unchanged.)*

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

## Bundle detection — LLM classifier on every page

Multiple uni sites publish "index" pages where one URL describes 2+ distinct sub-awards (e.g. UNSW's "Exchange Awards" page lists 8 separate awards each with their own name, code, and dollar amount). Importing such a page as one row produces a visually broken card (long checklist of mixed bullets + titles + prices) and makes filtering, bookmarks, and detail URLs incoherent.

**Rule:** every scraper must classify each page as single-or-bundled at scrape time, and for bundles either split into N rows (one per sub-award) or skip + log. Never silently emit one row for a bundled page.

### Architecture: LLM classifier is primary, regex is audit-only

The scraper calls `requirements_extractor.classify_page(title, outline, eligibility, selection)` for every page. The model returns `{is_bundle: bool, reason: str}`. That verdict is the source of truth for the control flow:

```
classify_page → is_bundle?
  ├── False → emit parent_row as a single scholarship
  └── True  → call split_bundle()
                ├── sub_awards has 1+ items → emit N rows (one per sub-award)
                └── sub_awards empty       → skip + log (aggregator page)
```

The regex `detect_bundle()` still runs alongside the LLM call but is **audit-only** — disagreements are logged so we can spot drift, but the regex never gates the control flow. If the LLM call fails (no API key, network blip, model errored), the regex provides a fallback verdict so the scraper still works offline.

**Why LLM-first:** the regex was over-eager on the "multi-amount" and "multi-code" signals — it couldn't tell *enumerated sub-awards* apart from *tiered amounts mentioned in one award* (e.g. the New Colombo Plan has 3 grant amounts but is one program). And it under-fired on aggregator-style pages where bundles are stated in prose without obvious structural markers. The LLM handles both cases naturally given the full page context.

**Why keep the regex:**
- Free, deterministic offline fallback when the API is unavailable.
- Audit signal — when the regex says "bundle" and the LLM says "single" (or vice versa), the disagreement gets logged so a future scraper author can sanity-check.
- Quick local development without burning API calls.

### Cost

Per scrape: one `classify_page` call per page (~1500 input tokens × ~$0.0003 = $0.0003 per page). UNSW's 87 pages cost ~$0.025 for classification. Plus splitter calls for the small subset flagged as bundles (~$0.01) and extractor calls for kept rows (~$0.025). **Total: ~$0.06 per full UNSW re-scrape**, all cached, so subsequent runs hit the cache and cost nothing.

### Cross-uni portability

The classifier prompt is uni-agnostic — it describes the difference between single and bundled pages structurally, not in terms of UNSW conventions. The same `classify_page()` call works against any uni's pages without modification.

The regex `detect_bundle()` is uni-specific because it depends on `AWARD_CODE_RE`. Each new scraper declares its own regex — see next section.

### Anti-patterns to avoid (lessons learned)

- **Don't gate on "N Scholarships available".** UNSW (and several other unis) use this phrasing to mean "N recipients of the same award", not "N distinct awards".
- **Don't use a generic `[A-Z]{2,}\d+` regex for award codes.** It catches course codes (`LAWS3361`, `COMP1511`) mentioned in eligibility prose, producing false positives. Per-uni award-code regexes must be specific to the uni's actual award-code shape.
- **Don't gate on "2 distinct amounts".** Many single-award pages quote both a principal and a stipend, or "up to $X / minimum $Y / total $Z". The threshold to reduce false positives was found empirically to be 3+, but even that misses real bundles where all sub-awards have the same amount — which is why LLM classification is now primary.
- **Don't gate on bare "considered for the".** Single-award pages routinely say "applicants will be considered for the [single award name]". Only the LLM (or a tightly-bounded phrase regex requiring `scholarships|awards|external` adjacent) handles this safely.
- **Don't rely on title parentheticals for sub-award differentiation.** When a bundle splits into siblings like `Award (Indigenous)` and `Award (Refugee)`, UNSW's source page often uses identical eligibility prose for both — the differentiator is only in the title. The classifier+splitter does the right thing structurally (separate rows with distinct codes); the identical must_meet is a known-acceptable artifact of the source data.

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

### Splitting bundles into one row per sub-award (preferred)

For bundle pages that *enumerate* their sub-awards on the UNSW page itself (codes + amounts + per-sub-award eligibility printed in the outline table), the scraper calls `split_bundle()` in `requirements_extractor.py` and emits one DB row per sub-award instead of skipping. This recovers the ~50+ real scholarships that would otherwise be lost across the 16 detected bundles.

**Implementation:**

```python
# unsw_scraper.py — pseudocode for the scrape_scholarship flow
parent_row = build_parent_row(...)  # everything up to and including the dict literal
bundle_reason = detect_bundle(hf, outline, eligibility, selection)
if not bundle_reason:
    return parent_row

sub_rows = build_sub_rows(parent_row, title, outline, eligibility, selection)
if sub_rows is None:
    return {"_skipped": True, ...}   # aggregator page, falls back to skip+log
return {"_sub_awards": sub_rows, ...}  # main() writes N rows
```

**Each sub-row:**

| Field | Source |
|---|---|
| `id` | Fresh UUID4 |
| `external_id` | Sub-award code from LLM (e.g. `PUCE1025`), or `<parent_external_id>-<idx>` fallback |
| `external_url` | Parent URL + `#<code>` fragment (links back to the index page) |
| `slug` | `_slugify(sub_title)` with parent fallback |
| `title`, `amount_*` | From LLM extraction |
| `eligibility_summary` | LLM's per-sub-award `must_meet` joined by newlines |
| `application_requirements` | LLM's `{application_mode, separate_application_required, must_meet}` + `parent_url`/`parent_title` for audit |
| Everything else | Inherited from `parent_row` (provider, country, term, study_level, etc.) |

**LLM splitter contract** — `split_bundle(title, outline, eligibility, selection)` returns:

```python
{
    "application_mode": "manual" | "automatic",
    "separate_application_required": bool,
    "sub_awards": [
        {"title": str, "code"?: str, "amount_display"?: str,
         "amount_value"?: float, "must_meet": [str, ...]},
        ...
    ]
}
```

Empty `sub_awards` means the page is an aggregator (Graduate Women style — only links to scholarships hosted elsewhere). Caller falls back to skip+log.

**When the splitter fails:**
- API key missing / network error → `None` → skip+log (same as before)
- Page is genuinely an aggregator → empty `sub_awards` → skip+log
- Splitter ran but returned partial/messy data → still emit; raw_payload preserves the original sections for audit

**Cost:** ~16 splitter calls per UNSW scrape × ~$0.0005 = ~$0.008. Cached on `(model, title, outline, eligibility, selection)` SHA-256 under `australia/cache/split/`, so re-scrapes are free.

### What to do with the bundled-pages log

`unsw_scraper.py` writes skipped bundles to `australia/unsw_bundled_pages.csv`. Treat that file as a backlog:
- For pages worth importing as multiple rows, write a per-site splitter that fetches each URL, chunks the eligibility text on the `Title (CODE)` pattern, and emits N rows.
- For pages not worth splitting (informational hubs, no funded sub-awards), leave them skipped.

### Re-applying detection + splitting after a scrape has already run

If you tighten `detect_bundle()` or improve `split_bundle()` after a full scrape and don't want to re-fetch every page, use `filter_bundles.py`:

```sh
python3 filter_bundles.py
# Reads:  australia/unsw_scholarships.csv
# Writes: australia/unsw_scholarships.filtered.csv      ← singles + sub-awards
#         australia/unsw_bundled_pages.filtered.csv     ← unsplittable aggregators
```

The filter reads each row's `raw_payload.outline / eligibility / selection`, re-runs the current `detect_bundle()`, and for bundled rows calls `build_sub_rows()` (which calls the LLM splitter). No HTTP — the splitter is the only thing that hits the API, and only for the small subset of rows flagged as bundled. Useful when the scraper has finished but you've since added a signal, fixed a false negative, or improved the splitter prompt.

---

## LLM-powered scraping (classify / split / extract)

`requirements_extractor.py` exposes three LLM-backed functions, all using the Anthropic SDK pointed at DeepSeek's Anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`). The same code works against Anthropic's first-party API by overriding `EXTRACTOR_BASE_URL` and `EXTRACTOR_MODEL` env vars — only the endpoint and model string change.

| Function | Called when | Returns | Purpose |
|---|---|---|---|
| `classify_page(title, outline, eligibility, selection)` | Every page | `{is_bundle: bool, reason: str}` | Source of truth for single vs. bundle. Replaces the regex `detect_bundle()` in the control flow. |
| `split_bundle(title, outline, eligibility, selection)` | Pages classified as bundles | `{application_mode, separate_application_required, sub_awards: [{title, code?, amount_*, must_meet}]}` | Enumerates the sub-awards on a bundled page so the scraper can emit one row per sub-award. Empty `sub_awards` = aggregator page (skip + log). |
| `extract_requirements(eligibility, application)` | Every single-scholarship row | `{application_mode, separate_application_required, must_meet}` | Structured eligibility for the detail page's green-tick checklist UI. The detail page renders these three keys from `application_requirements`. |

All three use:
- **Tool-use, not free-form JSON.** Single tool per call, the model is prompted to invoke it; `block.input` comes back as a parsed dict. No JSON parsing failure modes.
- **Anti-hallucination system prompts.** "Only what's stated, don't infer."
- **SHA-256 disk cache** under `australia/cache/{classify,split,extract}/`, keyed by `(model, input parts)`. Re-runs are free unless source pages change.
- **Graceful fallback** when no API key is set: classifier returns `None` (scraper falls back to regex), splitter returns `None` (scraper falls back to skip-and-log), extractor returns `None` (scraper falls back to raw eligibility text).

### Env vars

The scraper auto-loads a `.env` file next to `unsw_scraper.py`. Put the key there once, no need to `source` it before each run:

```sh
# .env (gitignored)
DEEPSEEK_API_KEY=sk-...
# EXTRACTOR_MODEL=deepseek-chat         # default — Anthropic-shape tool use works
# EXTRACTOR_BASE_URL=https://api.deepseek.com/anthropic   # default
```

The loader prefers `python-dotenv` when installed and falls back to a tiny built-in parser otherwise. Existing shell env vars take precedence over `.env` values.

If no key is set, `extract_requirements()` returns `None` and the scraper falls back to writing the raw eligibility/selection text into `application_requirements` (the previous behaviour). Scrapes still run, just without the structured `must_meet` list.

### Model compatibility on DeepSeek's Anthropic-compat endpoint

Tested 2026-05: only `deepseek-chat` reliably accepts Anthropic-shape tool use against `https://api.deepseek.com/anthropic`. Caveats:

- `deepseek-v4-flash` / `deepseek-v4-pro`: at the time of writing the Anthropic-compat layer silently routes these to `deepseek-reasoner` (the error response gives away the actual model). Use the OpenAI-format endpoint if you need v4.
- `deepseek-reasoner`: rejects `tool_choice: {"type": "tool", "name": "..."}` and `{"type": "any"}` with a 400. The extractor avoids both forms and relies on the system prompt + a single-tool surface to ensure the tool is called.
- `deepseek-chat` (current default): supports the standard tool-use shape end-to-end. Deprecating per DeepSeek's docs, so plan to migrate once their Anthropic-compat layer adds the v4 models or relaxes `tool_choice` on reasoner.
- First-party Anthropic: any current model works unchanged — override `EXTRACTOR_BASE_URL` to `https://api.anthropic.com` and `EXTRACTOR_MODEL` to e.g. `claude-haiku-4-5`.

### Design notes

- **Tool-use, not free-form JSON.** The model is forced to call `record_requirements` (single tool, `tool_choice` pinned), so we never have to parse free-form JSON from a text block. The Anthropic SDK gives back `block.input` as a parsed dict.
- **Pre-extracted text, not raw HTML.** We pass the already-parsed `eligibility` and `selection` sections — ~1–2KB per page vs. 50–200KB of HTML, and the signal is much cleaner.
- **System prompt is anti-hallucination.** "Only what the page explicitly states. Omit, don't infer." We also save the raw text alongside the LLM output in `application_requirements` so an audit can spot drift.
- **On-disk SHA-256 cache.** `australia/cache/extraction/<hash>.json` keyed on `(model, eligibility, selection)`. Re-scrapes pay the API only on genuinely new/changed pages. Delete the directory to force re-extraction.
- **Prompt-cache hint on the system prompt.** Harmless if DeepSeek's compat layer ignores it; a small win if it honours it across calls.

### Reusing the extractor for other uni scrapers

`requirements_extractor.extract_requirements(eligibility_text, application_text)` is uni-agnostic. To wire it into a new scraper:

```python
from requirements_extractor import extract_requirements

application_requirements = {}
extracted = extract_requirements(eligibility, application_or_selection_text)
if extracted:
    application_requirements.update(extracted)
# Always keep the raw text alongside for audit
if eligibility:
    application_requirements["eligibility_text"] = eligibility
```

The only thing per-uni is *which* page sections you pass in. The extraction shape is fixed by the DB schema.

---

## Adding a new uni scraper — checklist

When wiring up a scraper for a uni that hasn't been covered yet (UniMelb, USyd, Monash, ANU, UQ, etc.), work the checklist in order. The earlier items unblock the later ones.

### 1. Survey the source site (10–20 sample pages, ~30 min)

Pick a spread of pages: an undergrad cash award, a postgrad coursework, an exchange/travel grant, a closed scholarship, a bundled "Exchange Awards"-style page if one exists. For each, note:

- **Award-code format.** Does this uni mint scholarship codes? What shape? (UNSW: `UGCA1392` / `PGCE2017`. UniMelb: 6–7 digit numeric. USyd: ???). Faculty/course codes (`LAWS3361`, `COMP1511`, etc.) are NOT award codes — your regex must reject them.
- **Page sections.** Find the eligibility section (might be called "Eligibility", "Who can apply", "Selection criteria") and the application section ("How to apply", "Application process"). Note the exact heading text — the scraper will use it.
- **Bundled pages.** Search for "scholarships" or "awards" plural in titles, look for pages listing multiple `Title (CODE) — $X` rows. Note the URL pattern.
- **Apply-link presence.** Is there an explicit "Apply" button with a URL, or is application automatic? The scraper should pull this when present.

### 2. Define `AWARD_CODE_RE`

Per `unsw_scraper.py:24`, declare a module-level constant matching only this uni's award codes — never a generic `[A-Z]{2,}\d+`. Make the regex tight enough to reject faculty/course codes mentioned in eligibility prose.

```python
# unsw_scraper.py
AWARD_CODE_RE = re.compile(r'\b((?:UG|PG|PU)[A-Z]{2}\d{3,5})\b')

# unimelb_scraper.py (hypothetical)
AWARD_CODE_RE = re.compile(r'\b\d{6,7}\b')  # numeric IDs only
```

Use the same regex for both `external_id` extraction and bundle detection — see the existing "Bundle detection" section.

### 3. Fallback for `external_id` when no award code is found

Many real scholarship pages don't carry a uni-style award code (especially when the scholarship is sponsored externally, e.g. TagEnergy, ANZ, alumni-named awards). The scraper should fall back to a stable URL-derived ID rather than dropping the row or inheriting a junk match.

```python
"external_id": hf.get("code") or slug,  # award code preferred, else URL slug
```

`slug` should be derived from the URL path so it's unique and stable across re-scrapes. UNSW uses `/scholarships/id/<numeric>`; the slug is just that numeric ID.

### 4. Wire up the LLM classifier + splitter (regex stays as fallback)

The primary bundle detector is `requirements_extractor.classify_page()` — call it on every page. The regex `detect_bundle()` is uni-specific (depends on `AWARD_CODE_RE`) and runs alongside as an offline fallback when the API is unavailable plus an audit signal for spotting regressions.

For bundled pages, prefer **splitting** over skipping. Reuse `build_sub_rows(parent_row, title, outline, eligibility, selection)` from `unsw_scraper.py` — it calls `requirements_extractor.split_bundle()` and emits one row per sub-award, inheriting provider/country/term from the parent and overriding the per-sub-award fields. Pages that the splitter can't enumerate (true aggregator pages that only link out) still go to `australia/<uni>_bundled_pages.csv` as a backlog.

The full scrape-time flow for each page:

```python
parent_row = build_parent_row(...)
classification = classify_page(title, outline, eligibility, selection)
regex_reason = detect_bundle(hf, outline, eligibility, selection)  # audit-only

is_bundle = (classification.get("is_bundle") if classification
             else bool(regex_reason))  # regex fallback if API unavailable

if not is_bundle:
    return parent_row

sub_rows = build_sub_rows(parent_row, title, outline, eligibility, selection)
return sub_rows if sub_rows else {"_skipped": True, ...}
```

See `unsw_scraper.py:scrape_scholarship` for the exact pattern.

### 5. Wire up the LLM extractor

Drop the standard three-line invocation into the row-construction function:

```python
from requirements_extractor import extract_requirements

application_requirements = {}
extracted = extract_requirements(eligibility, application_or_selection_text)
if extracted:
    application_requirements.update(extracted)
if eligibility:
    application_requirements["eligibility_text"] = eligibility  # audit trail
```

The extractor is uni-agnostic. The system prompt is anti-hallucination; only what's on the page makes it into `must_meet`.

### 6. Pick a `source_id` (UUID v4)

Generate once, hardcode in the scraper as `SOURCE_ID`. Add an entry in this doc so the next session doesn't pick a conflicting one. Existing assignments:

- UNSW: `c2d4e6f8-1a3b-5c7d-9e0f-2b4c6d8e0f1a`
- USyd: `b5e8c3a1-7d4f-4e2a-9b1c-6f3a8d5b2c7e`
- UniMelb: see `caat-frontend/supabase/seeds/scholarships_unimelb.sql`

### 7. Verify the `schools` row exists

Step 4 of the import workflow joins on `schools.name ILIKE '%<pattern>%'`. If the uni isn't in `public.schools` yet, that join inserts zero rows. Check first:

```sql
SELECT id, name FROM public.schools WHERE name ILIKE '%<uni name>%';
```

If empty, insert the school row before running Step 4 (not as part of the scraper — schools are managed manually).

### 8. Run on a 5-page sample first

Don't unleash the scraper on the full catalogue immediately. Hardcode 5 URLs of varied shapes (single award with code, single award without code, bundled page, closed, postgrad), inspect the CSV by hand. Look for:

- Every row has a non-null `external_id`
- Every active row has a non-null `amount_value` or `amount_display`
- At least some rows have populated `application_requirements.must_meet` (proves LLM extractor is firing)
- The bundled page is in the `_bundled_pages.csv`, not the main CSV

If those check out, run the full scrape.

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

### Step 3 — Pre-import sanity checks (do this locally before clicking import)

Catching shape problems on disk is much cheaper than catching them after a half-failed import. Run these python one-liners against the CSV before uploading:

```sh
python3 -c "
import csv, json, re
with open('australia/<uni>_scholarships.csv') as f:
    rows = list(csv.DictReader(f))
print(f'Total rows:       {len(rows)}')
print(f'Have amount:      {sum(1 for r in rows if r[\"amount_value\"])}')
print(f'Have must_meet:   {sum(1 for r in rows if r[\"application_requirements\"] and \"must_meet\" in json.loads(r[\"application_requirements\"]))}')
print(f'Have external_id: {sum(1 for r in rows if r[\"external_id\"])}')
prefixes = {}
for r in rows:
    m = re.match(r'^([A-Z]+)', r['external_id'] or '')
    p = m.group(1) if m else '(slug)'
    prefixes[p] = prefixes.get(p, 0) + 1
print(f'external_id prefixes: {dict(sorted(prefixes.items(), key=lambda x: -x[1]))}')
"
```

Red flags to look for in the output:
- **Any rows with empty `external_id`** — the import will succeed but `(source_id, external_id)` uniqueness lets duplicates through on re-scrape.
- **Faculty/course-code prefixes in the `external_id` breakdown** (e.g. `LAWS`, `COMP`, `ARTS`, `ECON`, `MATH`) — your `AWARD_CODE_RE` is too loose. Tighten it before importing.
- **`Have must_meet` is zero** — the LLM extractor didn't fire. Either the API key isn't loaded, the model rejected every call, or every page had empty `eligibility`. Check the scraper stderr for `extractor: ...` messages.
- **`Have amount` is suspiciously low** — `parse_amount` probably isn't matching the uni's format. Inspect a few `value_raw` fields from `raw_payload`.

### Step 4 — Import the CSV

Open the Supabase dashboard → **Table Editor** → `scholarships` table → **Insert** → **Import data from CSV**.

Upload the CSV. The preview will show the actual row count being imported. If the preview shows fewer rows than expected, check for:
- Unmatched headers (typos vs. table columns)
- Missing source row (Step 2 not run)
- Invalid types in specific rows (e.g. `text[]` columns receiving non-array text)

> **Note**: `wc -l` on the CSV is misleading — fields with embedded newlines (descriptions, JSON payloads) inflate the line count. Trust the dashboard's row count over `wc -l`.

### Step 5 — Link scholarships to the school

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

The dashboard CSV import does **not** upsert — re-running on the same data hits unique-constraint errors on `id` or `(source_id, external_id)`. Two paths:

### Path A: Delete + re-import (the simple case)

Use this when the new scrape changes shape (different `external_id` strategy, new columns, bundles filtered out at scrape time) and matching against existing rows by ID is unreliable.

```sql
-- 1. Clear junction rows first. Safe even if ON DELETE CASCADE is set —
--    just becomes a no-op. The junction-table FK definition has been
--    scrubbed from local migrations, so don't assume cascade.
DELETE FROM public.scholarship_schools
WHERE scholarship_id IN (
  SELECT id FROM public.scholarships WHERE source_id = '<source_uuid>'
);

-- 2. Clear scholarships
DELETE FROM public.scholarships
WHERE source_id = '<source_uuid>';
```

Then re-run the import workflow (Steps 3 → 5). The LLM-extraction on-disk cache (`australia/cache/extraction/`) survives the DB delete, so re-scraping is fast and ~free.

### Path B: Upsert (incremental updates)

Use this when you want to preserve existing IDs (e.g. user bookmarks, foreign keys from other tables) and only change rows that meaningfully changed. Don't use the dashboard UI — write `INSERT ... ON CONFLICT (source_id, external_id) DO UPDATE SET ...` SQL. The shape is in `caat-frontend/supabase/seeds/scholarships_unimelb.sql`.

### Verification queries after re-import

```sql
-- No bundled pages should remain (UNSW-specific regex; adapt per uni)
SELECT count(*)
FROM public.scholarships
WHERE source_id = '<source_uuid>'
  AND (
    SELECT count(DISTINCT m[1])
    FROM regexp_matches(eligibility_summary, '\(((?:UG|PG|PU)[A-Z]{2}\d{3,5})\)', 'g') AS m
  ) >= 2;
-- Expect: 0

-- No junk course-code prefixes in external_id (UNSW: only UG/PG/PU should appear)
SELECT DISTINCT substring(external_id from '^[A-Z]+') AS prefix, count(*)
FROM public.scholarships
WHERE source_id = '<source_uuid>'
GROUP BY 1
ORDER BY 2 DESC;

-- Spot-check that LLM extraction landed
SELECT title, application_requirements->>'application_mode' AS mode,
       application_requirements->'must_meet' AS must_meet
FROM public.scholarships
WHERE source_id = '<source_uuid>'
  AND application_requirements ? 'must_meet'
LIMIT 10;
```

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
