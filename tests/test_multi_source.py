#!/usr/bin/env python3
"""
Tests for the multi-source plumbing around the (unchanged) lead contract:
  - scrape-state key namespacing + one-time legacy migration
  - district-directory loading (new format, legacy fallback) and merge rules
  - run_all_districts row loading / filename mapping
  - export identity resolution (record fields > legacy filename > overrides)
  - a non-BoardBook lead validates against the byte-identical schema, uses
    the record-stamped URLs, and carries platform provenance in
    evidence.details only

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
import fetch_org_directory as dir_tool  # noqa: E402
import run_all_districts  # noqa: E402
import scrape_state  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_PATH = REPO_ROOT / "contracts" / "lead.schema.json"


class StateNamespacingTest(unittest.TestCase):
    def test_org_key_format(self):
        self.assertEqual(scrape_state.org_key("boardbook", "795"), "boardbook:795")
        self.assertEqual(scrape_state.org_key("boarddocs", "ny/albany"), "boarddocs:ny/albany")

    def legacy_state(self):
        # The shape state/scrape_state.json had before multi-platform: bare
        # BoardBook org ids at the org level (org 795 with recorded meetings).
        return {
            "schema_version": "1.0",
            "orgs": {
                "795": {
                    "last_scraped_at": "2026-07-20T12:00:00Z",
                    "meetings": {
                        "725242": {"date": "February 5, 2026 at 6:15 PM",
                                   "agenda_processed": True, "turf_mentioned": True,
                                   "minutes_captured": True, "minutes_final": True},
                        "723526": {"date": "January 22, 2026 at 6:15 PM",
                                   "agenda_processed": True, "turf_mentioned": False,
                                   "minutes_captured": False, "minutes_final": True},
                    },
                },
            },
        }

    def test_legacy_file_is_migrated_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scrape_state.json"
            path.write_text(json.dumps(self.legacy_state()), encoding="utf-8")
            state = scrape_state.load_state(path)

        self.assertEqual(state["schema_version"], "2.0")
        self.assertNotIn("795", state["orgs"])
        migrated = state["orgs"]["boardbook:795"]
        # Every recorded meeting survives the migration.
        self.assertEqual(set(migrated["meetings"]), {"725242", "723526"})
        self.assertEqual(migrated["last_scraped_at"], "2026-07-20T12:00:00Z")
        # And the skip decision still applies through the namespaced key.
        entry = scrape_state.get_entry(state, "boardbook:795", "725242")
        self.assertEqual(scrape_state.should_process(entry)[1], "already_scraped")

    def test_migration_is_idempotent_and_leaves_namespaced_keys_alone(self):
        state = self.legacy_state()
        state["orgs"]["sparq:120"] = {"meetings": {"9": {"agenda_processed": True}}}
        self.assertTrue(scrape_state.migrate_legacy_org_keys(state))
        first = json.dumps(state, sort_keys=True)
        self.assertFalse(scrape_state.migrate_legacy_org_keys(state))
        self.assertEqual(json.dumps(state, sort_keys=True), first)
        self.assertIn("sparq:120", state["orgs"])

    def test_same_numeric_id_on_two_platforms_does_not_collide(self):
        state = {"schema_version": "2.0", "orgs": {}}
        scrape_state.record_result(state, scrape_state.org_key("boardbook", "120"),
                                   "1", "July 1, 2026", None, False, False)
        scrape_state.record_result(state, scrape_state.org_key("sparq", "120"),
                                   "1", "July 1, 2026", None, True, True)
        self.assertFalse(
            scrape_state.get_entry(state, "boardbook:120", "1")["turf_mentioned"])
        self.assertTrue(
            scrape_state.get_entry(state, "sparq:120", "1")["turf_mentioned"])


class DirectoryMergeTest(unittest.TestCase):
    def row(self, **kw):
        base = dir_tool.empty_row(kw.pop("platform", "sparq"), kw.pop("org", "120"))
        base.update(kw)
        return base

    def test_new_rows_are_appended(self):
        directory = {}
        self.assertEqual(dir_tool.merge_row(directory, self.row(org_name="Omaha"), "live"), "new")
        self.assertEqual(directory[("sparq", "120")]["org_name"], "Omaha")

    def test_live_refresh_updates_name_but_never_curation(self):
        directory = {}
        dir_tool.merge_row(directory, self.row(org_name="Old Name",
                                               include_in_rollout="False",
                                               county="Douglas County"), "seed")
        dir_tool.merge_row(directory, self.row(org_name="Omaha Public Schools",
                                               include_in_rollout="True"), "live")
        merged = directory[("sparq", "120")]
        self.assertEqual(merged["org_name"], "Omaha Public Schools")  # live wins
        self.assertEqual(merged["include_in_rollout"], "False")       # curation kept
        self.assertEqual(merged["county"], "Douglas County")

    def test_seed_overrides_curation_fields_and_fills_blanks(self):
        directory = {}
        dir_tool.merge_row(directory, self.row(org_name="Omaha Public Schools",
                                               include_in_rollout="False"), "live")
        dir_tool.merge_row(directory, self.row(include_in_rollout="True",
                                               state="NE", notes="NE pilot"), "seed")
        merged = directory[("sparq", "120")]
        self.assertEqual(merged["org_name"], "Omaha Public Schools")  # not clobbered
        self.assertEqual(merged["include_in_rollout"], "True")        # seed overrides
        self.assertEqual(merged["state"], "NE")                       # blank filled
        self.assertEqual(merged["notes"], "NE pilot")

    def test_boarddocs_state_derived_from_org_ref_prefix(self):
        directory = {}
        dir_tool.merge_row(directory, self.row(platform="boarddocs", org="oh/plsd"), "seed")
        self.assertEqual(directory[("boarddocs", "oh/plsd")]["state"], "OH")

    def test_legacy_org_directory_rows_become_boardbook_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "org_directory.csv"
            legacy.write_text(
                "org_id,org_name,organization_id,likely_school_district,include_in_rollout,state,county,notes\n"
                "795,Leander ISD,leander-isd-tx,True,True,TX,Williamson County,\n",
                encoding="utf-8",
            )
            rows = list(dir_tool.rows_from_legacy(legacy))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform"], "boardbook")
        self.assertEqual(rows[0]["platform_org_id"], "795")
        self.assertEqual(rows[0]["organization_id"], "leander-isd-tx")
        self.assertEqual(rows[0]["county"], "Williamson County")

    def test_directory_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "district_directory.csv"
            directory = {}
            dir_tool.merge_row(directory, self.row(org_name="Omaha Public Schools",
                                                   state="NE"), "live")
            dir_tool.save_directory(path, directory)
            self.assertEqual(dir_tool.load_directory(path), directory)


class RunAllDistrictsTest(unittest.TestCase):
    def test_new_directory_format_loads_platform_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "district_directory.csv"
            csv_path.write_text(
                "organization_id,org_name,state,county,counties,place,platform,platform_org_id,likely_school_district,include_in_rollout,notes\n"
                ",Omaha Public Schools,NE,Douglas County,,,sparq,120,True,True,\n"
                ",Excluded ISD,TX,,,,boardbook,999,True,False,\n"
                ",Albany CSD,NY,,,,boarddocs,ny/albany,True,True,\n",
                encoding="utf-8",
            )
            districts = run_all_districts.load_districts(csv_path)
        self.assertEqual(districts, [
            {"platform": "sparq", "org_id": "120", "org_name": "Omaha Public Schools"},
            {"platform": "boarddocs", "org_id": "ny/albany", "org_name": "Albany CSD"},
        ])

    def test_legacy_directory_format_still_loads_as_boardbook(self):
        districts = run_all_districts.load_districts(FIXTURES / "districts.csv")
        self.assertIn({"platform": "boardbook", "org_id": "795", "org_name": "Leander ISD"},
                      districts)

    def test_output_filenames_are_platform_namespaced_and_safe(self):
        self.assertEqual(run_all_districts.safe_org_filename("boardbook", "795"),
                         "boardbook_795")
        self.assertEqual(run_all_districts.safe_org_filename("boarddocs", "ny/albany"),
                         "boarddocs_ny-albany")

    def test_legacy_output_files_are_migrated_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "org_795.json").write_text("[]", encoding="utf-8")
            run_all_districts.migrate_legacy_output(out, "boardbook", "795", "boardbook_795")
            self.assertFalse((out / "org_795.json").exists())
            self.assertTrue((out / "boardbook_795.json").exists())
            # Idempotent: nothing left to migrate on a second call.
            run_all_districts.migrate_legacy_output(out, "boardbook", "795", "boardbook_795")


class ExportIdentityTest(unittest.TestCase):
    def test_record_fields_beat_filename(self):
        records = [{"meeting_id": "1", "platform": "sparq", "platform_org_id": "120"}]
        self.assertEqual(
            export_leads.identity_for_file(Path("whatever.json"), records),
            ("sparq", "120"),
        )

    def test_legacy_filename_maps_to_boardbook(self):
        self.assertEqual(
            export_leads.identity_for_file(Path("org_795.json"), [{"meeting_id": "1"}]),
            ("boardbook", "795"),
        )

    def test_override_wins_and_carries_platform(self):
        self.assertEqual(
            export_leads.identity_for_file(Path("x.json"), [], "kcs", "boeconnect"),
            ("boeconnect", "kcs"),
        )
        self.assertEqual(
            export_leads.identity_for_file(Path("x.json"), [], "795", None),
            ("boardbook", "795"),
        )

    def test_unidentifiable_file_returns_none(self):
        self.assertIsNone(export_leads.identity_for_file(Path("notes.json"), [{}]))

    def test_directory_loader_reads_both_formats(self):
        legacy = export_leads.load_org_directory(FIXTURES / "districts.csv")
        self.assertEqual(legacy[("boardbook", "795")]["org_name"], "Leander ISD")
        multi = export_leads.load_org_directory(FIXTURES / "districts_multi.csv")
        self.assertEqual(multi[("sparq", "120")]["org_name"], "Omaha Public Schools")
        self.assertEqual(multi[("boardbook", "795")]["organization_id"], "leander-isd-tx")


class NonBoardBookLeadExportTest(unittest.TestCase):
    """End-to-end export of a Sparq district: the lead must validate against
    the unchanged schema, deep-link via the record-stamped platform URLs, and
    carry platform provenance in evidence.details (nowhere else)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.ledger = tmp / "leads" / "ledger.json"
        self.exports = tmp / "exports"
        self.counts = export_leads.run_export(
            input_path=FIXTURES / "sparq_120.json",
            districts_csv=FIXTURES / "districts_multi.csv",
            ledger_path=self.ledger,
            exports_dir=self.exports,
            schema_path=SCHEMA_PATH,
            run_timestamp="2026-07-20T12:00:00Z",
        )
        self.leads = json.loads(self.ledger.read_text())["leads"]

    def tearDown(self):
        self._tmp.cleanup()

    def lead(self):
        self.assertEqual(len(self.leads), 1)
        return self.leads[0]

    def test_lead_validates_against_unchanged_schema(self):
        self.assertEqual(self.counts["new"], 1)
        self.assertEqual(self.counts["invalid"], 0)
        validator = Draft202012Validator(
            json.loads(SCHEMA_PATH.read_text()),
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        self.assertEqual(list(validator.iter_errors(self.lead())), [])

    def test_core_fields_are_platform_agnostic(self):
        lead = self.lead()
        self.assertEqual(lead["source"], "meeting-minutes")  # NOT the platform
        self.assertEqual(lead["organization"], "Omaha Public Schools")
        self.assertEqual(lead["state"], "NE")
        self.assertEqual(lead["county"], "Douglas")  # " County" suffix stripped
        self.assertEqual(lead["location_id"], "us-ne-meeting-minutes")

    def test_urls_come_from_the_records_not_boardbook(self):
        lead = self.lead()
        self.assertEqual(
            lead["source_url"],
            "https://meeting.sparqdata.com/Public/Minutes/120?meeting=749130",
        )
        for url in lead["evidence"]["source_urls"]:
            self.assertIn("meeting.sparqdata.com", url)
        self.assertIn("https://meeting.sparqdata.com/Public/Organization/120",
                      lead["evidence"]["source_urls"])

    def test_platform_provenance_lives_in_details_only(self):
        lead = self.lead()
        self.assertIn("platform=sparq org=120", lead["evidence"]["details"])
        # No new keys smuggled into the contract shapes.
        self.assertNotIn("platform", lead)
        self.assertNotIn("platform", lead["evidence"])


if __name__ == "__main__":
    unittest.main()
