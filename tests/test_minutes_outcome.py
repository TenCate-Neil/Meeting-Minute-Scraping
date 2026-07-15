#!/usr/bin/env python3
"""
Tests for the hybrid minutes-confirmation pass:
  - pick_confirmed_outcome() decisiveness ordering (scrape_boardbook.py)
  - confirmed outcome flowing into the exported lead's evidence.details
    (export_leads.py)

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_boardbook as sb  # noqa: E402
import export_leads  # noqa: E402


class PickConfirmedOutcomeTest(unittest.TestCase):
    def test_no_turf_in_minutes_returns_none(self):
        self.assertIsNone(sb.pick_confirmed_outcome([]))

    def test_most_decisive_wins_regardless_of_order(self):
        self.assertEqual(
            sb.pick_confirmed_outcome(["Informational only", "Tabled", "Approved", "Denied"]),
            "Approved",
        )
        self.assertEqual(
            sb.pick_confirmed_outcome(["Informational only", "Denied", "Tabled"]),
            "Denied",
        )

    def test_informational_only_when_nothing_decisive(self):
        self.assertEqual(
            sb.pick_confirmed_outcome(["Informational only", "Informational only"]),
            "Informational only",
        )

    def test_confirm_outcome_from_minutes_text(self):
        # A minutes excerpt mentioning turf and a recorded vote -> Approved.
        text = (
            "1. Consider Approval of reallocation for Artificial Turf replacement. "
            "This motion passed six in favor and one opposed."
        )
        outcome, context = sb.confirm_outcome_from_minutes(text)
        self.assertEqual(outcome, "Approved")
        self.assertIn("Artificial Turf", context)

    def test_confirm_outcome_no_turf_mention(self):
        outcome, context = sb.confirm_outcome_from_minutes("Approval of the HR policy handbook.")
        self.assertIsNone(outcome)
        self.assertEqual(context, "")


class ExportSurfacesConfirmedOutcomeTest(unittest.TestCase):
    ORG_META = {"org_name": "Leander ISD", "state": "TX", "county": "Williamson County"}

    def _record(self, **extra):
        record = {
            "meeting_id": "723526",
            "date": "January 22, 2026 at 6:15 PM",
            "title": "Regular Meeting",
            "turf_mentioned": True,
            "matches": [{
                "term": "artificial turf",
                "context": "replacement of the artificial turf at the stadium",
                "topic_type": "Budget / capital expenditure",
                "sentiment": "Neutral / factual",
                "outcome": "Informational only",
            }],
            "summary": "1 turf-related mention(s) found (artificial turf).",
            "pages": 284,
        }
        record.update(extra)
        return record

    def _details(self, record):
        lead = export_leads.build_lead(
            record, "795", self.ORG_META,
            discovered_at="2026-07-14T09:12:00Z", run_stamp="20260714T091200Z",
        )
        return lead["evidence"]["details"]

    def test_confirmed_outcome_appears_in_details(self):
        details = self._details(self._record(minutes_available=True, minutes_outcome="Approved"))
        self.assertIn("Outcome per minutes (heuristic): Approved", details)

    def test_minutes_present_but_turf_not_found(self):
        details = self._details(self._record(minutes_available=True, minutes_outcome=None))
        self.assertIn("Outcome per minutes (heuristic): turf item not located in minutes", details)

    def test_no_minutes_no_confirmed_line(self):
        # Agenda-only meeting (no minutes pass) -> no minutes-outcome line.
        details = self._details(self._record())
        self.assertNotIn("per minutes", details)


if __name__ == "__main__":
    unittest.main()
