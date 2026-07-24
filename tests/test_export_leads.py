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

# Pinned so the frozen external_id recipe can't drift silently. This is
# sha1("Leander ISD|Leander ISD - artificial turf replacement|")[:16], i.e. the
# lead the org_795 fixture produces. Changing this duplicates every lead in the
# database, so this test failing is a red flag.
EXPECTED_ID_795 = "23e580ae7f87c63c"

# The exact external_id recipe the web-search pipeline uses, taken from real
# leads in its ledger. This locks the two pipelines' upsert keys together
# byte-for-byte: (organization, project_name, project_address) -> external_id.
WEB_PIPELINE_LEADS = [
    ("Leander ISD", "Bible Stadium - turf replacement, football/soccer field",
     "3301 S Bagdad Rd, Leander, TX 78641", "5c024f743861f427"),
    ("Round Rock ISD", "Chisholm Trail MS - competition football/soccer turf replacement",
     "500 Oakridge Drive, Round Rock, TX 78681", "15cba1457029d357"),
    ("Leander ISD", "LISD High Schools - baseball/softball turf installation",
     "", "1d3cb0f9ecf08208"),
]


class ExportLeadsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.ledger = tmp / "leads" / "ledger.json"
        self.exports = tmp / "exports"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, input_file, run_timestamp="2026-07-14T09:12:00Z", registry=None):
        return export_leads.run_export(
            input_path=FIXTURES / input_file,
            districts_csv=DISTRICTS_CSV,
            ledger_path=self.ledger,
            exports_dir=self.exports,
            schema_path=SCHEMA_PATH,
            run_timestamp=run_timestamp,
            org_registry_path=(FIXTURES / registry) if registry else None,
        )

    def _lead(self, ext_id=EXPECTED_ID_795):
        by_id = {l["external_id"]: l for l in json.loads(self.ledger.read_text())["leads"]}
        return by_id[ext_id]

    def test_mapped_lead_validates_against_schema(self):
        # Fixture has 2 records but only 1 turf hit -> aggregated into 1 project.
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
            # location_id is not emitted: geography resolves platform-side
            # via organization_id -> organization_geography.
            self.assertNotIn("location_id", lead)
            # source_url must deep-link the specific meeting, never bare boardbook.
            self.assertIn("795?meeting=", lead["source_url"])

    def test_external_id_matches_web_pipeline_byte_for_byte(self):
        # The recipe must reproduce real web-pipeline ledger ids exactly.
        for organization, project_name, project_address, expected in WEB_PIPELINE_LEADS:
            self.assertEqual(
                export_leads.compute_external_id(organization, project_name, project_address),
                expected,
                f"{organization} / {project_name}",
            )

    def test_external_id_is_stable_and_pinned(self):
        # Same inputs -> same id; surrounding whitespace is stripped.
        a = export_leads.compute_external_id(
            "Leander ISD", "Leander ISD - artificial turf replacement", "")
        b = export_leads.compute_external_id(
            "  Leander ISD ", " Leander ISD - artificial turf replacement ", "  ")
        self.assertEqual(a, b)
        self.assertEqual(a, EXPECTED_ID_795)

        self._run("org_795.json")
        ids = {l["external_id"] for l in json.loads(self.ledger.read_text())["leads"]}
        self.assertIn(EXPECTED_ID_795, ids)

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

    def test_non_sport_turf_produces_no_lead(self):
        # Landscaping/courtyard turf is not football/soccer/baseball-softball,
        # so the sport gate drops it: a candidate never forms.
        counts = self._run("org_888.json")
        self.assertEqual(counts["candidates"], 0)
        self.assertEqual(counts["new"], 0)
        self.assertEqual(counts["skipped_non_sport"], 1)
        self.assertEqual(json.loads(self.ledger.read_text())["leads"], [])

    def test_evidence_quote_is_single_verbatim_line(self):
        self._run("org_795.json")
        lead = self._lead()
        # A single verbatim context line (not a joined blob), drawn from the doc.
        fixture = json.loads((FIXTURES / "org_795.json").read_text())
        contexts = [
            export_leads.trim_one_line(m["context"], export_leads.EVIDENCE_QUOTE_MAX)
            for rec in fixture for m in (rec.get("matches") or [])
        ]
        self.assertIn(lead["evidence_quote"], contexts)
        self.assertEqual(lead["evidence"]["matched_terms"], ["artificial turf"])
        # org_id came from the directory column -> reconciled, no review flag.
        self.assertFalse(lead["evidence"]["needs_review"])

    def test_organization_id_from_directory_column(self):
        # The agent-leading organization_id column drives the id (no registry).
        self._run("org_795.json")
        self.assertEqual(self._lead()["organization_id"], "leander-isd-tx")

    def test_county_suffix_stripped_to_match_web_search_repo(self):
        # org_directory has "Williamson County"; the shared convention is "Williamson".
        self._run("org_795.json")
        self.assertEqual(self._lead()["county"], "Williamson")

    def test_registry_supplies_id_and_county_when_org_matches(self):
        # With the shared registry, id + county come from it and agree with web-search.
        counts = self._run("org_795.json", registry="registry.json")
        self.assertEqual(counts["unreconciled"], 0)
        lead = self._lead()
        self.assertEqual(lead["organization_id"], "leander-isd-tx")
        self.assertEqual(lead["county"], "Williamson")
        self.assertFalse(lead["evidence"]["needs_review"])

    def test_org_absent_from_registry_is_flagged_for_reconciliation(self):
        # A registry that does NOT list Leander -> lead still exports, but is
        # flagged needs_review so it gets reconciled (not silently divergent).
        counts = self._run("org_795.json", registry="registry_without_leander.json")
        self.assertEqual(counts["new"], 1)
        self.assertEqual(counts["unreconciled"], 1)
        lead = self._lead()
        self.assertEqual(lead["organization_id"], "leander-isd-tx")  # best-effort
        self.assertTrue(lead["evidence"]["needs_review"])
        self.assertIn("registry", lead["evidence"]["details"].lower())


if __name__ == "__main__":
    unittest.main()
