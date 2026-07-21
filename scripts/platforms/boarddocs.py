#!/usr/bin/env python3
"""
Adapter for BoardDocs (Diligent), a different architecture from the BoardBook
family: a JS single-page front-end over Domino, with machine-readable
endpoints per client site. An org ref is "{state}/{slug}", e.g. "ny/albany"
or "oh/plsd" (the path under go.boarddocs.com).

Endpoints (verified live 2026-07-20; see docs/ARCHITECTURE.md):

    GET  {org}/Board.nsf/XML-ActiveMeetings
         XML list of every public meeting (full history: 334 meetings back to
         2015 for ny/albany), with ids, dates, names and inline agenda item
         names. CAUTION: the XML is not well-formed (stray </category> tags
         around meetings whose agenda items sit outside a category), so it is
         parsed by extracting <meeting>...</meeting> blocks with a regex - a
         strict XML parse dies at the first mismatch and lxml's recover mode
         silently drops most meetings (4 of 334 survived in testing).
    POST {org}/Board.nsf/BD-GetMeetingsList?open   form: current_committee_id
         JSON meeting list per committee - the fallback when the XML route
         fails. Committee ids come from <option value="..."> tags in the
         public page HTML ({org}/Board.nsf/Public).
    POST {org}/Board.nsf/PRINT-AgendaDetailed?open   form: id={meetingId}
         The detailed agenda as HTML (item names + full item body text).
    POST {org}/Board.nsf/BD-GetMinutes?open          form: id={meetingId}
         The approved minutes as HTML. An empty body means "not posted"
         (same for a bogus meeting id) - mapped to None, which the shared
         pipeline already treats as the document-not-posted case.

Documents are HTML, not PDF - the shared extractor sniffs the content type.
BoardDocs rejects the default python-requests User-Agent with HTTP 403, so
this adapter uses a normal browser UA and a conservative per-request delay.
"""
import html
import json
import re
from datetime import datetime
from typing import List, Optional

from .base import MeetingRef, PlatformAdapter

BASE_URL = "https://go.boarddocs.com"

# An org ref must be "{state}/{slug}" - a bare slug cannot be resolved.
ORG_REF_RE = re.compile(r"^[a-z]{2}/[A-Za-z0-9_.-]+$")

# <meeting ...>...</meeting> blocks, tolerant of the malformed inner structure.
_MEETING_BLOCK_RE = re.compile(r"<meeting\b[^>]*>.*?</meeting>", re.S)
_MEETING_ID_RE = re.compile(r'<meeting\b[^>]*\bid="([^"]+)"')
_NAME_RE = re.compile(r"<name>(.*?)</name>", re.S)
_ISO_DATE_RE = re.compile(r'<date format="yyyy-mm-dd">([^<]*)</date>')
_ENGLISH_DATE_RE = re.compile(r"<english>.*?<date>([^<]*)</date>", re.S)
_COMMITTEE_OPTION_RE = re.compile(r'<option[^>]+value="([A-Za-z0-9]+)"')


def _clean_name(raw: str) -> str:
    """Collapse whitespace and unescape entities in a meeting name."""
    return re.sub(r"\s+", " ", html.unescape(raw or "")).strip()


class BoardDocsAdapter(PlatformAdapter):
    platform = "boarddocs"
    user_agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    min_request_interval = 1.0  # conservative: BoardDocs sits behind a WAF

    def __init__(self, base_url: str = BASE_URL):
        super().__init__()
        self.base_url = base_url.rstrip("/")

    def _nsf(self, org_ref: str) -> str:
        org = str(org_ref).strip().strip("/").lower()
        if not ORG_REF_RE.match(org):
            raise ValueError(
                f"BoardDocs org ref must be 'state/slug' (e.g. 'ny/albany'), got {org_ref!r}"
            )
        return f"{self.base_url}/{org}/Board.nsf"

    # -- meeting list ----------------------------------------------------------

    def list_meetings(self, org_ref: str) -> List[MeetingRef]:
        meetings: List[MeetingRef] = []
        try:
            resp = self._request("GET", f"{self._nsf(org_ref)}/XML-ActiveMeetings",
                                 timeout=120)
            if resp.status_code == 200:
                meetings = self.parse_active_meetings_xml(resp.text)
        except OSError:
            meetings = []
        if not meetings:
            meetings = self._list_meetings_via_json(org_ref)
        # Most recent first, matching the BoardBook-family page order the rest
        # of the pipeline assumes for --limit. Unparseable dates sort last.
        meetings.sort(
            key=lambda m: m.parsed_date or datetime.min, reverse=True
        )
        return meetings

    @staticmethod
    def parse_active_meetings_xml(text: str) -> List[MeetingRef]:
        """Extract meetings from XML-ActiveMeetings without a strict XML parse
        (the feed is not well-formed - see module docstring)."""
        meetings, seen = [], set()
        for block in _MEETING_BLOCK_RE.findall(text):
            id_m = _MEETING_ID_RE.search(block)
            iso_m = _ISO_DATE_RE.search(block)
            if not id_m or id_m.group(1) in seen:
                continue
            seen.add(id_m.group(1))
            parsed = None
            if iso_m:
                try:
                    parsed = datetime.strptime(iso_m.group(1).strip(), "%Y-%m-%d")
                except ValueError:
                    parsed = None
            eng_m = _ENGLISH_DATE_RE.search(block)
            if eng_m:
                date_str = eng_m.group(1).strip()
            else:
                date_str = parsed.strftime("%B %d, %Y") if parsed else ""
            name_m = _NAME_RE.search(block)
            title = _clean_name(name_m.group(1)) if name_m else ""
            meetings.append(
                MeetingRef(
                    meeting_id=id_m.group(1),
                    title=title or date_str,
                    date_str=date_str,
                    parsed_date=parsed,
                )
            )
        return meetings

    def _list_meetings_via_json(self, org_ref: str) -> List[MeetingRef]:
        """Fallback: committee ids from the public page HTML, then one
        BD-GetMeetingsList POST per committee."""
        nsf = self._nsf(org_ref)
        resp = self._request("GET", f"{nsf}/Public", timeout=60)
        resp.raise_for_status()
        committee_ids = list(dict.fromkeys(_COMMITTEE_OPTION_RE.findall(resp.text)))

        meetings, seen = [], set()
        for committee_id in committee_ids:
            resp = self._request(
                "POST", f"{nsf}/BD-GetMeetingsList?open",
                data={"current_committee_id": committee_id}, timeout=60,
            )
            if resp.status_code != 200:
                continue
            try:
                entries = json.loads(resp.text)
            except json.JSONDecodeError:
                continue
            for entry in entries:
                meetings_from_entry = self.parse_meetings_list_entry(entry)
                if meetings_from_entry and meetings_from_entry.meeting_id not in seen:
                    seen.add(meetings_from_entry.meeting_id)
                    meetings.append(meetings_from_entry)
        return meetings

    @staticmethod
    def parse_meetings_list_entry(entry: dict) -> Optional[MeetingRef]:
        """One BD-GetMeetingsList JSON entry -> MeetingRef.
        Shape: {"unique": "DUGMQR5C6405", "name": "July 9, 2026",
                "numberdate": "20260709", ...}"""
        meeting_id = (entry.get("unique") or "").strip()
        if not meeting_id:
            return None
        parsed = None
        numberdate = (entry.get("numberdate") or "").strip()
        if numberdate:
            try:
                parsed = datetime.strptime(numberdate, "%Y%m%d")
            except ValueError:
                parsed = None
        date_str = parsed.strftime("%B %d, %Y") if parsed else ""
        title = _clean_name(entry.get("name") or "")
        return MeetingRef(
            meeting_id=meeting_id,
            title=title or date_str,
            date_str=date_str,
            parsed_date=parsed,
        )

    # -- documents -------------------------------------------------------------

    _DOCUMENT_ROUTES = {"agenda": "PRINT-AgendaDetailed", "minutes": "BD-GetMinutes"}

    def fetch_document(self, org_ref: str, meeting_id: str, kind: str) -> Optional[bytes]:
        route = self._DOCUMENT_ROUTES[kind]
        resp = self._request(
            "POST", f"{self._nsf(org_ref)}/{route}?open",
            data={"id": meeting_id}, timeout=90,
        )
        if resp.status_code != 200 or not resp.content.strip():
            # Empty 200 body = document not posted (verified for future
            # meetings and bogus ids alike).
            return None
        return resp.content

    def document_page_url(self, org_ref: str, meeting_id: str, kind: str) -> str:
        # BoardDocs has one public page per meeting (no separate agenda vs
        # minutes URL); goto?open&id= deep-links it in the SPA.
        return f"{self._nsf(org_ref)}/goto?open&id={meeting_id}"

    def org_page_url(self, org_ref: str) -> str:
        return f"{self._nsf(org_ref)}/Public"
