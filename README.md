# Scholarship Scraper

Scrapers and cleanup utilities for importing university scholarship data into
the Caat_V2 Supabase `public.scholarships` table.

The repository has three main layers:

- `unsw/unsw_scraper.py` and `usyd/usyd_scraper.py` fetch source pages and write importable
  CSVs under `australia/`.
- `requirements_extractor.py` owns all LLM calls for page classification,
  bundle splitting, requirement extraction, and QA. Calls are cached under
  `australia/cache/`.
- `scholarship_common.py` contains shared CSV schema, bundle fallback detection,
  slugging, and sub-award row construction used by multiple scrapers.

## Quick Start

```sh
# Small USyd sample, fresh output
python3 usyd/usyd_scraper.py 5

# Full USyd run, resuming from the existing CSV
python3 usyd/usyd_scraper.py

# Full UNSW run
python3 unsw/unsw_scraper.py

# Re-apply current bundle detection/splitting to an existing UNSW CSV
python3 unsw/filter_bundles.py
```

Set `DEEPSEEK_API_KEY` in `.env` to enable LLM classification/extraction. Without
an API key, scrapers still run with regex fallback and leave some structured
fields empty.

## Outputs

- UNSW: `australia/unsw_scholarships.csv`
- UNSW unsplittable bundles: `australia/unsw_bundled_pages.csv`
- USyd: `australia/usyd/usyd_scholarships.csv`
- USyd unsplittable bundles: `australia/usyd/usyd_bundled_pages.csv`

The CSV column order is defined once in `scholarship_common.OUTPUT_FIELDNAMES`.

## Docs

- `CLAUDE.md` / `AGENTS.md`: detailed Supabase import guide and scraper rules.
- `example_schema.csv`: example import schema.

## Script Inventory

| Script | Purpose |
| --- | --- |
| `unsw/unsw_scraper.py` | Scrapes UNSW scholarship pages and writes `australia/unsw_scholarships.csv`. |
| `usyd/usyd_scraper.py` | Scrapes the USyd AEM feed/detail pages and writes `australia/usyd/usyd_scholarships.csv`. |
| `requirements_extractor.py` | Shared LLM classify/split/extract/verify client. |
| `scholarship_common.py` | Shared CSV schema, slugging, regex bundle fallback, and sub-award row builder. |
| `unsw/filter_bundles.py` | Re-runs current UNSW bundle detection/splitting against an existing CSV. |
| `usyd/prep_usyd_amount_csv.py` | Builds clean USyd amount-normalisation input from `raw_payload.benefits`. |
| `usyd/summarize_amounts.py` | Shortens long `amount_display` values and generates update SQL. |
| `usyd/retry_misses.py` | Re-runs strict amount summarisation for simple-pass misses. |
| `unsw/verify_international.py` / `usyd/verify_usyd_international.py` | Verify international eligibility. |
| `unsw/compare_international.py` | Compares baseline and LLM international eligibility verdicts. |
| `unsw/gen_citizenships_update_sql.py` | Generates Supabase SQL to update `citizenships` from verification results. |

## Adding a Scraper

Keep scraper-specific code in a new `<uni>_scraper.py` file and reuse:

- `scholarship_common.OUTPUT_FIELDNAMES`
- `scholarship_common.detect_bundle(AWARD_CODE_RE, ...)` for regex audit/fallback
- `scholarship_common.build_sub_rows(...)` for LLM-split bundle pages
- `requirements_extractor.extract_requirements(...)` for detail-page requirements

Each scraper should define its own `SOURCE_ID`, source URLs, `AWARD_CODE_RE`,
page-section parsing, and field inference rules.
