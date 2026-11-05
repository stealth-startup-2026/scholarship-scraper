# PROD-151: truthful scraper timestamps and offline CI

Resume PR #2. Scraping a page must not imply it was verified against a previous
CAAT baseline. Preserve the import column order, leave `last_verified_at` empty,
and record each successful page fetch in `source_last_synced_at`. Re-import
guidance must preserve scholarship IDs, bookmarks, creation dates, and existing
verification history.

## Task 1: validate the existing implementation

- Run the expanded offline tests against a copied `origin/main` baseline and
  observe the false verification timestamps fail.
- Run against this branch and check all six tests pass.
- Include successful per-page timestamps, failed fetches, and bundle inheritance.
- Keep copies and dependencies under `~/Archive.noindex/caat/scraper-qa`.

Expected: baseline fails because it stamps verification without baseline evidence;
candidate passes. No live HTTP fetch, model call, import, or production write.

## Task 2: make the checks repeatable

- Pin test-only Python dependencies and document the offline command.
- Add read-only GitHub Actions on PRs/main for Python 3.12 and 3.14, with immutable
  action commits and a bounded runtime.
- Run Claude and independent whole-branch reviews; address consequential findings
  once with regression coverage and rerun all tests.
- Merge only after both CI variants and reviews pass, then verify main CI.

Expected: fresh CI passes using only synthetic data and no secrets.

## Review focus

Check both direct and bundle output paths, actual fetch time versus run start,
failed fetch handling, unchanged CSV shape, and the documented upsert preservation
of IDs/created_at/last_verified_at. Check CI dependency compatibility and absence
of credentials or live service calls. Existing extraction algorithms and source
coverage are not changed; this does not verify the production catalogue.

## Deferred work

PROD-143 owns the separate source refresh/baseline process. This PR does not apply
freshness timestamps, fix blocked university sites, or rewrite imported rows.
