#!/usr/bin/env python3
"""
Tests for scripts/export_leads.py.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import export_leads  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DISTRICTS_CSV = FIXTURES / "districts.csv"
SCHEMA_PATH = REPO_ROOT / "contracts" / "lead.schema.json"

# Pinned so the frozen external_id recipe can't drift silently: SHA-1 of
# "795|725242|mm-turf". If this constant ever needs to change, every lead in
# the database would be duplicated - so this test failing is a red flag.
EXPECTED_ID_795_725242 = "f49fd606be799e5627211c03587e2cf6e12fc01f"


class ExportLeadsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.ledger = tmp / "leads" / "ledger.json"
        self.exports = tmp / "exports"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, input_file, run_timestamp="2026-07-14T09:12:00Z"):
        return export_leads.run_export(
            input_path=FIXTURES / input_file,
            districts_csv=DISTRICTS_CSV,
            ledger_path=self.ledger,
            exports_dir=self.exports,
            schema_path=SCHEMA_PATH,
            run_timestamp=run_timestamp,
        )

    def test_mapped_lead_validates_against_schema(self):
        # Fixture has 2 records but only 1 turf hit -> the filter drops the other.
        counts = self._run("org_795.json")
        self.assertEqual(counts["candidates"], 1)
        self.assertEqual(counts["new"], 1)
        self.assertEqual(counts["invalid"], 0)

        ledger = json.loads(self.ledger.read_text())
        self.assertEqual(ledger["schema_version"], "2.0")
        validator = Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text()),
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        for lead in ledger["leads"]:
            self.assertEqual(list(validator.iter_errors(lead)), [], lead["external_id"])
            self.assertEqual(lead["source"], "meeting-minutes")
            self.assertEqual(lead["location_id"], "us-xx-boardbook")
            # source_url must deep-link the specific meeting, never bare boardbook.
            self.assertIn("/Public/Agenda/795?meeting=", lead["source_url"])

    def test_external_id_is_stable_and_pinned(self):
        # Same inputs -> same id, computed directly and via a full run.
        self.assertEqual(
            export_leads.compute_external_id("795", "725242"),
            export_leads.compute_external_id(" 795 ", "725242"),
        )
        self.assertEqual(
            export_leads.compute_external_id("795", "725242"),
            EXPECTED_ID_795_725242,
        )
        self._run("org_795.json")
        ids = {l["external_id"] for l in json.loads(self.ledger.read_text())["leads"]}
        self.assertIn(EXPECTED_ID_795_725242, ids)

    def test_second_run_adds_nothing_new(self):
        first = self._run("org_795.json", run_timestamp="2026-07-14T09:12:00Z")
        self.assertEqual(first["new"], 1)

        second = self._run("org_795.json", run_timestamp="2026-08-01T00:00:00Z")
        self.assertEqual(second["candidates"], 1)
        self.assertEqual(second["new"], 0)
        self.assertEqual(second["already_known"], 1)

        # Ledger unchanged in size; the second run file holds zero leads.
        ledger = json.loads(self.ledger.read_text())
        self.assertEqual(len(ledger["leads"]), 1)
        run_file = json.loads(Path(second["run_file"]).read_text())
        self.assertEqual(run_file["leads"], [])

    def test_discovered_at_is_frozen_on_first_sight(self):
        self._run("org_795.json", run_timestamp="2026-07-14T09:12:00Z")
        self._run("org_795.json", run_timestamp="2026-08-01T00:00:00Z")
        for lead in json.loads(self.ledger.read_text())["leads"]:
            # First-run timestamp preserved, not overwritten by the later run.
            self.assertEqual(lead["discovered_at"], "2026-07-14T09:12:00Z")

    def test_missing_state_lead_is_refused_not_written(self):
        counts = self._run("org_999.json")
        self.assertEqual(counts["candidates"], 1)
        self.assertEqual(counts["new"], 0)
        self.assertEqual(counts["invalid"], 1)
        self.assertEqual(json.loads(self.ledger.read_text())["leads"], [])

    def test_evidence_quote_joins_match_contexts(self):
        self._run("org_795.json")
        by_id = {l["external_id"]: l for l in json.loads(self.ledger.read_text())["leads"]}
        lead = by_id[EXPECTED_ID_795_725242]
        # Two matches in the fixture -> both contexts present, joined by "; ".
        self.assertIn("; ", lead["evidence_quote"])
        self.assertEqual(lead["evidence"]["matched_terms"], ["artificial turf"])
        self.assertFalse(lead["evidence"]["needs_review"])


if __name__ == "__main__":
    unittest.main()
