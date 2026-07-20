#!/usr/bin/env python3
"""
Platform adapter interface: the ONLY platform-specific part of the pipeline.

Everything downstream of fetching (PDF/HTML text extraction, turf-term
matching, minutes-outcome confirmation, scrape state, lead export, Supabase
sync) is shared across platforms and lives in scripts/scrape_meetings.py and
friends. A new source platform is added by writing one adapter subclass and
registering it in scripts/platforms/__init__.py - nothing else changes.

An adapter answers exactly three questions:
  - list_meetings(org_ref)                what meetings does this org have?
  - fetch_document(org_ref, id, kind)     give me the agenda/minutes bytes
                                          (None when not posted - the caller's
                                          "download_failed" semantics)
  - document_page_url / org_page_url      human-facing deep links recorded in
                                          output records and lead evidence

org_ref is always a STRING and is platform-scoped: a BoardBook-family org id
may be numeric ("795") or a slug ("kcs", "papillion-lavista"); a BoardDocs
org ref is "{state}/{slug}" (e.g. "ny/albany"). Meeting ids from different
platforms may collide numerically - never compare them across platforms
without the platform namespace (see scripts/scrape_state.py org keys).
"""
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import requests


@dataclass
class MeetingRef:
    """One meeting as listed by a platform: platform-scoped id, display date
    string, parsed date (None when the display string doesn't parse), and the
    meeting name/type."""
    meeting_id: str
    title: str
    date_str: str
    parsed_date: Optional[datetime]


# The two document kinds the shared pipeline understands.
DOCUMENT_KINDS = ("agenda", "minutes")


def parse_display_date(date_str: str) -> Optional[datetime]:
    """Parse a display date like 'June 18, 2026 at 6:15 PM' or 'June 18, 2026'.

    A leading 'Cancelled' prefix (BoardBook renders cancelled meetings as
    'CancelledFebruary 11, 2021 at 6:15 PM') is stripped so cancelled meetings
    still carry a parseable date and --start-date/--end-date filtering applies
    to them like any other meeting.
    """
    s = (date_str or "").strip()
    if s.startswith("Cancelled"):
        s = s[len("Cancelled"):].strip()
    for fmt in ("%B %d, %Y at %I:%M %p", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class PlatformAdapter:
    """Base class for platform adapters. Subclasses set `platform`, `user_agent`
    and `min_request_interval`, and implement the three fetch/URL methods."""

    platform: str = ""
    user_agent: str = "Mozilla/5.0"
    # Seconds enforced between any two HTTP requests made through this adapter
    # (on top of whatever per-meeting --sleep the caller adds). Platforms that
    # need conservative pacing (BoardDocs) raise this.
    min_request_interval: float = 0.0

    def __init__(self):
        self._session: Optional[requests.Session] = None
        self._last_request_at = 0.0

    # -- plumbing -------------------------------------------------------------

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent})
        return self._session

    def _throttle(self) -> None:
        if self.min_request_interval <= 0:
            return
        wait = self._last_request_at + self.min_request_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """All adapter HTTP goes through here so the per-adapter rate limit is
        enforced uniformly."""
        self._throttle()
        try:
            return self.session.request(method, url, **kwargs)
        finally:
            self._last_request_at = time.monotonic()

    # -- the adapter contract -------------------------------------------------

    def list_meetings(self, org_ref: str) -> List[MeetingRef]:
        """All meetings the platform lists for this org, most recent first
        where the platform provides an order."""
        raise NotImplementedError

    def fetch_document(self, org_ref: str, meeting_id: str, kind: str) -> Optional[bytes]:
        """Raw document bytes (PDF or HTML - the shared extractor sniffs), or
        None when the platform has no such document posted for the meeting."""
        raise NotImplementedError

    def document_page_url(self, org_ref: str, meeting_id: str, kind: str) -> str:
        """Human-facing deep link for this meeting document (recorded in output
        records and used as lead source_url)."""
        raise NotImplementedError

    def org_page_url(self, org_ref: str) -> str:
        """Human-facing link to the org's public meeting listing."""
        raise NotImplementedError
