#!/usr/bin/env python3
"""
Verify which USyd scholarships are open to international students.

This script gives USyd the same audit workflow as compare_international.py,
but uses the existing USyd scrape as the baseline:

- baseline: the scraper's `citizenships` field / USyd `student-type` tags
- optional LLM audit: reuses verify_international.py's DeepSeek classifier
- comparison: prints agreement stats and writes a disagreement CSV

Typical use:
    python3 usyd/verify_usyd_international.py
    python3 usyd/verify_usyd_international.py --llm
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UNSW_DIR = REPO_ROOT / "unsw"
for path in (REPO_ROOT, UNSW_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from verify_international import (
    _cache_key,
    _read_cache,
    extract_relevant_text,
    verify,
)

DEFAULT_CSV = Path("australia/usyd/usyd_scholarships.csv")
BASELINE_NAME = "usyd_tag_international_eligibility.csv"
BASELINE_REASONS_NAME = "usyd_tag_international_reasons.csv"
LLM_NAME = "deepseek_international_eligibility.csv"
LLM_REASONS_NAME = "deepseek_international_reasons.csv"
DISAGREEMENTS_NAME = "usyd_international_disagreements.csv"
SAMPLE_SUFFIX = ".sample"


def _json_list(value: str) -> list:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _student_type_tags(row: dict) -> list[str]:
    try:
        raw_payload = json.loads(row.get("raw_payload") or "{}")
    except json.JSONDecodeError:
        raw_payload = {}
    out: list[str] = []
    for tag in raw_payload.get("tags_raw") or []:
        value = str(tag.get("value", ""))
        if "scholarships:student-type/" in value:
            out.append(str(tag.get("name", "")).strip())
    return [t for t in out if t]


def baseline_verdict(row: dict) -> tuple[bool | None, str]:
    """Return the USyd tag-based verdict.

    None means the page has no usable student-type signal, so it should be
    manually/LLM reviewed rather than treated as a confident domestic result.
    """
    citizenships = {str(x).upper() for x in _json_list(row.get("citizenships", ""))}
    if "INTERNATIONAL" in citizenships:
        return True, "scraper citizenships includes INTERNATIONAL"
    if citizenships == {"AU"}:
        return False, "scraper citizenships only includes AU"

    student_types = {t.lower() for t in _student_type_tags(row)}
    if "international" in student_types:
        return True, "USyd student-type tag includes International"
    if student_types == {"domestic"}:
        return False, "USyd student-type tag only includes Domestic"
    if {"domestic", "international"} <= student_types:
        return True, "USyd student-type tags include Domestic and International"

    tags = (row.get("tags") or "").lower()
    if "international" in tags:
        return True, "scraper tags include international"
    if "domestic" in tags and "international" not in tags:
        return False, "scraper tags only include domestic"

    return None, "no citizenship/student-type tag found"


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        print(f"missing: {path}", file=sys.stderr)
        raise SystemExit(1)
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_baseline(rows: list[dict], output_dir: Path) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for row in rows:
        verdict, reason = baseline_verdict(row)
        results.append({
            "id": row.get("id", ""),
            "name": row.get("title", ""),
            "is_international": verdict,
            "reasoning": reason,
            "external_url": row.get("external_url", ""),
        })

    with (output_dir / BASELINE_NAME).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "is_international"])
        writer.writeheader()
        for row in results:
            # Keep this diffable with verify_international.py. Unknowns are
            # written as False in the compact file and preserved in reasons.
            writer.writerow({
                "id": row["id"],
                "name": row["name"],
                "is_international": "True" if row["is_international"] is True else "False",
            })

    with (output_dir / BASELINE_REASONS_NAME).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["id", "name", "is_international", "reasoning", "external_url"],
        )
        writer.writeheader()
        for row in results:
            label = "Unknown" if row["is_international"] is None else (
                "True" if row["is_international"] else "False"
            )
            writer.writerow({**row, "is_international": label})

    return results


def _with_sample_suffix(filename: str, *, sample: bool) -> str:
    if not sample:
        return filename
    stem, dot, suffix = filename.rpartition(".")
    return f"{stem}{SAMPLE_SUFFIX}{dot}{suffix}" if dot else f"{filename}{SAMPLE_SUFFIX}"


def run_llm(rows: list[dict], output_dir: Path, limit: int | None = None) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = rows[:limit] if limit else rows
    scored_name = _with_sample_suffix(LLM_NAME, sample=bool(limit))
    reasons_name = _with_sample_suffix(LLM_REASONS_NAME, sample=bool(limit))
    results: list[dict] = []
    api_calls = 0
    cache_hits = 0
    failures = 0
    no_text = 0

    for i, row in enumerate(selected, 1):
        sid = row.get("id", "")
        name = row.get("title", "")
        text = extract_relevant_text(row)
        if not text:
            no_text += 1
            print(f"  [{i:>4}/{len(selected)}] (no text)   {name[:60]}")
            results.append({
                "id": sid,
                "name": name,
                "is_international": False,
                "reasoning": "no eligibility text on page",
            })
            continue

        was_cached = _read_cache(_cache_key(name, text)) is not None
        verdict = verify(name, text)
        if was_cached:
            cache_hits += 1
        else:
            api_calls += 1
            time.sleep(0.1)

        if verdict is None:
            failures += 1
            print(f"  [{i:>4}/{len(selected)}] FAIL        {name[:60]}")
            results.append({
                "id": sid,
                "name": name,
                "is_international": False,
                "reasoning": "API failed",
            })
            continue

        is_international = bool(verdict.get("is_international"))
        marker = "INT " if is_international else "DOM "
        print(f"  [{i:>4}/{len(selected)}] {marker}        {name[:60]}")
        results.append({
            "id": sid,
            "name": name,
            "is_international": is_international,
            "reasoning": (verdict.get("reasoning") or "").strip(),
        })

    with (output_dir / scored_name).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "is_international"])
        writer.writeheader()
        for row in results:
            writer.writerow({
                "id": row["id"],
                "name": row["name"],
                "is_international": "True" if row["is_international"] else "False",
            })

    with (output_dir / reasons_name).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "is_international", "reasoning"])
        writer.writeheader()
        for row in results:
            writer.writerow({
                **row,
                "is_international": "True" if row["is_international"] else "False",
            })

    print()
    print(f"LLM rows scored:   {len(results)}")
    print(f"LLM international: {sum(1 for r in results if r['is_international'])}")
    print(f"API calls:         {api_calls}")
    print(f"Cache hits:        {cache_hits}")
    print(f"No-text rows:      {no_text}")
    print(f"API failures:      {failures}")
    return results


def load_llm_results(output_dir: Path, *, sample: bool = False) -> tuple[dict[str, dict], dict[str, str]]:
    scored_path = output_dir / _with_sample_suffix(LLM_NAME, sample=sample)
    reasons_path = output_dir / _with_sample_suffix(LLM_REASONS_NAME, sample=sample)
    if not scored_path.exists():
        return {}, {}

    scored: dict[str, dict] = {}
    with scored_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            scored[row["id"]] = {
                **row,
                "is_international": row.get("is_international", "").strip().lower() == "true",
            }

    reasons: dict[str, str] = {}
    if reasons_path.exists():
        with reasons_path.open(newline="", encoding="utf-8") as f:
            reasons = {row["id"]: row.get("reasoning", "") for row in csv.DictReader(f)}
    return scored, reasons


def compare(
    baseline: list[dict],
    llm_by_id: dict[str, dict],
    llm_reasons: dict[str, str],
    output_dir: Path,
    *,
    sample: bool = False,
) -> None:
    base_by_id = {row["id"]: row for row in baseline}
    base_true = {row["id"] for row in baseline if row["is_international"] is True}
    base_false = {row["id"] for row in baseline if row["is_international"] is False}
    base_unknown = {row["id"] for row in baseline if row["is_international"] is None}
    scored_ids = set(llm_by_id)
    scored_base_true = base_true & scored_ids
    scored_base_false = base_false & scored_ids
    scored_base_unknown = base_unknown & scored_ids
    llm_true = {sid for sid, row in llm_by_id.items() if row["is_international"]}

    agree_true = scored_base_true & llm_true
    only_baseline = scored_base_true - llm_true
    only_llm = llm_true - scored_base_true
    llm_on_known = scored_ids & (base_true | base_false)
    known_agree = sum(
        1
        for sid in llm_on_known
        if (sid in base_true) == bool(llm_by_id[sid]["is_international"])
    )

    print()
    print(f"USyd tag baseline international: {len(base_true)} total, {len(scored_base_true)} scored")
    print(f"USyd tag baseline domestic:      {len(base_false)} total, {len(scored_base_false)} scored")
    print(f"USyd tag baseline unknown:       {len(base_unknown)} total, {len(scored_base_unknown)} scored")
    print(f"DeepSeek marked international:   {len(llm_true)}")
    print(f"DeepSeek rows scored:            {len(llm_by_id)}")
    print()
    print(f"Agree (both = True):             {len(agree_true)}")
    print(f"Only USyd tags say True:         {len(only_baseline)}")
    print(f"Only DeepSeek says True:         {len(only_llm)}")
    if llm_on_known:
        print(f"Known-tag accuracy:              {known_agree / len(llm_on_known):.1%}")

    disagreement_ids = sorted(only_baseline | only_llm)
    disagreements_name = _with_sample_suffix(DISAGREEMENTS_NAME, sample=sample)
    with (output_dir / disagreements_name).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "id",
                "name",
                "tag_is_international",
                "llm_is_international",
                "tag_reasoning",
                "llm_reasoning",
                "external_url",
            ],
        )
        writer.writeheader()
        for sid in disagreement_ids:
            base = base_by_id.get(sid, {})
            llm = llm_by_id.get(sid, {})
            writer.writerow({
                "id": sid,
                "name": base.get("name") or llm.get("name", ""),
                "tag_is_international": (
                    "Unknown" if base.get("is_international") is None
                    else "True" if base.get("is_international") else "False"
                ),
                "llm_is_international": "True" if llm.get("is_international") else "False",
                "tag_reasoning": base.get("reasoning", ""),
                "llm_reasoning": llm_reasons.get(sid, ""),
                "external_url": base.get("external_url", ""),
            })
    print(f"Disagreements written to:        {output_dir / disagreements_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Run the DeepSeek verifier before comparing. Otherwise uses existing output if present.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only score the first N rows with --llm. Useful for smoke tests.",
    )
    args = parser.parse_args()

    rows = load_rows(args.csv)
    output_dir = args.out_dir or args.csv.parent

    baseline = write_baseline(rows, output_dir)
    print(f"Rows read:                         {len(rows)}")
    print(f"USyd tag baseline international:   {sum(1 for r in baseline if r['is_international'] is True)}")
    print(f"USyd tag baseline unknown:         {sum(1 for r in baseline if r['is_international'] is None)}")
    print(f"Baseline written to:               {output_dir / BASELINE_NAME}")
    print(f"Baseline reasons written to:       {output_dir / BASELINE_REASONS_NAME}")

    if args.llm:
        run_llm(rows, output_dir, limit=args.limit)

    use_sample = bool(args.llm and args.limit)
    llm_by_id, llm_reasons = load_llm_results(output_dir, sample=use_sample)
    if llm_by_id:
        compare(baseline, llm_by_id, llm_reasons, output_dir, sample=use_sample)
    else:
        print()
        print(f"No LLM output found at:            {output_dir / LLM_NAME}")
        print("Run with --llm to generate DeepSeek verification, or compare the baseline CSV directly.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
