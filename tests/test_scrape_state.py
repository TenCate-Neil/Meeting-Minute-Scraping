#!/usr/bin/env python3
"""
Tests for the document-level scrape state that prevents double scraping:
  - should_process() skip/recheck decisions (scrape_state.py)
  - record_result() flag derivation and state round-trip
  - merge_records() carrying forward skipped meetings (scrape_boardbook.py)

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_state  # noqa: E402
import scrape_boardbook as sb  # noqa: E402

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def entry(days_ago=10, **overrides):
    """A state entry for a meeting `days_ago` days before NOW."""
    date = (NOW - timedelta(days=days_ago)).strftime("%B %d, %Y") + " at 6:15 PM"
    base = {
        "date": date,
        "first_scraped_at": "2026-07-01T00:00:00Z",
        "last_scraped_at": "2026-07-01T00:00:00Z",
        "agenda_processed": True,
        "error": None,
        "turf_mentioned": False,
        "minutes_captured": False,
        "minutes_final": True,
    }
    base.update(overrides)
    return base


class ShouldProcessTest(unittest.TestCase):
    def test_unseen_meeting_is_processed(self):
        self.assertEqual(scrape_state.should_process(None, NOW), (True, "new"))

    def test_fully_captured_meeting_is_skipped(self):
        process, reason = scrape_state.should_process(entry(), NOW)
        self.assertFalse(process)
        self.assertEqual(reason, "already_scraped")

    def test_failed_download_is_retried_while_recent(self):
        e = entry(days_ago=30, agenda_processed=False, error="download_failed",
                  minutes_final=False)
        self.assertEqual(scrape_state.should_process(e, NOW), (True, "retry_document"))

    def test_failed_download_finalized_when_overdue(self):
        e = entry(days_ago=400, agenda_processed=False, error="download_failed",
                  minutes_final=False)
        self.assertEqual(scrape_state.should_process(e, NOW), (False, "document_overdue"))

    def test_turf_hit_without_minutes_is_rechecked(self):
        # The confirmed decision only exists once minutes are posted, so a
        # turf hit whose minutes were missing must be looked at again.
        e = entry(days_ago=30, turf_mentioned=True, minutes_captured=False,
                  minutes_final=False)
        self.assertEqual(scrape_state.should_process(e, NOW), (True, "recheck_minutes"))

    def test_turf_hit_without_minutes_finalized_when_overdue(self):
        e = entry(days_ago=400, turf_mentioned=True, minutes_captured=False,
                  minutes_final=False)
        self.assertEqual(scrape_state.should_process(e, NOW), (False, "minutes_overdue"))

    def test_turf_hit_with_captured_minutes_is_skipped(self):
        e = entry(days_ago=30, turf_mentioned=True, minutes_captured=True)
        process, reason = scrape_state.should_process(e, NOW)
        self.assertFalse(process)
        self.assertEqual(reason, "already_scraped")

    def test_finalized_entry_is_never_rechecked(self):
        e = entry(days_ago=30, turf_mentioned=True, minutes_captured=False,
                  minutes_final=True)
        self.assertEqual(scrape_state.should_process(e, NOW),
                         (False, "already_scraped"))

    def test_unparseable_date_keeps_rechecking(self):
        e = entry(agenda_processed=False, error="download_failed",
                  minutes_final=False, date="(manual)")
        self.assertEqual(scrape_state.should_process(e, NOW), (True, "retry_document"))

    def test_recheck_window_is_configurable(self):
        e = entry(days_ago=30, turf_mentioned=True, minutes_captured=False,
                  minutes_final=False)
        self.assertEqual(scrape_state.should_process(e, NOW, recheck_days=7),
                         (False, "minutes_overdue"))


class RecordResultTest(unittest.TestCase):
    def fresh_state(self):
        return {"schema_version": "1.0", "orgs": {}}

    def test_clean_non_hit_is_final(self):
        state = self.fresh_state()
        scrape_state.record_result(state, "795", "1", "July 1, 2026 at 6:15 PM",
                                   error=None, turf_mentioned=False,
                                   minutes_captured=False, now=NOW)
        e = scrape_state.get_entry(state, "795", "1")
        self.assertTrue(e["agenda_processed"])
        self.assertTrue(e["minutes_final"])
        self.assertEqual(scrape_state.should_process(e, NOW), (False, "already_scraped"))

    def test_hit_without_minutes_stays_open(self):
        state = self.fresh_state()
        scrape_state.record_result(state, "795", "2", "July 1, 2026 at 6:15 PM",
                                   error=None, turf_mentioned=True,
                                   minutes_captured=False, now=NOW)
        e = scrape_state.get_entry(state, "795", "2")
        self.assertFalse(e["minutes_final"])
        self.assertEqual(scrape_state.should_process(e, NOW), (True, "recheck_minutes"))

    def test_hit_with_minutes_is_final(self):
        state = self.fresh_state()
        scrape_state.record_result(state, "795", "3", "July 1, 2026 at 6:15 PM",
                                   error=None, turf_mentioned=True,
                                   minutes_captured=True, now=NOW)
        self.assertTrue(scrape_state.get_entry(state, "795", "3")["minutes_final"])

    def test_errored_meeting_stays_retryable(self):
        state = self.fresh_state()
        scrape_state.record_result(state, "795", "4", "July 1, 2026 at 6:15 PM",
                                   error="download_failed", turf_mentioned=False,
                                   minutes_captured=False, now=NOW)
        e = scrape_state.get_entry(state, "795", "4")
        self.assertFalse(e["agenda_processed"])
        self.assertEqual(scrape_state.should_process(e, NOW), (True, "retry_document"))

    def test_rerecording_preserves_first_scraped_at(self):
        state = self.fresh_state()
        earlier = NOW - timedelta(days=7)
        scrape_state.record_result(state, "795", "5", "July 1, 2026 at 6:15 PM",
                                   error="download_failed", turf_mentioned=False,
                                   minutes_captured=False, now=earlier)
        scrape_state.record_result(state, "795", "5", "July 1, 2026 at 6:15 PM",
                                   error=None, turf_mentioned=False,
                                   minutes_captured=False, now=NOW)
        e = scrape_state.get_entry(state, "795", "5")
        self.assertEqual(e["first_scraped_at"], scrape_state.iso_z(earlier))
        self.assertEqual(e["last_scraped_at"], scrape_state.iso_z(NOW))
        self.assertTrue(e["minutes_final"])

    def test_mark_final_stops_rechecks(self):
        state = self.fresh_state()
        scrape_state.record_result(state, "795", "6", "July 1, 2026 at 6:15 PM",
                                   error="download_failed", turf_mentioned=False,
                                   minutes_captured=False, now=NOW)
        scrape_state.mark_final(state, "795", "6", "document_overdue", NOW)
        e = scrape_state.get_entry(state, "795", "6")
        self.assertEqual(scrape_state.should_process(e, NOW), (False, "already_scraped"))

    def test_save_and_load_round_trip(self):
        state = self.fresh_state()
        scrape_state.record_result(state, "795", "7", "July 1, 2026 at 6:15 PM",
                                   error=None, turf_mentioned=True,
                                   minutes_captured=True, now=NOW)
        scrape_state.touch_org(state, "795", NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "scrape_state.json"
            scrape_state.save_state(path, state)
            loaded = scrape_state.load_state(path)
        self.assertEqual(loaded, state)
        self.assertEqual(loaded["orgs"]["795"]["last_scraped_at"],
                         scrape_state.iso_z(NOW))

    def test_load_missing_file_gives_empty_state(self):
        state = scrape_state.load_state(Path("/nonexistent/scrape_state.json"))
        self.assertEqual(state, {"schema_version": "1.0", "orgs": {}})


class MergeRecordsTest(unittest.TestCase):
    def rec(self, mid, **overrides):
        r = {"meeting_id": mid, "turf_mentioned": False, "matches": []}
        r.update(overrides)
        return r

    def test_new_records_replace_prior_in_place(self):
        prior = [self.rec("1"), self.rec("2", turf_mentioned=True)]
        new = [self.rec("2", turf_mentioned=True, minutes_available=True)]
        merged = sb.merge_records(prior, new)
        self.assertEqual([r["meeting_id"] for r in merged], ["1", "2"])
        self.assertTrue(merged[1]["minutes_available"])

    def test_skipped_meetings_are_carried_forward(self):
        prior = [self.rec("1", turf_mentioned=True)]
        merged = sb.merge_records(prior, [self.rec("3")])
        self.assertEqual([r["meeting_id"] for r in merged], ["1", "3"])
        self.assertTrue(merged[0]["turf_mentioned"])

    def test_no_prior_file_keeps_new_records(self):
        new = [self.rec("1"), self.rec("2")]
        self.assertEqual(sb.merge_records([], new), new)


if __name__ == "__main__":
    unittest.main()
