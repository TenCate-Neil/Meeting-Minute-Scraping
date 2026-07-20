#!/usr/bin/env python3
"""
Tests for the platform adapter layer (scripts/platforms/):
  - adapter registry dispatch (implemented / deferred / unknown)
  - BoardBook-family meeting-list + directory parsing (slug org ids,
    PublicNotice-only rows, Cancelled-prefixed dates, case-insensitive PDF
    content type)
  - BoardDocs meeting-list parsing from saved live fixtures (the XML feed is
    not well-formed; the JSON fallback entries) and org-ref validation
  - the shared extractor's PDF/HTML content sniffing

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_meetings  # noqa: E402
from platforms import (  # noqa: E402
    DEFERRED_PLATFORMS,
    FAMILY_BASE_URLS,
    BoardBookFamilyAdapter,
    BoardDocsAdapter,
    get_adapter,
    implemented_platforms,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, text=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text if text is not None else content.decode("utf-8", "replace")


class AdapterRegistryTest(unittest.TestCase):
    def test_each_implemented_platform_dispatches(self):
        for name in implemented_platforms():
            adapter = get_adapter(name)
            self.assertEqual(adapter.platform, name)

    def test_family_instances_use_their_own_base_url(self):
        for name, base in FAMILY_BASE_URLS.items():
            adapter = get_adapter(name)
            self.assertIsInstance(adapter, BoardBookFamilyAdapter)
            self.assertEqual(adapter.base_url, base)

    def test_boarddocs_dispatches_to_its_own_adapter(self):
        self.assertIsInstance(get_adapter("boarddocs"), BoardDocsAdapter)

    def test_deferred_platform_raises_with_explanation(self):
        for name in DEFERRED_PLATFORMS:
            with self.assertRaises(ValueError) as ctx:
                get_adapter(name)
            self.assertIn("deferred", str(ctx.exception))

    def test_unknown_platform_raises(self):
        with self.assertRaises(ValueError):
            get_adapter("civicclerk")

    def test_dispatch_normalizes_case_and_whitespace(self):
        self.assertEqual(get_adapter("  Sparq ").platform, "sparq")


class BoardBookFamilyParsingTest(unittest.TestCase):
    HTML = (FIXTURES / "boardbook_family_org_page.html").read_text()

    def meetings(self, org="kcs"):
        return BoardBookFamilyAdapter._parse_meeting_rows(self.HTML, org)

    def test_slug_org_rows_are_parsed(self):
        ids = [m.meeting_id for m in self.meetings()]
        self.assertEqual(ids, ["720021", "718829", "727804", "555001"])

    def test_meeting_discovered_from_publicnotice_only_row(self):
        # Some orgs (e.g. Papillion-La Vista on Sparq) post only PublicNotice
        # links; the meeting must still be discovered so a later agenda
        # posting is picked up by the retry logic.
        m = next(m for m in self.meetings() if m.meeting_id == "727804")
        self.assertEqual(m.title, "Board of Education Meeting")

    def test_cancelled_prefix_still_parses_date(self):
        m = next(m for m in self.meetings() if m.meeting_id == "555001")
        self.assertEqual(m.parsed_date, datetime(2021, 2, 11, 18, 15))

    def test_rows_for_other_orgs_are_not_matched(self):
        # Org id is part of the link filter: parsing the same page for a
        # different org must find nothing (slug ids are matched exactly).
        self.assertEqual(self.meetings(org="kc"), [])

    def test_directory_parses_numeric_and_slug_org_ids(self):
        html = (FIXTURES / "platform_directory_page.html").read_text()
        orgs = BoardBookFamilyAdapter._parse_directory(html)
        self.assertIn(("120", "Omaha Public Schools"), orgs)
        self.assertIn(("papillion-lavista", "Papillion La Vista Community Schools"), orgs)

    def test_pdf_content_type_check_is_case_insensitive(self):
        # Sparq serves "application/PDF" (verified live); the old lowercase-only
        # check silently dropped every Sparq document.
        adapter = get_adapter("sparq")
        adapter._request = lambda *a, **k: FakeResponse(
            headers={"Content-Type": "application/PDF"}, content=b"%PDF-1.7 fake")
        self.assertEqual(adapter.fetch_document("120", "1", "agenda"), b"%PDF-1.7 fake")

    def test_redirect_to_html_means_not_posted(self):
        adapter = get_adapter("boardbook")
        adapter._request = lambda *a, **k: FakeResponse(
            headers={"Content-Type": "text/html; charset=utf-8"}, content=b"<html></html>")
        self.assertIsNone(adapter.fetch_document("795", "1", "agenda"))

    def test_document_page_urls_match_platform_base(self):
        adapter = get_adapter("boeconnect")
        self.assertEqual(
            adapter.document_page_url("kcs", "718829", "minutes"),
            "https://meeting.boeconnect.net/Public/Minutes/kcs?meeting=718829",
        )
        self.assertEqual(adapter.org_page_url("kcs"),
                         "https://meeting.boeconnect.net/Public/Organization/kcs")


class BoardDocsParsingTest(unittest.TestCase):
    XML = (FIXTURES / "boarddocs_active_meetings.xml").read_text()

    def test_xml_meetings_survive_malformed_feed(self):
        # The fixture is cut from the live ny/albany feed and includes a block
        # with a stray </category> (the feed is NOT well-formed XML; a strict
        # parse dies and lxml recover drops most meetings).
        meetings = BoardDocsAdapter.parse_active_meetings_xml(self.XML)
        by_id = {m.meeting_id: m for m in meetings}
        self.assertEqual(len(meetings), 3)
        self.assertIn("B2QKQK526437", by_id)  # the malformed block
        self.assertEqual(by_id["B2QKQK526437"].parsed_date, datetime(2018, 7, 19))
        self.assertEqual(by_id["B2QKQK526437"].date_str, "July 19, 2018")

    def test_meeting_titles_are_unescaped_and_collapsed(self):
        meetings = BoardDocsAdapter.parse_active_meetings_xml(self.XML)
        for m in meetings:
            self.assertNotIn("&amp;", m.title)
            self.assertNotIn("\n", m.title)

    def test_list_meetings_sorts_most_recent_first(self):
        adapter = BoardDocsAdapter()
        adapter._request = lambda *a, **k: FakeResponse(text=self.XML)
        meetings = adapter.list_meetings("ny/albany")
        dates = [m.parsed_date for m in meetings]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_json_fallback_entries_parse(self):
        entries = json.loads((FIXTURES / "boarddocs_meetings_list.json").read_text())
        refs = [BoardDocsAdapter.parse_meetings_list_entry(e) for e in entries]
        self.assertTrue(all(r is not None for r in refs))
        first = refs[0]
        self.assertEqual(first.meeting_id, "DUGMQR5C6405")
        self.assertEqual(first.parsed_date, datetime(2026, 7, 9))

    def test_org_ref_must_be_state_slash_slug(self):
        adapter = BoardDocsAdapter()
        for bad in ("albany", "ny", "ny/", "/albany", "newyork/albany"):
            with self.assertRaises(ValueError):
                adapter._nsf(bad)
        self.assertEqual(adapter._nsf("NY/Albany"),
                         "https://go.boarddocs.com/ny/albany/Board.nsf")

    def test_empty_document_body_means_not_posted(self):
        # Verified live: BD-GetMinutes returns HTTP 200 with an empty body for
        # meetings without posted minutes (and for bogus ids).
        adapter = BoardDocsAdapter()
        adapter._request = lambda *a, **k: FakeResponse(content=b"")
        self.assertIsNone(adapter.fetch_document("ny/albany", "X", "minutes"))

    def test_document_page_url_is_the_goto_deep_link(self):
        adapter = BoardDocsAdapter()
        self.assertEqual(
            adapter.document_page_url("oh/plsd", "ABC123", "agenda"),
            "https://go.boarddocs.com/oh/plsd/Board.nsf/goto?open&id=ABC123",
        )


class ExtractTextSniffingTest(unittest.TestCase):
    def test_html_documents_extract_text(self):
        html = b"<html><body><div>Turf replacement <b>approved</b></div></body></html>"
        text, pages = scrape_meetings.extract_text(html)
        self.assertIn("Turf replacement", text)
        self.assertIn("approved", text)
        self.assertEqual(pages, 1)

    def test_unknown_format_raises_like_a_corrupt_pdf(self):
        with self.assertRaises(Exception):
            scrape_meetings.extract_text(b"\x00\x01\x02 not a document")


if __name__ == "__main__":
    unittest.main()
