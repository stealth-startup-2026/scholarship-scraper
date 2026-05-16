#!/usr/bin/env python3
"""
Compare DeepSeek's international-eligibility verdicts against the Codex
baseline. Codex's CSV is a positive list (True-only rows); DeepSeek's
covers all 147 with True/False. We compute set agreement and surface
the disagreement rows so you can spot-check who's right.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

CODEX_PATH = Path("australia/unsw/unsw_international_eligibility.csv")
DEEPSEEK_PATH = Path("australia/unsw/deepseek_international_eligibility.csv")
REASONS_PATH = Path("australia/unsw/deepseek_international_reasons.csv")


def load_set(path: Path, *, true_only: bool) -> dict[str, dict]:
    """Returns {id: row}. Optionally only keeps rows where is_international is True."""
    if not path.exists():
        print(f"missing: {path}", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            is_intl = (r.get("is_international", "").strip().lower() == "true")
            if true_only and not is_intl:
                continue
            out[r["id"]] = {**r, "is_international": is_intl}
    return out


def load_reasons(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["id"]: r.get("reasoning", "") for r in csv.DictReader(f)}


def main() -> int:
    codex = load_set(CODEX_PATH, true_only=True)
    ds_all = load_set(DEEPSEEK_PATH, true_only=False)
    ds_true = {k: v for k, v in ds_all.items() if v["is_international"]}
    reasons = load_reasons(REASONS_PATH)

    if not codex or not ds_all:
        return 1

    codex_ids = set(codex.keys())
    ds_ids = set(ds_true.keys())

    agree_both_true = codex_ids & ds_ids
    only_codex = codex_ids - ds_ids   # Codex says intl, DeepSeek says not
    only_ds = ds_ids - codex_ids       # DeepSeek says intl, Codex doesn't list

    # Codex doesn't have negatives, so we can't compute "both say false" agreement
    # against the Codex baseline. We only see the two disagreement directions.

    print(f"Codex marked international:    {len(codex_ids)}")
    print(f"DeepSeek marked international: {len(ds_ids)}")
    print(f"Total scholarships scored:     {len(ds_all)}")
    print()
    print(f"Agree (both = True):           {len(agree_both_true)}")
    print(f"Only Codex says True:          {len(only_codex)}  (DeepSeek missed)")
    print(f"Only DeepSeek says True:       {len(only_ds)}  (DeepSeek added)")

    precision = len(agree_both_true) / len(ds_ids) if ds_ids else 0.0
    recall = len(agree_both_true) / len(codex_ids) if codex_ids else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    print()
    print("(Treating Codex as ground truth)")
    print(f"  Precision: {precision:.1%}")
    print(f"  Recall:    {recall:.1%}")
    print(f"  F1:        {f1:.1%}")

    if only_codex:
        print()
        print(f"=== Codex says True, DeepSeek says False ({len(only_codex)}) ===")
        for sid in sorted(only_codex):
            name = codex[sid]["name"]
            reason = reasons.get(sid, "(no reason logged)")
            print(f"  [{sid[:8]}] {name[:60]}")
            print(f"      DS reason: {reason[:140]}")

    if only_ds:
        print()
        print(f"=== DeepSeek says True, Codex doesn't list ({len(only_ds)}) ===")
        for sid in sorted(only_ds):
            name = ds_true[sid]["name"]
            reason = reasons.get(sid, "(no reason logged)")
            print(f"  [{sid[:8]}] {name[:60]}")
            print(f"      DS reason: {reason[:140]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
