#!/usr/bin/env python3
"""
Tests for the Supabase lead sync (sync/push_to_supabase.py):
  - ledger -> row transform (core columns + evidence JSONB, no lifecycle)
  - organization_id preflight (unknown ids sent as null so the FK holds)

Network functions are not exercised here; the transform and preflight are the
parts that decide WHAT gets pushed.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "sync"))

import push_to_supabase as pts  # noqa: E402


def make_lead(**overrides):
    lead = {
        "external_id": "23e580ae7f87c63c",
        "source": "meeting-minutes",
        "organization": "Leander ISD",
        "organization_id": "leander-isd-tx",
        "state": "TX",
        "county": "Williamson",
        "summary": "Leander ISD: artificial turf replacement.",
        "evidence_quote": "quote",
        "source_url": "https://example.test/minutes",
        "discovered_at": "2026-07-16T12:00:00Z",
        "location_id": "us-tx-meeting-minutes",
        "evidence": {"project_name": "Leander ISD - artificial turf replacement"},
    }
    lead.update(overrides)
    return lead


def write_ledger(tmp: str, leads: list) -> Path:
    path = Path(tmp) / "ledger.json"
    path.write_text(json.dumps({"schema_version": "2.0", "leads": leads}),
                    encoding="utf-8")
    return path


class LeadRowsTest(unittest.TestCase):
    def test_core_columns_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = pts.lead_rows(write_ledger(tmp, [make_lead()]))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), set(pts.CORE_FIELDS) | {"evidence"})
        self.assertEqual(row["external_id"], "23e580ae7f87c63c")
        self.assertEqual(row["source"], "meeting-minutes")
        self.assertEqual(row["evidence"]["project_name"],
                         "Leander ISD - artificial turf replacement")

    def test_lifecycle_columns_never_sent(self):
        # Even if a ledger record somehow carried lifecycle state, the row
        # must not include it: those columns are BDM-owned in Retool.
        with tempfile.TemporaryDirectory() as tmp:
            rows = pts.lead_rows(write_ledger(
                tmp, [make_lead(status="Qualified", assigned_bdm="someone")]))
        self.assertNotIn("status", rows[0])
        self.assertNotIn("assigned_bdm", rows[0])
        self.assertNotIn("rejected_reason", rows[0])

    def test_missing_bid_due_date_sent_as_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = pts.lead_rows(write_ledger(tmp, [make_lead()]))
        self.assertIn("bid_due_date", rows[0])
        self.assertIsNone(rows[0]["bid_due_date"])

    def test_empty_ledger_gives_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pts.lead_rows(write_ledger(tmp, [])), [])


class OrgPreflightTest(unittest.TestCase):
    def test_known_org_id_is_kept(self):
        rows = [{"external_id": "a" * 16, "organization_id": "leander-isd-tx"}]
        warnings = pts.apply_org_preflight(rows, {"leander-isd-tx"})
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["organization_id"], "leander-isd-tx")

    def test_unknown_org_id_is_nulled_with_warning(self):
        rows = [{"external_id": "a" * 16, "organization_id": "unregistered-isd-tx"}]
        warnings = pts.apply_org_preflight(rows, {"leander-isd-tx"})
        self.assertIsNone(rows[0]["organization_id"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("unregistered-isd-tx", warnings[0])

    def test_absent_org_id_passes_untouched(self):
        rows = [{"external_id": "a" * 16, "organization_id": None}]
        self.assertEqual(pts.apply_org_preflight(rows, set()), [])
        self.assertIsNone(rows[0]["organization_id"])


if __name__ == "__main__":
    unittest.main()
