#!/usr/bin/env python3
"""
Document-level scrape state: which meeting documents have already been
scraped, so a re-run does not download and analyze the same PDFs twice.

This concerns the meeting-minutes scraping only; the web-search agent
pipeline keeps its own re-run bookkeeping in its own repo.

The state lives in state/scrape_state.json (tracked in git, unlike output/),
keyed org_id -> meeting_id:

    {
      "schema_version": "1.0",
      "orgs": {
        "795": {
          "last_scraped_at": "2026-07-20T12:00:00Z",
          "meetings": {
            "725242": {
              "date": "February 5, 2026 at 6:15 PM",
              "first_scraped_at": "2026-07-20T12:00:00Z",
              "last_scraped_at": "2026-07-20T12:00:00Z",
              "agenda_processed": true,     # agenda PDF downloaded + analyzed
              "error": null,                # last error, e.g. "download_failed"
              "turf_mentioned": true,
              "minutes_captured": true,     # minutes PDF fetched for this hit
              "minutes_final": true         # no further minutes recheck needed
            }
          }
        }
      }
    }

Skip/recheck rules (see should_process):
  - A meeting never seen is processed.
  - A meeting whose document could not be fetched/parsed last time is retried
    (documents get posted late), until the meeting is older than the recheck
    window - then it is finalized and skipped for good.
  - A turf-hit meeting whose minutes were not yet posted is rechecked (the
    confirmed decision only exists once minutes appear), under the same
    recheck window.
  - Everything else was fully captured and is skipped.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

STATE_SCHEMA_VERSION = "1.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = REPO_ROOT / "state" / "scrape_state.json"

# A meeting older than this whose document/minutes never appeared is finalized:
# districts that have not posted by then realistically never will, and endless
# rechecks would defeat the point of the state file.
DEFAULT_RECHECK_DAYS = 180


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_meeting_date(date_str: str) -> Optional[datetime]:
    """Parse a BoardBook display date ('January 22, 2026 at 6:15 PM', possibly
    prefixed 'Cancelled'); None if it doesn't parse. Mirrors export_leads."""
    s = (date_str or "").strip()
    if s.startswith("Cancelled"):
        s = s[len("Cancelled"):].strip()
    s = s.split(" at ")[0].strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# --- load / save -------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        with path.open(encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("schema_version", STATE_SCHEMA_VERSION)
        state.setdefault("orgs", {})
        return state
    return {"schema_version": STATE_SCHEMA_VERSION, "orgs": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def org_state(state: dict, org_id: str) -> dict:
    return state["orgs"].setdefault(str(org_id), {"meetings": {}})


def get_entry(state: dict, org_id: str, meeting_id: str) -> Optional[dict]:
    return org_state(state, org_id)["meetings"].get(str(meeting_id))


# --- skip / recheck decision --------------------------------------------------

def _overdue(entry: dict, now: datetime, recheck_days: int) -> bool:
    """True when the meeting is old enough that a still-missing document or
    minutes PDF is not expected to appear anymore. An unparseable date is
    never overdue (keep rechecking rather than silently dropping it)."""
    meeting_date = parse_meeting_date(entry.get("date", ""))
    if meeting_date is None:
        return False
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    return (now_naive - meeting_date).days > recheck_days


def should_process(entry: Optional[dict], now: Optional[datetime] = None,
                   recheck_days: int = DEFAULT_RECHECK_DAYS) -> Tuple[bool, str]:
    """Decide whether a meeting needs (re)processing.

    Returns (process, reason). Reasons:
      new              never seen before
      retry_document   agenda fetch/parse failed last time; document may appear
      document_overdue document never appeared and the meeting is old; finalize
      recheck_minutes  turf hit, minutes not yet posted; decision still pending
      minutes_overdue  minutes never appeared and the meeting is old; finalize
      already_scraped  fully captured; nothing left to fetch
    """
    now = now or utc_now()
    if entry is None:
        return True, "new"
    if entry.get("minutes_final"):
        return False, "already_scraped"
    if not entry.get("agenda_processed"):
        if _overdue(entry, now, recheck_days):
            return False, "document_overdue"
        return True, "retry_document"
    if entry.get("turf_mentioned") and not entry.get("minutes_captured"):
        if _overdue(entry, now, recheck_days):
            return False, "minutes_overdue"
        return True, "recheck_minutes"
    return False, "already_scraped"


def mark_final(state: dict, org_id: str, meeting_id: str, reason: str,
               now: Optional[datetime] = None) -> None:
    """Finalize an overdue meeting so it is never rechecked again."""
    entry = get_entry(state, org_id, meeting_id)
    if entry is not None:
        entry["minutes_final"] = True
        entry["finalized_reason"] = reason
        entry["finalized_at"] = iso_z(now or utc_now())


# --- recording results --------------------------------------------------------

def record_result(state: dict, org_id: str, meeting_id: str, date_str: str,
                  error: Optional[str], turf_mentioned: bool,
                  minutes_captured: bool, now: Optional[datetime] = None) -> None:
    """Record one processed meeting document into the state.

    agenda_processed is True only for a clean run (no download/parse error);
    errored meetings stay retryable until they go overdue. minutes_final is
    True as soon as nothing further can be fetched for this meeting: a non-hit
    needs no minutes pass, and a hit with captured minutes is complete.
    """
    stamp = iso_z(now or utc_now())
    meetings = org_state(state, org_id)["meetings"]
    entry = meetings.setdefault(str(meeting_id), {"first_scraped_at": stamp})
    processed = error is None
    entry.update({
        "date": date_str,
        "last_scraped_at": stamp,
        "agenda_processed": processed,
        "error": error,
        "turf_mentioned": bool(turf_mentioned),
        "minutes_captured": bool(minutes_captured),
        "minutes_final": processed and (not turf_mentioned or bool(minutes_captured)),
    })


def touch_org(state: dict, org_id: str, now: Optional[datetime] = None) -> None:
    """Record when this org was last scraped (whether or not anything was new)."""
    org_state(state, org_id)["last_scraped_at"] = iso_z(now or utc_now())
