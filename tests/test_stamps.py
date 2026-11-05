"""PROD-151: scrapers must not stamp last_verified_at, and the CSV shape is fixed.

Runs offline: HTTP and the LLM helpers are stubbed.
    python3 -m unittest discover -s tests
"""

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scholarship_common import OUTPUT_FIELDNAMES, build_sub_rows  # noqa: E402

EXPECTED_COLUMNS = [
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

HTML = """<html><body><h1>Test Scholarship</h1>
<h2>Overview</h2><p>A scholarship for testing worth $5,000 per year.</p>
<h2>Eligibility</h2><p>Open to domestic students.</p>
</body></html>"""


def load(rel, name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeResp:
    text = HTML
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {}


class StampTests(unittest.TestCase):
    def test_column_order_unchanged(self):
        self.assertEqual(OUTPUT_FIELDNAMES, EXPECTED_COLUMNS)

    def _check(self, mod, arg):
        with mock.patch.object(mod.SESSION, "get", return_value=FakeResp()), \
             mock.patch.object(mod, "classify_page", return_value=None), \
             mock.patch.object(mod, "extract_requirements", return_value=None), \
             mock.patch.object(mod, "detect_bundle", return_value=None, create=True), \
             mock.patch.object(mod, "detect_bundle_with_regex", return_value=None):
            row = mod.scrape_scholarship(arg)
        self.assertIsInstance(row, dict)
        self.assertEqual(list(row.keys()), EXPECTED_COLUMNS)
        self.assertEqual(row["last_verified_at"], "")
        self.assertTrue(row["source_last_synced_at"])
        self.assertNotEqual(row["source_last_synced_at"], mod.NOW)
        return row

    def test_each_fetched_page_gets_its_own_stamp(self):
        cases = [
            ("unsw/unsw_scraper.py", "unsw_clock", "https://www.scholarships.unsw.edu.au/scholarships/id/1"),
            ("usyd/usyd_scraper.py", "usyd_clock", {"url": "https://www.sydney.edu.au/scholarships/test.html", "title": "Test Scholarship", "tags": []}),
        ]
        for path, name, arg in cases:
            with self.subTest(scraper=name):
                mod = load(path, name)
                first = datetime(2026, 11, 1, 1, 2, 3, tzinfo=timezone.utc)
                second = datetime(2026, 11, 1, 2, 3, 4, tzinfo=timezone.utc)
                with mock.patch.object(mod, "datetime") as clock:
                    clock.now.side_effect = [first, second]
                    rows = [self._check(mod, arg), self._check(mod, arg)]
                self.assertEqual([r["source_last_synced_at"] for r in rows],
                                 [t.strftime("%Y-%m-%d %H:%M:%S.%f+00") for t in [first, second]])

    def test_failed_fetch_never_emits_a_fresh_row(self):
        cases = [
            ("unsw/unsw_scraper.py", "unsw_failed", "https://www.scholarships.unsw.edu.au/scholarships/id/1"),
            ("usyd/usyd_scraper.py", "usyd_failed", {"url": "https://www.sydney.edu.au/scholarships/test.html", "title": "Test Scholarship", "tags": []}),
        ]
        for path, name, arg in cases:
            with self.subTest(scraper=name):
                mod = load(path, name)
                with mock.patch.object(mod.SESSION, "get", side_effect=RuntimeError("Synthetic fetch failure")):
                    self.assertIsNone(mod.scrape_scholarship(arg))

    def test_bundle_children_preserve_the_page_stamp_without_verification(self):
        parent = self._check(load("unsw/unsw_scraper.py", "unsw_bundle"),
                             "https://www.scholarships.unsw.edu.au/scholarships/id/1")
        with mock.patch("scholarship_common.split_bundle", return_value={
            "sub_awards": [{"title": "First award", "code": "UGCA1234"},
                           {"title": "Second award", "code": "UGCA1235"}],
        }):
            children = build_sub_rows(parent, "Bundle", "", "", "")
        self.assertEqual(len(children), 2)
        for child in children:
            self.assertEqual(child["source_last_synced_at"], parent["source_last_synced_at"])
            self.assertEqual(child["last_verified_at"], "")
        self.assertEqual(parent["title"], "Test Scholarship")

    def test_unsw_row(self):
        self._check(load("unsw/unsw_scraper.py", "unsw_scraper"),
                    "https://www.scholarships.unsw.edu.au/scholarships/id/1")

    def test_usyd_row(self):
        self._check(load("usyd/usyd_scraper.py", "usyd_scraper"),
                    {"url": "https://www.sydney.edu.au/scholarships/test.html",
                     "title": "Test Scholarship", "tags": []})


if __name__ == "__main__":
    unittest.main()
