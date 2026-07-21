#!/usr/bin/env python3
"""
Adapter for the BoardBook product family.

BoardBook Premier (meetings.boardbook.org), Sparq Meetings
(meeting.sparqdata.com, NASB's eMeeting for Nebraska) and BOEconnect
(meeting.boeconnect.net, TSBA's Tennessee offering) are the same white-labeled
application with identical public routes, so one adapter parameterized by base
URL serves all three:

    /Public                                  org directory (flat list, no paging)
    /Public/Organization/{org}               full meeting history for one org
    /Public/Agenda/{org}?meeting={id}        agenda HTML view
    /Public/DownloadAgenda/{org}?meeting={id}    agenda PDF (302-redirect when none)
    /Public/Minutes/{org}?meeting={id}       minutes HTML view
    /Public/DownloadMinutes/{org}?meeting={id}   minutes PDF (302-redirect when none)

Verified live (2026-07-20) on all three hosts:
  - org refs may be numeric ("795", "120") or slugs ("kcs", "papillion-lavista",
    "almaschools") and appear verbatim in the page's meeting links;
  - Sparq serves PDFs with Content-Type "application/PDF" (uppercase), so the
    content-type check must be case-insensitive;
  - a meeting row may expose Agenda, Minutes and/or PublicNotice links in any
    combination (some orgs, e.g. Papillion-La Vista on Sparq, post ONLY public
    notices) - discovery accepts any of the three, and a meeting without an
    agenda PDF keeps the existing "download_failed"/retry semantics.
"""
import re
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from .base import MeetingRef, PlatformAdapter, parse_display_date

# The three /Public routes whose links carry the meeting id. PublicNotice-only
# meetings are still real meetings (the org just posts no agenda/minutes PDF).
_MEETING_LINK_ROUTES = ("Agenda", "Minutes", "PublicNotice")


class BoardBookFamilyAdapter(PlatformAdapter):
    """One adapter, three registered instances: boardbook / sparq / boeconnect."""

    # Transparent research UA (verified accepted by all three hosts).
    user_agent = ("Mozilla/5.0 (compatible; MeetingMinuteResearchBot/1.0; "
                  "contact: n.basson@tencategrass.com)")
    min_request_interval = 0.0  # pacing stays with the caller's --sleep, as before

    def __init__(self, platform: str, base_url: str):
        super().__init__()
        self.platform = platform
        self.base_url = base_url.rstrip("/")

    # -- meeting list ----------------------------------------------------------

    def list_meetings(self, org_ref: str) -> List[MeetingRef]:
        url = f"{self.base_url}/Public/Organization/{org_ref}"
        resp = self._request("GET", url, timeout=30)
        resp.raise_for_status()
        return self._parse_meeting_rows(resp.text, org_ref)

    @staticmethod
    def _parse_meeting_rows(html: str, org_ref: str) -> List[MeetingRef]:
        soup = BeautifulSoup(html, "lxml")
        link_re = re.compile(
            r"/Public/(?:%s)/%s\?" % ("|".join(_MEETING_LINK_ROUTES), re.escape(str(org_ref)))
        )
        meetings, seen = [], set()
        for row in soup.select("tr.row-for-board"):
            meeting_id = None
            for link in row.find_all("a", href=True):
                href = link["href"]
                if not link_re.search(href):
                    continue
                m = re.search(r"[?&]meeting=([^&#]+)", href)
                if m:
                    meeting_id = m.group(1)
                    break
            if not meeting_id or meeting_id in seen:
                continue
            seen.add(meeting_id)

            first_cell = row.find("td")
            title_div = first_cell.find("div") if first_cell else None
            title_text = title_div.get_text(strip=True) if title_div else ""
            # e.g. "June 18, 2026 at 6:15 PM - Regular Meeting with Public Hearing"
            date_str, _, title = title_text.partition(" - ")
            meetings.append(
                MeetingRef(
                    meeting_id=meeting_id,
                    title=title.strip() or title_text,
                    date_str=date_str.strip(),
                    parsed_date=parse_display_date(date_str),
                )
            )
        return meetings

    # -- documents -------------------------------------------------------------

    def fetch_document(self, org_ref: str, meeting_id: str, kind: str) -> Optional[bytes]:
        route = "DownloadAgenda" if kind == "agenda" else "DownloadMinutes"
        url = f"{self.base_url}/Public/{route}/{org_ref}?meeting={meeting_id}"
        resp = self._request("GET", url, timeout=60)
        if resp.status_code != 200:
            return None
        # No document posted -> the app 302-redirects back to the org page and
        # we end up with HTML. Sparq spells the type "application/PDF", hence
        # the case-insensitive check.
        if "application/pdf" not in resp.headers.get("Content-Type", "").lower():
            return None
        return resp.content

    def document_page_url(self, org_ref: str, meeting_id: str, kind: str) -> str:
        route = "Agenda" if kind == "agenda" else "Minutes"
        return f"{self.base_url}/Public/{route}/{org_ref}?meeting={meeting_id}"

    def org_page_url(self, org_ref: str) -> str:
        return f"{self.base_url}/Public/Organization/{org_ref}"

    # -- org directory (this family exposes a public one) -----------------------

    def list_organizations(self) -> List[Tuple[str, str]]:
        """(org_ref, display name) for every org in the platform's /Public
        directory. Org refs may be numeric or slugs - kept verbatim."""
        resp = self._request("GET", f"{self.base_url}/Public", timeout=30)
        resp.raise_for_status()
        return self._parse_directory(resp.text)

    @staticmethod
    def _parse_directory(html: str) -> List[Tuple[str, str]]:
        soup = BeautifulSoup(html, "lxml")
        orgs, seen = [], set()
        for link in soup.select('a[href*="/Public/Organization/"]'):
            m = re.search(r"/Public/Organization/([^/?#\"]+)", link.get("href", ""))
            if not m:
                continue
            org_ref = m.group(1)
            if org_ref in seen:
                continue
            seen.add(org_ref)
            orgs.append((org_ref, link.get_text(strip=True)))
        return orgs
