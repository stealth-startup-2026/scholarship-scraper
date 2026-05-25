#!/usr/bin/env python3
"""
Scraper for University of Sydney scholarship listings.
https://www.sydney.edu.au/scholarships/search.html

USyd's search page is JS-rendered, but the underlying AEM JSON feed at
`/content/corporate/scholarships/search/jcr:content/par-zone2/scholarship_filter.scholarship-data.json`
gives us 1,059 scholarship records with title/url/description/tags. We
fetch that once, then hit each detail page for the full eligibility,
amount, and application info.

Outputs to `australia/usyd/usyd_scholarships.csv` (same schema as UNSW
so they import into the same Supabase table).
"""

import csv
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from requirements_extractor import classify_page, extract_requirements
from unsw_scraper import _slugify, build_sub_rows, detect_bundle, OUTPUT_FIELDNAMES

SOURCE_ID = "b5e8c3a1-7d4f-4e2a-9b1c-6f3a8d5b2c7e"
SOURCE_SLUG = "usyd-official"
BASE_URL = "https://www.sydney.edu.au"
SEARCH_DATA_URL = (
    f"{BASE_URL}/content/corporate/scholarships/search/jcr:content/"
    "par-zone2/scholarship_filter.scholarship-data.json"
)
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00")

EXCLUDED_EMAIL_PATTERNS = ["foundation", "alumni", "giving", "philanthropy"]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ScholarshipScraper/1.0)",
    "Accept": "text/html,application/json,application/xhtml+xml",
})

# USyd doesn't publish award codes on scholarship pages; external_id falls
# back to the URL slug, which is stable per scholarship.
AWARD_CODE_RE = re.compile(r"a^")  # never matches — placeholder for symmetry with unsw_scraper

_NON_INFORMATIVE_AMOUNTS = {
    "not specified", "n/a", "na", "tba", "to be advised",
    "to be confirmed", "varies", "refer to handbook", "see details",
}


def parse_amount(raw: str) -> tuple[float | None, str | None]:
    """Parse the first $X amount out of free text. Reject 'Not specified'
    and similar placeholders so the UI doesn't render them as a headline.

    For display, return the full line (sentence) containing the dollar match,
    capped at 120 chars so card UI stays compact."""
    if not raw:
        return None, None
    match = re.search(r"\$([0-9][0-9,]*)", raw)
    if match:
        numeric = float(match.group(1).replace(",", ""))
        # Find the natural-text boundary around the match (line in joined text).
        line_start = raw.rfind("\n", 0, match.start()) + 1
        line_end = raw.find("\n", match.end())
        if line_end == -1:
            line_end = len(raw)
        line = raw[line_start:line_end].strip()
        if len(line) > 120:
            line = line[:117] + "..."
        return numeric, line
    norm = raw.strip().lower()
    if not norm or norm in _NON_INFORMATIVE_AMOUNTS:
        return None, None
    return None, raw.strip().splitlines()[0]


def _norm_heading(s: str) -> str:
    """Normalize a heading for comparison: lowercase, strip apostrophes/punct,
    collapse whitespace. So 'Who’s eligible' / 'Who is eligible' /
    'Whos eligible' all compare equal."""
    s = re.sub(r"[‘’']", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


_DATE_FORMATS = ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%Y-%m-%d")


def parse_highlights_date(cell: str) -> str | None:
    """Convert a Highlights-table date cell like '23 March 2026' or '14 Apr 2026'
    to an ISO-8601 date string ('2026-03-23'). Returns None for non-dates
    (e.g. 'Ongoing', 'TBA', empty cells)."""
    if not cell:
        return None
    s = re.sub(r"\s+", " ", cell.strip())
    if not s or s.lower() in {"tba", "ongoing", "n/a", "to be confirmed"}:
        return None
    from datetime import datetime
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_highlights_table(soup: BeautifulSoup) -> dict[str, str]:
    """USyd scholarship pages often have a 'Highlights' table with columns
    Value | Eligibility | Open date | Close date. The 'Value' cell is a clean,
    card-friendly headline ('USYD RTP rate p.a. (up to 3.5 years)',
    '$5,000 / year for one year') — much better than parsing the verbose
    Benefits paragraph.

    Returns {header_norm: cell_text} or {} when no table is found. Header keys
    are lowercased & whitespace-normalised so callers can look up 'value',
    'open date', etc. reliably.
    """
    for h in soup.find_all(["h2", "h3"]):
        if _norm_heading(h.get_text(strip=True)) != _norm_heading("Highlights"):
            continue
        # Walk forward to the next table before the next heading.
        for sib in h.find_all_next(["table", "h2", "h3"]):
            if sib.name in ("h2", "h3") and sib is not h:
                break
            if sib.name == "table":
                headers = [
                    re.sub(r"\s+", " ", th.get_text(" ", strip=True)).lower()
                    for th in sib.find_all("th")
                ]
                # Use the first data row only (USyd's Highlights table has one row).
                first_tr = next(
                    (tr for tr in sib.find_all("tr") if tr.find("td")), None,
                )
                if not first_tr or not headers:
                    return {}
                cells = [
                    td.get_text(" ", strip=True) for td in first_tr.find_all("td")
                ]
                # Align by index, tolerating mismatched lengths.
                return {
                    headers[i]: cells[i]
                    for i in range(min(len(headers), len(cells)))
                    if cells[i]
                }
        break
    return {}


def get_section_text(soup: BeautifulSoup, headings: str | list[str]) -> str:
    """USyd uses h2/h3 section headings followed by paragraphs/lists. Accepts
    either a single heading or a list of acceptable variants — the first one
    that matches wins. Walk forward until the next h2/h3, collecting text.

    USyd templates vary: Eligibility is sometimes 'Who's eligible',
    sometimes 'Who is eligible', sometimes 'Eligibility' or 'Requirements'.
    Benefits is sometimes 'Benefits and duration'. Pass all variants."""
    if isinstance(headings, str):
        wanted = {_norm_heading(headings)}
    else:
        wanted = {_norm_heading(h) for h in headings}

    for h in soup.find_all(["h2", "h3"]):
        if _norm_heading(h.get_text(strip=True)) in wanted:
            parts: list[str] = []
            for sib in h.find_all_next(["p", "li", "h2", "h3"]):
                if sib.name in ("h2", "h3") and sib is not h:
                    break
                txt = sib.get_text(" ", strip=True)
                if txt:
                    parts.append(txt)
            return "\n".join(parts)
    return ""


def filter_contact_emails(emails: list[str]) -> list[str]:
    out = []
    for e in emails:
        lower = e.lower()
        if not any(pat in lower for pat in EXCLUDED_EMAIL_PATTERNS):
            out.append(e)
    return out


def tags_to_dict(tags: list[dict]) -> dict[str, list[str]]:
    """USyd tags look like
       {'name': 'Undergraduate', 'value': 'scholarships:study-level/undergraduate'}
    Group by the category portion (between 'scholarships:' and '/')."""
    out: dict[str, list[str]] = {}
    for t in tags or []:
        val = t.get("value", "")
        if "/" not in val:
            continue
        cat = val.split("/", 1)[0].replace("scholarships:", "")
        out.setdefault(cat, []).append(t.get("name", ""))
    return out


def infer_study_level(td: dict[str, list[str]]) -> list[str]:
    levels: list[str] = []
    for n in td.get("study-level", []):
        low = n.lower()
        if "postgrad" in low:
            levels.append("postgraduate")
        elif "undergrad" in low or "honours" in low or "non-award" in low:
            levels.append("undergraduate")
    if not levels:
        levels.append("undergraduate")
    return list(dict.fromkeys(levels))


def infer_citizenships(td: dict[str, list[str]]) -> list[str]:
    cit: list[str] = []
    for n in td.get("student-type", []):
        low = n.lower()
        if low == "domestic":
            cit.append("AU")
        elif low == "international":
            cit.append("INTERNATIONAL")
    return list(dict.fromkeys(cit))


def infer_funding_type(benefits_text: str) -> list[str]:
    text = (benefits_text or "").lower()
    types: list[str] = []
    if any(w in text for w in ("tuition", "fee", "remission")):
        types.append("tuition")
    if any(w in text for w in ("living", "stipend", "accommodation")):
        types.append("living")
    if any(w in text for w in ("travel", "exchange", "overseas")):
        types.append("travel")
    if not types:
        types.append("cash")
    return types


def _url_to_slug(url: str) -> str | None:
    """Extract the slug from a USyd scholarship URL — matches the same logic
    used to derive external_id during scraping."""
    m = re.search(r"/scholarships/[a-z]/(.+?)\.html", url or "")
    return m.group(1) if m else None


def get_scholarship_listings() -> list[dict]:
    """Fetch the AEM JSON feed; returns a list of {name, title, url, description, tags} dicts.

    USyd indexes the same scholarship under multiple alphabetical directory
    letters (e.g. /scholarships/a/clissold-scholarship.html AND
    /scholarships/b/clissold-scholarship.html), which would produce duplicate
    (source_id, external_id) rows and fail the Supabase unique constraint.
    Dedupe by URL slug here so each scholarship gets scraped once."""
    resp = SESSION.get(SEARCH_DATA_URL, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    listings = resp.json()["scholarshipData"]
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in listings:
        slug = _url_to_slug(item.get("url", ""))
        if slug is None:
            deduped.append(item)  # keep non-matching URLs as-is
            continue
        if slug in seen:
            continue
        seen.add(slug)
        deduped.append(item)
    dropped = len(listings) - len(deduped)
    if dropped:
        print(f"Deduplicated {dropped} listings sharing a slug (USyd multi-letter buckets)")
    return deduped


def scrape_scholarship(listing: dict) -> dict | None:
    url = listing.get("url", "")
    if not url:
        return None

    try:
        resp = SESSION.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  HTTP error {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else listing.get("title", "")

    highlights = get_section_text(soup, ["Highlights"])
    highlights_table = parse_highlights_table(soup)
    how_to_apply = get_section_text(soup, ["How to apply", "Application"])
    benefits = get_section_text(soup, ["Benefits", "Benefits and duration", "Value", "Award value"])
    eligibility = get_section_text(soup, [
        "Who's eligible", "Who is eligible", "Eligibility", "Requirements", "Who can apply",
    ])
    background = get_section_text(soup, ["Background", "About", "About the scholarship"])

    raw_emails = [a["href"].replace("mailto:", "").strip()
                  for a in soup.find_all("a", href=re.compile(r"^mailto:", re.IGNORECASE))]
    contact_emails = filter_contact_emails(raw_emails)

    # Slug from URL (e.g. /scholarships/a/deans-outstanding-merit-scholarship.html -> deans-outstanding-merit-scholarship)
    slug_match = re.search(r"/scholarships/[a-z]/(.+?)\.html", url)
    slug = slug_match.group(1) if slug_match else url.rstrip("/").split("/")[-1].replace(".html", "")

    td = tags_to_dict(listing.get("tags", []))
    amount_value, amount_display = parse_amount(benefits)
    # Prefer the Highlights table's Value cell — it's the page's own card
    # headline, already short and authoritative. Fall back to parse_amount on
    # the Benefits paragraph only when the table is missing or has no Value.
    table_value = highlights_table.get("value", "").strip()
    if table_value:
        amount_display = table_value
        # Re-derive the numeric value from the table cell when possible.
        m = re.search(r"\$([0-9][0-9,]*)", table_value)
        if m:
            amount_value = float(m.group(1).replace(",", ""))
    study_levels = infer_study_level(td)
    funding_type = infer_funding_type(benefits)
    citizenships = infer_citizenships(td)
    eligible_countries = td.get("countries", []) or ["Australia"]

    description = background or listing.get("description") or None

    tags_out: list[str] = []
    for cat in ("area-of-study", "scholarship-types", "student-type"):
        for n in td.get(cat, []):
            tags_out.append(n.lower().replace(" ", "-"))
    tags_out = list(dict.fromkeys(tags_out))

    types_lower = [n.lower() for n in td.get("scholarship-types", [])]
    full_text = " ".join(filter(None, [title, eligibility, background, highlights])).lower()
    need_based = "equity" in types_lower or any(
        w in full_text for w in ("financial need", "need-based", "disadvantaged", "low income", "financial hardship")
    )
    merit_based = "merit" in types_lower or any(
        w in full_text for w in ("merit", "academic", "gpa", "achievement", "excellence")
    )
    requires_essay = "essay" in full_text or "personal statement" in full_text

    raw_payload = {
        "highlights": highlights,
        "highlights_table": highlights_table,
        "how_to_apply": how_to_apply,
        "benefits": benefits,
        "eligibility": eligibility,
        "background": background,
        "tags_raw": listing.get("tags", []),
        "source_url": url,
    }

    application_requirements: dict = {}
    extracted = extract_requirements(eligibility, how_to_apply)
    if extracted:
        application_requirements.update(extracted)
    if eligibility:
        application_requirements["eligibility_text"] = eligibility
    if how_to_apply:
        application_requirements["application_process"] = how_to_apply

    parent_row = {
        "id": str(uuid.uuid4()),
        "source_id": SOURCE_ID,
        "external_id": slug,
        "external_url": url,
        "slug": slug,
        "title": title,
        "provider_name": "University of Sydney",
        "description": description,
        "amount_value": amount_value,
        "amount_currency": "AUD",
        "amount_display": amount_display,
        "awards_count": None,
        "frequency": "yearly",
        "study_level": json.dumps(study_levels),
        "funding_type": json.dumps(funding_type),
        "citizenships": json.dumps(citizenships),
        "eligible_countries": json.dumps(eligible_countries),
        "excluded_countries": json.dumps([]),
        "eligible_genders": json.dumps([]),
        "minimum_gpa": None,
        "requires_essay": requires_essay,
        "need_based": need_based,
        "merit_based": merit_based,
        "school_name": "University of Sydney",
        "country": "Australia",
        # state_region encodes "scholarship restricted to this state", not
        # "institution is in this state". Most USyd scholarships have no state
        # restriction, so default to null. Set per-row only when eligibility
        # text explicitly limits to a state.
        "state_region": None,
        "application_open_at": parse_highlights_date(highlights_table.get("open date", "")),
        "deadline_at": parse_highlights_date(highlights_table.get("close date", "")),
        "start_term": None,
        "is_recurring": True,
        "is_active": True,
        "is_featured": False,
        "last_verified_at": NOW,
        "source_last_synced_at": NOW,
        "tags": json.dumps(tags_out),
        "eligibility_summary": eligibility or None,
        "application_requirements": json.dumps(application_requirements),
        "contact_info": json.dumps(contact_emails) if contact_emails else None,
        "raw_payload": json.dumps(raw_payload),
        "created_at": NOW,
        "updated_at": NOW,
    }

    # LLM classifier on every page. We pass background/eligibility/how-to-apply
    # as the three sections; benefits + highlights are appended to background
    # for richer context.
    classification = classify_page(
        title,
        outline=(background + "\n\n" + benefits).strip(),
        eligibility=eligibility,
        selection=how_to_apply,
    )
    # Regex audit fallback (USyd's AWARD_CODE_RE matches nothing, so this only
    # fires the phrase signal — useful for offline fallback when the API fails).
    regex_reason = detect_bundle({}, background + " " + benefits, eligibility, how_to_apply)

    if classification is None:
        is_bundle = bool(regex_reason)
        decision_reason = regex_reason or "classifier unavailable"
    else:
        is_bundle = bool(classification.get("is_bundle"))
        decision_reason = classification.get("reason", "")
        if regex_reason and bool(regex_reason) != is_bundle:
            print(f"  NOTE: regex/llm disagree (regex=bundle, llm={'bundle' if is_bundle else 'single'})")

    if not is_bundle:
        return parent_row

    sub_rows = build_sub_rows(
        parent_row,
        title,
        (background + "\n\n" + benefits).strip(),
        eligibility,
        how_to_apply,
    )
    if sub_rows is None:
        print(f"  SKIP (bundled, not splittable): {decision_reason}")
        return {"_skipped": True, "url": url, "reason": decision_reason}

    print(f"  SPLIT → {len(sub_rows)} sub-awards: {decision_reason}")
    return {"_sub_awards": sub_rows, "url": url, "parent_title": title}


def _load_done_urls(path: str) -> set[str]:
    """Return URLs already present in the output CSV. Used to skip them on
    resume. Counts both single rows and sub-awards (sub-award external_url
    is parent_url + '#fragment', so we split on '#')."""
    done: set[str] = set()
    if not os.path.exists(path):
        return done
    try:
        with open(path) as f:
            for r in csv.DictReader(f):
                url = (r.get("external_url") or "").split("#", 1)[0]
                if url:
                    done.add(url)
    except (OSError, csv.Error):
        pass
    return done


def main():
    output_dir = "australia/usyd"
    os.makedirs(output_dir, exist_ok=True)
    output_path = f"{output_dir}/usyd_scholarships.csv"
    bundles_path = f"{output_dir}/usyd_bundled_pages.csv"

    # CLI args:
    #   python3 usyd_scraper.py            -> full run, resuming from existing CSV
    #   python3 usyd_scraper.py 5          -> sample run (first 5), fresh start
    #   python3 usyd_scraper.py --restart  -> fresh full run, ignore existing CSV
    import sys
    args = sys.argv[1:]
    restart = "--restart" in args
    args = [a for a in args if a != "--restart"]
    limit = int(args[0]) if args else None

    print("Fetching scholarship listing JSON...")
    listings = get_scholarship_listings()
    if limit:
        listings = listings[:limit]
        print(f"Limiting to first {limit}")

    done_urls = set() if (restart or limit) else _load_done_urls(output_path)
    if done_urls:
        print(f"Resume: skipping {len(done_urls)} URLs already in {output_path}")

    file_exists = os.path.exists(output_path) and not restart and not limit
    mode = "a" if file_exists else "w"

    count = 0
    split_count = 0
    sub_rows_count = 0
    errors = 0
    skipped_done = 0
    bundled: list[dict] = []

    with open(output_path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        if not file_exists:
            writer.writeheader()

        for i, listing in enumerate(listings, 1):
            url = listing.get("url", "")
            if url in done_urls:
                skipped_done += 1
                continue
            print(f"  [{i}/{len(listings)}] {url}")
            row = scrape_scholarship(listing)
            if row is None:
                errors += 1
            elif row.get("_skipped"):
                bundled.append(row)
            elif row.get("_sub_awards"):
                for sub in row["_sub_awards"]:
                    writer.writerow(sub)
                f.flush()
                sub_rows_count += len(row["_sub_awards"])
                split_count += 1
            else:
                writer.writerow(row)
                f.flush()
                count += 1
            time.sleep(0.4)

    bundle_mode = "a" if (os.path.exists(bundles_path) and not restart and not limit) else "w"
    with open(bundles_path, bundle_mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "reason"])
        if bundle_mode == "w":
            writer.writeheader()
        for b in bundled:
            writer.writerow({"url": b["url"], "reason": b["reason"]})

    print(f"\nDone — {count} single + {sub_rows_count} sub-awards from {split_count} "
          f"bundle pages saved to {output_path} "
          f"({errors} errors, {len(bundled)} unsplittable, {skipped_done} skipped as already-done)")


if __name__ == "__main__":
    main()
