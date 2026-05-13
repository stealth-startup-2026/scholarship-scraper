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

from requirements_extractor import classify_page, extract_requirements, split_bundle

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


_NON_INFORMATIVE_AMOUNTS = {
    "not specified", "n/a", "na", "tba", "to be advised",
    "to be confirmed", "varies", "refer to handbook", "see details",
}


def parse_amount(raw: str) -> tuple[float | None, str | None]:
    """Return (numeric_value, display_string) from raw amount text.

    Drops non-informative placeholders ('Not specified', 'TBA', etc.) so the
    card UI doesn't render them as a headline. Preserves real non-dollar
    strings like 'Fully Funded' or 'Tuition + Stipend'.
    """
    if not raw:
        return None, None
    match = re.search(r'\$([0-9,]+)', raw)
    if match:
        numeric = float(match.group(1).replace(",", ""))
        return numeric, raw.strip()
    norm = raw.strip().lower()
    if not norm or norm in _NON_INFORMATIVE_AMOUNTS:
        return None, None
    return None, raw.strip()


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


# Phrases that strongly indicate a page lists multiple sub-awards. We require
# the keyword to sit adjacent to "scholarships/awards/external" so generic
# wording ("you'll be considered for the scholarship") doesn't trigger.
BUNDLE_PHRASE_RE = re.compile(
    r'(?:the\s+following|the\s+below|a\s+number\s+of|wide\s+range\s+of|several|various)\s+'
    r'(?:scholarships|awards|external\s+scholarships)',
    re.IGNORECASE,
)


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:80]


def detect_bundle(hf: dict, outline: str, eligibility: str, selection: str) -> str | None:
    """
    Return a reason string if this page bundles multiple distinct awards, else
    None. Signals are structural where possible (counts of codes / amounts) and
    fall back to a tight list of phrasings only when structure isn't enough.

    Scans outline + eligibility + selection together because UNSW pages put the
    sub-award table in different sections depending on the template — eligibility
    alone misses pages like "UNSW Sport Scholarships" or "Nuclear Engineering
    Awards Program" where the bundle markup lives in outline.
    """
    text = " ".join(filter(None, [outline, eligibility, selection]))
    if not text:
        return None

    # Signal 1 — ≥2 distinct UNSW award codes anywhere on the page.
    # AWARD_CODE_RE only matches UG/PG/PU prefixes, so course codes (LAWS3361
    # etc.) can't trigger this. Two real award codes = a real bundle.
    codes = set(AWARD_CODE_RE.findall(text))
    if len(codes) >= 2:
        return f"multiple award codes: {sorted(codes)}"

    # Signal 2 — ≥3 distinct dollar amounts. Threshold is 3 (not 2) because a
    # single scholarship legitimately quotes a principal + stipend or "up to
    # $X / $Y" range. Three distinct amounts almost always means three awards.
    amounts = set(re.findall(r'\$[0-9][0-9,]*', text))
    if len(amounts) >= 3:
        return f"multiple distinct amounts: {sorted(amounts)}"

    # Signal 3 — explicit "list of awards" prose. The regex requires the
    # keyword to sit next to scholarships/awards/external so bare "considered
    # for the" (common on single-award pages) doesn't fire.
    phrase = BUNDLE_PHRASE_RE.search(text)
    if phrase:
        return f"list-of-awards phrase: '{phrase.group(0)}'"

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

    # NOTE: bundle detection runs *after* we've built the parent row, so we can
    # re-use that row as a template for split sub-awards instead of throwing
    # away the work. See the bundle handling block at the end of this function.

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
    extracted = extract_requirements(eligibility, selection)
    if extracted:
        # Detail page renders these three keys; preserve raw text for audit.
        application_requirements.update(extracted)
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

    parent_row = {
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

    # LLM classifier is the source of truth for single vs. bundled. The regex
    # detector still runs for audit — disagreements are logged so we can spot
    # systemic regressions on either side.
    classification = classify_page(title, outline, eligibility, selection)
    regex_reason = detect_bundle(hf, outline, eligibility, selection)

    if classification is None:
        # Classifier failure (no key, network blip, model errored). Fall back
        # to the regex verdict so the scraper still works offline.
        is_bundle = bool(regex_reason)
        decision_source = "regex_fallback"
        decision_reason = regex_reason or "classifier unavailable, regex says single"
    else:
        is_bundle = bool(classification.get("is_bundle"))
        decision_source = "llm"
        decision_reason = classification.get("reason", "")

    # Log regex/LLM disagreement at INFO level — non-fatal but worth surfacing.
    if classification is not None and bool(regex_reason) != is_bundle:
        print(f"  NOTE: regex/llm disagree (regex={'bundle' if regex_reason else 'single'}, "
              f"llm={'bundle' if is_bundle else 'single'}). regex_reason={regex_reason!r}, "
              f"llm_reason={decision_reason!r}")

    if not is_bundle:
        return parent_row

    sub_rows = build_sub_rows(parent_row, title, outline, eligibility, selection)
    if sub_rows is None:
        print(f"  SKIP (bundled, not splittable, {decision_source}): {decision_reason}")
        return {"_skipped": True, "url": full_url, "reason": decision_reason}

    print(f"  SPLIT ({decision_source} → {len(sub_rows)} sub-awards): {decision_reason}")
    return {"_sub_awards": sub_rows, "url": full_url, "parent_title": title}


def build_sub_rows(
    parent_row: dict,
    title: str,
    outline: str,
    eligibility: str,
    selection: str,
) -> list[dict] | None:
    """
    Given a built parent row + the page's section texts, call the LLM splitter
    and return a list of sub-award rows derived from `parent_row`. Returns None
    when the splitter can't find any sub-awards (aggregator pages, API failure).
    Each sub-row inherits provider/country/term/etc. from the parent and
    overrides id, external_id, external_url, slug, title, amount, eligibility
    bullets, and application_requirements.
    """
    split = split_bundle(title, outline, eligibility, selection)
    sub_awards = (split or {}).get("sub_awards") or []
    if not sub_awards:
        return None

    application_mode = (split or {}).get("application_mode", "automatic")
    sep_required = (split or {}).get("separate_application_required", False)
    full_url = parent_row.get("external_url", "")

    sub_rows: list[dict] = []
    for i, sa in enumerate(sub_awards):
        code = (sa.get("code") or "").strip() or None
        sub_title = (sa.get("title") or title).strip()
        must_meet = [m.strip() for m in (sa.get("must_meet") or []) if m and m.strip()]

        row = dict(parent_row)
        row["id"] = str(uuid.uuid4())
        row["external_id"] = code or f"{parent_row['external_id']}-{i + 1}"
        row["external_url"] = f"{full_url}#{code or i + 1}"
        row["slug"] = _slugify(sub_title) or f"{parent_row['slug']}-{i + 1}"
        row["title"] = sub_title
        if sa.get("amount_value") is not None:
            try:
                row["amount_value"] = float(sa["amount_value"])
            except (TypeError, ValueError):
                pass
        if sa.get("amount_display"):
            row["amount_display"] = sa["amount_display"]
        row["eligibility_summary"] = "\n".join(must_meet) if must_meet else None
        row["application_requirements"] = json.dumps({
            "application_mode": application_mode,
            "separate_application_required": sep_required,
            "must_meet": must_meet,
            "parent_url": full_url,
            "parent_title": title,
        })
        sub_rows.append(row)

    return sub_rows


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
    split_count = 0
    sub_rows_count = 0
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
            elif row.get("_sub_awards"):
                for sub in row["_sub_awards"]:
                    writer.writerow(sub)
                sub_rows_count += len(row["_sub_awards"])
                split_count += 1
            else:
                writer.writerow(row)
                count += 1
            time.sleep(0.4)

    with open(bundles_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "reason"])
        writer.writeheader()
        for b in bundled:
            writer.writerow({"url": b["url"], "reason": b["reason"]})

    print(f"\nDone — {count} single + {sub_rows_count} sub-awards from {split_count} "
          f"bundle pages saved to {output_path} "
          f"({errors} errors, {len(bundled)} unsplittable pages logged to {bundles_path})")


if __name__ == "__main__":
    main()
