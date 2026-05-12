#!/usr/bin/env python3
"""
Scraper for UNSW scholarship listings.
https://www.scholarships.unsw.edu.au/scholarships/search
"""

import csv
import json
import re
import time
import uuid
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

SOURCE_ID = "c2d4e6f8-1a3b-5c7d-9e0f-2b4c6d8e0f1a"
BASE_URL = "https://www.scholarships.unsw.edu.au"
SEARCH_URL = f"{BASE_URL}/scholarships/search"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00")

# UNSW award codes use a fixed prefix shape: UG/PG/PU followed by a 2-letter
# category and 3-5 digits (e.g. UGCA1392, PGCE2017, PUCA1029). Faculty/course
# codes (LAWS3361, COMP1511) deliberately don't match, so we don't grab them
# as a scholarship's external_id when the page has no real award code.
AWARD_CODE_RE = re.compile(r'\b((?:UG|PG|PU)[A-Z]{2}\d{3,5})\b')

# Exclude contact emails from these domains (foundation/alumni)
EXCLUDED_EMAIL_PATTERNS = ["foundation", "alumni", "giving", "philanthropy"]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ScholarshipScraper/1.0)",
    "Accept": "text/html,application/xhtml+xml",
})

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


def parse_date(raw: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d 00:00:00.000000+00")
        except ValueError:
            continue
    return None


def parse_amount(raw: str) -> tuple[float | None, str | None]:
    """Return (numeric_value, display_string) from raw amount text."""
    if not raw:
        return None, None
    # e.g. "$15,000 available for up to 2 years" or "$30,000 available for 1 year"
    match = re.search(r'\$([0-9,]+)', raw)
    if match:
        numeric = float(match.group(1).replace(",", ""))
        return numeric, raw.strip()
    return None, raw.strip() or None


def infer_study_level(education_text: str, title: str = "") -> list:
    text = (education_text + " " + title).lower()
    levels = []
    if any(x in text for x in ["postgrad", "masters", "phd", "doctorate", "graduate", "pgca", "pgre"]):
        levels.append("postgraduate")
    if any(x in text for x in ["undergrad", "1st year", "2nd year", "3rd", "4th", "honours", "bachelor", "ugce", "ugtr"]):
        levels.append("undergraduate")
    if not levels:
        levels.append("undergraduate")
    return list(dict.fromkeys(levels))


def infer_funding_type(criteria: str, amount_display: str) -> list:
    text = (criteria + " " + (amount_display or "")).lower()
    types = []
    if any(x in text for x in ["tuition", "fee", "remission"]):
        types.append("tuition")
    if any(x in text for x in ["living", "stipend", "accommodation"]):
        types.append("living")
    if any(x in text for x in ["travel", "exchange", "overseas"]):
        types.append("travel")
    if not types:
        types.append("cash")
    return types


def infer_citizenships(residency: str) -> list:
    if not residency:
        return []
    text = residency.lower()
    if "no residency" in text or "no requirement" in text:
        return []
    citizens = []
    if "aus citizen" in text or "australian citizen" in text:
        citizens.append("AU")
    if "permanent resident" in text or "pr" in text:
        if "AU" not in citizens:
            citizens.append("AU-PR")
    if "international" in text:
        citizens.append("INTERNATIONAL")
    return citizens


def filter_contact_emails(emails: list) -> list:
    result = []
    for e in emails:
        lower = e.lower()
        if not any(pat in lower for pat in EXCLUDED_EMAIL_PATTERNS):
            result.append(e)
    return result


def get_section_text(soup: BeautifulSoup, heading: str) -> str:
    """
    Find a section by its label in block-main-content using the line-by-line text format.
    The content uses plain text rendering with label on one line, content on following lines.
    """
    container = soup.find(id="block-main-content") or soup.body
    text = container.get_text(separator="\n", strip=True) if container else ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    text_block = "\n".join(lines)

    # Sections of interest: Outline, Eligibility, Selection
    next_sections = ["Outline", "Eligibility", "Selection", "Contact us", "FAQs", "For donors"]
    others = "|".join(s for s in next_sections if s != heading)
    pattern = rf'\n{re.escape(heading)}\n(.*?)(?=\n(?:{others})|\Z)'
    m = re.search(pattern, text_block, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_header_block(soup: BeautifulSoup) -> dict:
    """
    Extract the structured field block from the scholarship header.
    Fields are rendered as label on one line, value on next line(s).
    Uses block-main-content (the real content div; id=main-content is a skip link).
    """
    container = soup.find(id="block-main-content") or soup.body
    text = container.get_text(separator="\n", strip=True) if container else ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    text_block = "\n".join(lines)

    fields = {}

    # Code (e.g. UGCE1536, PGCA1066) — UNSW scholarship award code only.
    code_match = AWARD_CODE_RE.search(text_block)
    fields["code"] = code_match.group(1) if code_match else None

    # Status — first occurrence of Open or Closed
    status_match = re.search(r'(?:^|\n)(Open|Closed)(?:\n|$)', text_block)
    fields["status"] = status_match.group(1) if status_match else None

    # Criteria — value on line(s) after "Criteria" label, before next label
    crit = re.search(
        r'\nCriteria\n(.*?)(?=\nEducation Level|\nMinimum Value|\nResidency|\nOpens|\nOutline|\Z)',
        text_block, re.DOTALL)
    fields["criteria"] = crit.group(1).strip().replace("\n", " ") if crit else ""

    # Education Level
    edu = re.search(
        r'\nEducation Level\n(.*?)(?=\nMinimum Value|\nResidency|\nOpens|\nOutline|\Z)',
        text_block, re.DOTALL)
    fields["education_level"] = edu.group(1).strip().replace("\n", " ") if edu else ""

    # Minimum Value — capture dollar amount and descriptor lines
    val = re.search(
        r'\nMinimum Value\n(.*?)(?=\nResidency|\nOpens|\nOutline|\Z)',
        text_block, re.DOTALL)
    fields["value_raw"] = val.group(1).strip().replace("\n", " ") if val else ""

    # Awards count (e.g. "3 Scholarship(s) available")
    count_match = re.search(r'(\d+)\s+Scholarship(?:s)?\s*\(?s?\)?\s+available',
                             fields.get("value_raw", ""), re.IGNORECASE)
    fields["awards_count"] = int(count_match.group(1)) if count_match else None

    # Residency
    res = re.search(
        r'\nResidency\n(.*?)(?=\nOpens|\nCloses|\nOutline|\Z)',
        text_block, re.DOTALL)
    fields["residency"] = res.group(1).strip().replace("\n", " ") if res else ""

    # Opens date
    opens = re.search(r'\nOpens\n(\d{1,2}/\d{1,2}/\d{4})', text_block)
    fields["opens"] = opens.group(1) if opens else None

    # Closes date
    closes = re.search(r'\nCloses\n(\d{1,2}/\d{1,2}/\d{4})', text_block)
    fields["closes"] = closes.group(1) if closes else None

    # For commencement term
    comm = re.search(r'For commencement\n?(Term \d+ \d{4})', text_block)
    fields["commencement"] = comm.group(1) if comm else None

    return fields


def detect_bundle(hf: dict, eligibility: str) -> str | None:
    """
    Return a reason string if this page bundles multiple distinct awards, else
    None. The signals here are intentionally structural (counts of codes and
    amounts) rather than tied to UNSW prose, so the same approach can be
    transplanted to other uni scrapers — only AWARD_CODE_RE changes per site.
    """
    elig = eligibility or ""

    # Signal 1 — multiple distinct award codes in the eligibility text.
    codes = set(AWARD_CODE_RE.findall(elig))
    if len(codes) >= 2:
        return f"multiple award codes in eligibility: {sorted(codes)}"

    # Signal 2 — multiple distinct dollar amounts in the eligibility text.
    # Awards quote one headline amount; pages listing 2+ amounts in the
    # eligibility section are almost always describing 2+ sub-awards.
    # (Note: hf['awards_count'] is intentionally NOT a signal here — UNSW
    # uses that to mean "N recipients of the same award", not "N awards".)
    amounts = set(re.findall(r'\$[0-9][0-9,]*', elig))
    if len(amounts) >= 2:
        return f"multiple distinct amounts in eligibility: {sorted(amounts)}"

    return None


def scrape_scholarship(url: str) -> dict | None:
    full_url = BASE_URL + url if url.startswith("/") else url
    try:
        resp = SESSION.get(full_url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  HTTP error {full_url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Header fields
    hf = parse_header_block(soup)

    # Section text
    outline = get_section_text(soup, "Outline")
    eligibility = get_section_text(soup, "Eligibility")
    selection = get_section_text(soup, "Selection")

    bundle_reason = detect_bundle(hf, eligibility)
    if bundle_reason:
        print(f"  SKIP (bundled page): {bundle_reason}")
        return {"_skipped": True, "url": full_url, "reason": bundle_reason}

    # Contact emails (from mailto: links only, excluding foundation/alumni)
    raw_emails = [a["href"].replace("mailto:", "").strip()
                  for a in soup.find_all("a", href=re.compile(r"^mailto:", re.IGNORECASE))]
    contact_emails = filter_contact_emails(raw_emails)

    # External URL: the scholarship's apply URL if present, else the page URL
    apply_link = soup.find("a", string=re.compile(r"apply", re.IGNORECASE))
    apply_url = (BASE_URL + apply_link["href"]) if apply_link and apply_link.get("href", "").startswith("/") else (apply_link["href"] if apply_link else full_url)

    # Slug from URL
    slug_match = re.search(r'/scholarships/id/(.+)', url)
    slug = slug_match.group(1).replace("/", "-") if slug_match else url.strip("/").replace("/", "-")

    amount_value, amount_display = parse_amount(hf.get("value_raw", ""))
    study_levels = infer_study_level(hf.get("education_level", ""), title + " " + (hf.get("code") or ""))
    funding_type = infer_funding_type(hf.get("criteria", ""), amount_display)
    citizenships = infer_citizenships(hf.get("residency", ""))
    is_active = (hf.get("status") or "").lower() == "open"

    full_text = " ".join(filter(None, [title, outline, eligibility, selection, hf.get("criteria", "")])).lower()
    requires_essay = "essay" in full_text or "personal statement" in full_text
    need_based = any(w in full_text for w in ["financial need", "need-based", "disadvantaged", "low income", "financial hardship", "financial difficulty"])
    merit_based = any(w in full_text for w in ["merit", "academic", "gpa", "grade", "achievement", "excellence"])

    # Tags from criteria + education level
    tags = []
    for word in re.split(r"[\s,\-]+", (hf.get("criteria", "") + " " + hf.get("education_level", "")).lower()):
        word = word.strip()
        if word and len(word) > 2:
            tags.append(word)
    tags = list(dict.fromkeys(tags))  # deduplicate preserving order

    application_requirements = {}
    if eligibility:
        application_requirements["eligibility_text"] = eligibility
    if selection:
        application_requirements["selection_criteria"] = selection

    raw_payload = {
        "code": hf.get("code"),
        "status": hf.get("status"),
        "criteria": hf.get("criteria"),
        "education_level": hf.get("education_level"),
        "value_raw": hf.get("value_raw"),
        "residency": hf.get("residency"),
        "opens": hf.get("opens"),
        "closes": hf.get("closes"),
        "commencement": hf.get("commencement"),
        "outline": outline,
        "eligibility": eligibility,
        "selection": selection,
        "source_url": full_url,
    }

    return {
        "id": str(uuid.uuid4()),
        "source_id": SOURCE_ID,
        "external_id": hf.get("code") or slug,
        "external_url": full_url,
        "slug": slug,
        "title": title,
        "provider_name": "University of New South Wales",
        "description": outline or None,
        "amount_value": amount_value,
        "amount_currency": "AUD",
        "amount_display": amount_display,
        "awards_count": hf.get("awards_count"),
        "frequency": "yearly",
        "study_level": json.dumps(study_levels),
        "funding_type": json.dumps(funding_type),
        "citizenships": json.dumps(citizenships),
        "eligible_countries": json.dumps(["Australia"]) if citizenships else json.dumps([]),
        "excluded_countries": json.dumps([]),
        "eligible_genders": json.dumps([]),
        "minimum_gpa": None,
        "requires_essay": requires_essay,
        "need_based": need_based,
        "merit_based": merit_based,
        "school_name": "University of New South Wales",
        "country": "Australia",
        "state_region": "nsw",
        "application_open_at": parse_date(hf.get("opens")),
        "deadline_at": parse_date(hf.get("closes")),
        "start_term": hf.get("commencement"),
        "is_recurring": True,
        "is_active": is_active,
        "is_featured": False,
        "last_verified_at": NOW,
        "source_last_synced_at": NOW,
        "tags": json.dumps(tags),
        "eligibility_summary": eligibility or None,
        "application_requirements": json.dumps(application_requirements),
        "contact_info": json.dumps(contact_emails) if contact_emails else None,
        "raw_payload": json.dumps(raw_payload),
        "created_at": NOW,
        "updated_at": NOW,
    }


def get_scholarship_urls() -> list:
    resp = SESSION.get(SEARCH_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    links = set()
    for a in soup.find_all("a", href=re.compile(r"/scholarships/id/")):
        href = a["href"]
        # Normalise: strip query strings
        href = href.split("?")[0]
        links.add(href)
    return sorted(links)


def main():
    print("Fetching scholarship list...")
    urls = get_scholarship_urls()
    print(f"Found {len(urls)} scholarship URLs")

    output_path = "australia/unsw_scholarships.csv"
    bundles_path = "australia/unsw_bundled_pages.csv"
    count = 0
    errors = 0
    bundled = []

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()

        for i, url in enumerate(urls, 1):
            print(f"  [{i}/{len(urls)}] {url}")
            row = scrape_scholarship(url)
            if row is None:
                errors += 1
            elif row.get("_skipped"):
                bundled.append(row)
            else:
                writer.writerow(row)
                count += 1
            time.sleep(0.4)

    with open(bundles_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "reason"])
        writer.writeheader()
        for b in bundled:
            writer.writerow({"url": b["url"], "reason": b["reason"]})

    print(f"\nDone — {count} scholarships saved to {output_path} "
          f"({errors} errors, {len(bundled)} bundled pages logged to {bundles_path})")


if __name__ == "__main__":
    main()
