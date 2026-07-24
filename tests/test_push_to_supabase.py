#!/usr/bin/env python3
"""
Tests for the Supabase lead_entry sync (sync/push_to_supabase.py):
  - ledger -> row transform (payload columns + evidence JSONB, nothing else)
  - the never-send guarantee (BDM / resolution / enrichment columns stay out
    of the payload, which is what preserves them across re-syncs)
  - normalized_url derivation and the location_id null override
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
        # Retired slug older ledger records may still carry; the sync must
        # override it with null (geography resolves via organization_id).
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


def rows_for(leads: list) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        return pts.entry_rows(write_ledger(tmp, leads))


class EntryRowsTest(unittest.TestCase):
    def test_payload_columns_and_evidence(self):
        rows = rows_for([make_lead()])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), set(pts.PAYLOAD_FIELDS) | {"evidence"})
        self.assertEqual(row["entry_id"], "23e580ae7f87c63c")
        self.assertNotIn("external_id", row)
        self.assertEqual(row["source"], "meeting-minutes")
        self.assertEqual(row["evidence"]["project_name"],
                         "Leander ISD - artificial turf replacement")

    def test_never_send_columns_stay_out_of_the_row(self):
        # merge-duplicates updates only the columns present in the body, so
        # keeping these out of the payload is what preserves BDM review
        # state, the resolution job's linkage, and the enrichment agent's
        # lead_value_estimation across re-syncs. Even a ledger record that
        # somehow carried them must not leak them into the row.
        junk = {col: "junk" for col in pts.NEVER_SEND if col != "external_id"}
        rows = rows_for([make_lead(**junk)])
        leaked = set(rows[0]) & set(pts.NEVER_SEND)
        self.assertEqual(leaked, set())

    def test_payload_and_never_send_are_disjoint(self):
        self.assertEqual(set(pts.PAYLOAD_FIELDS) & set(pts.NEVER_SEND), set())

    def test_location_id_always_null(self):
        # The ledger fixture carries the retired us-tx-meeting-minutes slug;
        # the row must send null regardless.
        rows = rows_for([make_lead()])
        self.assertIn("location_id", rows[0])
        self.assertIsNone(rows[0]["location_id"])

    def test_missing_bid_due_date_sent_as_null(self):
        rows = rows_for([make_lead()])
        self.assertIn("bid_due_date", rows[0])
        self.assertIsNone(rows[0]["bid_due_date"])

    def test_rows_have_uniform_keys(self):
        # PostgREST rejects bulk bodies whose objects have different keys, so
        # a lead missing optional fields must produce the same key set.
        sparse = make_lead(external_id="a" * 16)
        del sparse["location_id"]
        del sparse["organization_id"]
        rows = rows_for([make_lead(), sparse])
        self.assertEqual(set(rows[0]), set(rows[1]))

    def test_empty_ledger_gives_no_rows(self):
        self.assertEqual(rows_for([]), [])


class NormalizeUrlTest(unittest.TestCase):
    def test_boardbook_url_keeps_path_case_and_query(self):
        self.assertEqual(
            pts.normalize_url("https://meetings.boardbook.org/Public/Agenda/795?meeting=725242"),
            "https://meetings.boardbook.org/Public/Agenda/795?meeting=725242")

    def test_host_lowercased_fragment_and_default_port_dropped(self):
        self.assertEqual(
            pts.normalize_url("HTTPS://MEETINGS.Boardbook.org:443/Public/Agenda/795?meeting=1#minutes"),
            "https://meetings.boardbook.org/Public/Agenda/795?meeting=1")

    def test_missing_url_is_none(self):
        self.assertIsNone(pts.normalize_url(None))
        self.assertIsNone(pts.normalize_url(""))

    def test_row_carries_normalized_url(self):
        rows = rows_for([make_lead(source_url="HTTPS://Example.test/Minutes#top")])
        self.assertEqual(rows[0]["normalized_url"], "https://example.test/Minutes")


class OrgPreflightTest(unittest.TestCase):
    def test_known_org_id_is_kept(self):
        rows = [{"entry_id": "a" * 16, "organization_id": "leander-isd-tx"}]
        warnings = pts.apply_org_preflight(rows, {"leander-isd-tx"})
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["organization_id"], "leander-isd-tx")

    def test_unknown_org_id_is_nulled_with_warning(self):
        rows = [{"entry_id": "a" * 16, "organization_id": "unregistered-isd-tx"}]
        warnings = pts.apply_org_preflight(rows, {"leander-isd-tx"})
        self.assertIsNone(rows[0]["organization_id"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("unregistered-isd-tx", warnings[0])

    def test_absent_org_id_passes_untouched(self):
        rows = [{"entry_id": "a" * 16, "organization_id": None}]
        self.assertEqual(pts.apply_org_preflight(rows, set()), [])
        self.assertIsNone(rows[0]["organization_id"])


if __name__ == "__main__":
    unittest.main()
