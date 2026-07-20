#!/usr/bin/env python3
"""
Scrape and analyze meeting agenda/minutes PDFs from a BoardBook public
organization portal (e.g. https://meetings.boardbook.org/Public/Organization/795).

Usage:
    python3 scrape_boardbook.py --org 795 --limit 10
    python3 scrape_boardbook.py --org 795 --start-date 2024-01-01 --end-date 2026-12-31
    python3 scrape_boardbook.py --org 795 --meeting-id 452677   # single meeting test

By default, downloaded PDFs are analyzed in-memory and then deleted
(see STORAGE NOTE at the bottom of this file / README). Pass --keep-pdfs to
retain them on disk for audit purposes.
"""
import argparse
import csv
import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader

import scrape_state

BASE_URL = "https://meetings.boardbook.org"
USER_AGENT = "Mozilla/5.0 (compatible; MeetingMinuteResearchBot/1.0; contact: n.basson@tencategrass.com)"

# Search terms live in code (mirrors instructions/analysis_instructions.md).
# Keep both in sync if you edit the term list.
TURF_TERMS = [
    "artificial turf",
    "synthetic turf",
    "turf field",
    "turf fields",
    "artificial grass",
    "synthetic grass",
    "field turf",
    "fieldturf",
    "astroturf",
    "turf infill",
    "turf system",
    "turf replacement",
    "turf install",
    "turf installation",
    "sports turf",
]
TURF_PATTERN = re.compile("|".join(re.escape(t) for t in TURF_TERMS), re.IGNORECASE)

# Keyword-based heuristic classifier for the fields analysis_instructions.md
# asks for beyond term matching (topic type / sentiment / outcome). This is
# a rule-based first pass, not a semantic judgment - low-confidence or
# ambiguous contexts should still get a human/LLM read of the quoted excerpt
# before anything is reported externally. See docs/ARCHITECTURE.md.
TOPIC_KEYWORDS = [
    ("Procurement / bid / contract award", ("bid", "rfp", "proposal", "contract award", "vendor", "procurement", "purchase order", "awarded to")),
    ("Budget / capital expenditure", ("budget", "capital", "expenditure", "fund", "cost estimate", "appropriat", "bond")),
    ("Facility construction or renovation project", ("construct", "renovat", "install", "build", "project", "stadium", "complex")),
    ("Maintenance / replacement discussion", ("maintenance", "replace", "wear", "lifespan", "end of life", "repair")),
    ("Policy or safety discussion (e.g., heat, injury, environmental)", ("safety", "injury", "heat", "temperature", "environmental", "health", "hazard", "policy")),
]

SENTIMENT_KEYWORDS = {
    "positive": ("support", "durab", "cost saving", "benefit", "praise", "successful", "improve", "favor", "excited", "pleased"),
    "negative": ("concern", "oppos", "expensive", "costly", "risk", "injury", "hazard", "complaint", "reject", "against", "problem"),
}

OUTCOME_KEYWORDS = [
    ("Approved", ("approved", "passed", "carried", "unanimously")),
    ("Denied", ("denied", "rejected", "failed", "voted down")),
    ("Tabled", ("tabled", "postponed", "deferred")),
    ("Motion made (pending/unspecified result)", ("motion to", "motion by", "moved to")),
]

# Decisiveness order, most decisive first. Used to pick a single confirmed
# outcome from the minutes when a turf item was discussed (see the hybrid
# minutes pass in process_meeting).
OUTCOME_PRIORITY = [
    "Approved",
    "Denied",
    "Tabled",
    "Motion made (pending/unspecified result)",
    "Informational only",
]


def pick_confirmed_outcome(outcomes: list) -> Optional[str]:
    """Given the outcomes classified from turf mentions in the minutes, return
    the single most decisive one (Approved > Denied > Tabled > Motion > Info),
    or None if there were no turf mentions to classify in the minutes.
    """
    present = set(outcomes)
    for label in OUTCOME_PRIORITY:
        if label in present:
            return label
    return None


def classify_match(context: str) -> dict:
    """Heuristic keyword classification of a matched excerpt.

    Returns topic_type / sentiment / outcome per the categories defined in
    instructions/analysis_instructions.md. This is intentionally simple
    (substring matching, not NLP) - treat it as a triage aid, not a final
    verdict; re-read the quoted context yourself for anything reported
    outside this pipeline.
    """
    lower = context.lower()

    topic_type = "General mention / informational only"
    for label, keywords in TOPIC_KEYWORDS:
        if any(kw in lower for kw in keywords):
            topic_type = label
            break

    has_pos = any(kw in lower for kw in SENTIMENT_KEYWORDS["positive"])
    has_neg = any(kw in lower for kw in SENTIMENT_KEYWORDS["negative"])
    if has_pos and has_neg:
        sentiment = "Mixed"
    elif has_pos:
        sentiment = "Positive"
    elif has_neg:
        sentiment = "Negative"
    else:
        sentiment = "Neutral / factual"

    outcome = "Informational only"
    for label, keywords in OUTCOME_KEYWORDS:
        if any(kw in lower for kw in keywords):
            outcome = label
            break

    return {"topic_type": topic_type, "sentiment": sentiment, "outcome": outcome}


def summarize_document(meeting: "MeetingRef", matches: list) -> str:
    if not matches:
        return ""
    terms = sorted({m["term"] for m in matches})
    topics = sorted({m["topic_type"] for m in matches})
    sentiments = sorted({m["sentiment"] for m in matches})
    return (
        f"{len(matches)} turf-related mention(s) found ({', '.join(terms)}); "
        f"topic(s): {', '.join(topics)}; sentiment(s): {', '.join(sentiments)}."
    )


@dataclass
class MeetingRef:
    meeting_id: str
    title: str
    date_str: str
    parsed_date: Optional[datetime]


@dataclass
class AnalysisResult:
    meeting: MeetingRef
    turf_mentioned: bool
    matches: list = field(default_factory=list)  # list of {term, context, topic_type, sentiment, outcome}
    summary: str = ""
    error: Optional[str] = None
    pages: int = 0
    pdf_bytes: int = 0
    # Hybrid minutes pass: only populated for turf hits (see process_meeting).
    # The agenda says what was proposed; the minutes say what was decided.
    minutes_available: bool = False          # a Minutes PDF was posted and fetched
    minutes_outcome: Optional[str] = None    # confirmed decision from the minutes
    minutes_context: str = ""                # quoted turf context from the minutes
    minutes_pages: int = 0


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch_meeting_list(session: requests.Session, org_id: str) -> list:
    """Fetch the organization's public meeting list page and parse all meeting rows."""
    url = f"{BASE_URL}/Public/Organization/{org_id}"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    rows = soup.select("tr.row-for-board")

    meetings = []
    for row in rows:
        link = row.select_one(f'a[href*="/Public/Agenda/{org_id}"]')
        if not link:
            continue
        href = link.get("href", "")
        m = re.search(r"meeting=(\d+)", href)
        if not m:
            continue
        meeting_id = m.group(1)

        first_cell = row.find("td")
        title_div = first_cell.find("div") if first_cell else None
        title_text = title_div.get_text(strip=True) if title_div else ""

        # title_text looks like: "June 18, 2026 at 6:15 PM - Regular Meeting with Public Hearing"
        date_str, _, title = title_text.partition(" - ")
        parsed_date = None
        for fmt in ("%B %d, %Y at %I:%M %p", "%B %d, %Y"):
            try:
                # strip "at H:MM AM/PM" portion if present for date-only parse fallback
                parsed_date = datetime.strptime(date_str.strip(), fmt)
                break
            except ValueError:
                continue

        meetings.append(
            MeetingRef(
                meeting_id=meeting_id,
                title=title.strip() or title_text,
                date_str=date_str.strip(),
                parsed_date=parsed_date,
            )
        )

    return meetings


def download_agenda_pdf(session: requests.Session, org_id: str, meeting_id: str) -> Optional[bytes]:
    """Download the agenda PDF for a given meeting. Returns raw PDF bytes or None."""
    url = f"{BASE_URL}/Public/DownloadAgenda/{org_id}?meeting={meeting_id}"
    resp = session.get(url, timeout=60)
    if resp.status_code != 200:
        return None
    if "application/pdf" not in resp.headers.get("Content-Type", ""):
        return None
    return resp.content


def download_minutes_pdf(session: requests.Session, org_id: str, meeting_id: str) -> Optional[bytes]:
    """Download the *minutes* PDF for a given meeting, if one was posted.

    Mirrors download_agenda_pdf but hits /Public/DownloadMinutes/. Only ~54% of
    meetings have minutes; for the rest this returns None (non-PDF / redirect),
    exactly like a meeting with no posted agenda.
    """
    url = f"{BASE_URL}/Public/DownloadMinutes/{org_id}?meeting={meeting_id}"
    resp = session.get(url, timeout=60)
    if resp.status_code != 200:
        return None
    if "application/pdf" not in resp.headers.get("Content-Type", ""):
        return None
    return resp.content


# Minutes use a WIDER context window than the agenda's ~200 chars. In minutes
# the recorded vote ("...motion carried, six in favor and one opposed") sits
# further from the turf term than in an agenda: minutes typically open with a
# table-of-contents block (item titles, no outcomes) and record the actual
# motions later, so a tight window lands in the TOC and misses the decision.
# Empirically, +-200 mislabels the Leander turf vote as "Informational only"
# while +-400 and up correctly recover "Approved"; 500 is a stable midpoint.
MINUTES_CONTEXT_WINDOW = 500


def confirm_outcome_from_minutes(text: str) -> tuple:
    """Locate turf mentions in the minutes text and return the confirmed
    decision. Returns (outcome, context): outcome is the most decisive label
    per pick_confirmed_outcome (or None if turf isn't mentioned in the
    minutes), and context is the excerpt that produced that outcome, so the
    quote actually shows the decision rather than a table-of-contents line.

    Still a keyword heuristic, not semantic understanding: it can key on
    background/recommendation language near the turf term (e.g. "the committee
    unanimously approved...") rather than the board's actual vote. Treat
    minutes_outcome as a hint and read minutes_context before citing it.
    """
    hits = []  # (outcome, context) per turf mention in the minutes
    for m in TURF_PATTERN.finditer(text):
        start = max(0, m.start() - MINUTES_CONTEXT_WINDOW)
        end = min(len(text), m.end() + MINUTES_CONTEXT_WINDOW)
        context = text[start:end].strip()
        hits.append((classify_match(context)["outcome"], context))
    if not hits:
        return None, ""
    confirmed = pick_confirmed_outcome([outcome for outcome, _ in hits])
    context = next((c for outcome, c in hits if outcome == confirmed), hits[0][1])
    return confirmed, context


def extract_text(pdf_bytes: bytes) -> tuple:
    """Return (full_text, page_count) from PDF bytes."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    for i, page in enumerate(reader.pages):
        t = page.extract_text() or ""
        if t:
            parts.append(f"[Page {i + 1}] {t}")
    return "\n".join(parts), len(reader.pages)


def analyze_text(meeting: MeetingRef, text: str, pages: int, pdf_bytes: int) -> AnalysisResult:
    matches = []
    for m in TURF_PATTERN.finditer(text):
        start = max(0, m.start() - 200)
        end = min(len(text), m.end() + 200)
        context = text[start:end].strip()
        matches.append({"term": m.group(), "context": context, **classify_match(context)})

    return AnalysisResult(
        meeting=meeting,
        turf_mentioned=len(matches) > 0,
        matches=matches,
        summary=summarize_document(meeting, matches),
        pages=pages,
        pdf_bytes=pdf_bytes,
    )


def process_meeting(
    session: requests.Session,
    org_id: str,
    meeting: MeetingRef,
    keep_pdfs: bool,
    pdf_dir: Path,
    skip_minutes: bool = False,
) -> AnalysisResult:
    pdf_bytes = download_agenda_pdf(session, org_id, meeting.meeting_id)
    if pdf_bytes is None:
        return AnalysisResult(meeting=meeting, turf_mentioned=False, error="download_failed")

    try:
        text, pages = extract_text(pdf_bytes)
    except Exception as e:  # malformed/encrypted PDF, etc.
        return AnalysisResult(meeting=meeting, turf_mentioned=False, error=f"parse_error: {e}")

    result = analyze_text(meeting, text, pages, len(pdf_bytes))

    if keep_pdfs:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        safe_title = re.sub(r"[^A-Za-z0-9_\-]+", "_", meeting.date_str)[:60]
        out_path = pdf_dir / f"{meeting.meeting_id}_{safe_title}.pdf"
        out_path.write_bytes(pdf_bytes)
    # else: pdf_bytes goes out of scope here and is garbage-collected;
    # nothing touches disk. See STORAGE NOTE below.

    # Hybrid minutes pass: only for turf hits, and only when minutes exist.
    # The agenda is scraped for every meeting (discovery); the minutes are
    # fetched just for the handful of hits to confirm what was decided.
    if result.turf_mentioned and not skip_minutes:
        minutes_bytes = download_minutes_pdf(session, org_id, meeting.meeting_id)
        if minutes_bytes is not None:
            result.minutes_available = True
            try:
                minutes_text, minutes_pages = extract_text(minutes_bytes)
                outcome, context = confirm_outcome_from_minutes(minutes_text)
                result.minutes_outcome = outcome
                result.minutes_context = context
                result.minutes_pages = minutes_pages
            except Exception:
                # Minutes existed but couldn't be parsed; leave outcome unset.
                # minutes_available stays True so this is distinguishable from
                # "no minutes posted".
                pass
            if keep_pdfs:
                safe_title = re.sub(r"[^A-Za-z0-9_\-]+", "_", meeting.date_str)[:60]
                (pdf_dir / f"{meeting.meeting_id}_{safe_title}_minutes.pdf").write_bytes(minutes_bytes)

    return result


def format_report(results: list) -> str:
    lines = []
    hits = [r for r in results if r.turf_mentioned]
    errors = [r for r in results if r.error]

    lines.append(f"Analyzed {len(results)} meeting document(s).")
    lines.append(f"Turf mentions found in {len(hits)} document(s).")
    lines.append(f"Errors/skipped: {len(errors)} document(s).")
    lines.append("")

    for r in results:
        lines.append(f"Document: {r.meeting.date_str} - {r.meeting.title} (meeting={r.meeting.meeting_id})")
        if r.error:
            lines.append(f"  Status: ERROR - {r.error}")
            lines.append("")
            continue
        lines.append(f"Turf mentioned: {'Yes' if r.turf_mentioned else 'No'}")
        if r.turf_mentioned:
            for i, match in enumerate(r.matches, 1):
                lines.append(f"  - Item {i}: Term '{match['term']}'")
                lines.append(f"    Context: \"...{match['context']}...\"")
                lines.append(f"    Topic type: {match['topic_type']}")
                lines.append(f"    Sentiment: {match['sentiment']}")
                lines.append(f"    Outcome: {match['outcome']}")
            lines.append(f"Summary: {r.summary}")
            if r.minutes_available:
                confirmed = r.minutes_outcome or "turf item not located in minutes"
                lines.append(f"Outcome per minutes (heuristic): {confirmed}")
        lines.append("")

    return "\n".join(lines)


def result_to_record(r: AnalysisResult) -> dict:
    """The per-document JSON record shape written to the output file."""
    return {
        "meeting_id": r.meeting.meeting_id,
        "date": r.meeting.date_str,
        "title": r.meeting.title,
        "turf_mentioned": r.turf_mentioned,
        "matches": r.matches,
        "summary": r.summary,
        "error": r.error,
        "pages": r.pages,
        "minutes_available": r.minutes_available,
        "minutes_outcome": r.minutes_outcome,
        "minutes_context": r.minutes_context,
        "minutes_pages": r.minutes_pages,
    }


def merge_records(prior: list, new: list) -> list:
    """Merge this run's records into the records of an existing output file.

    Keyed by meeting_id: a reprocessed meeting replaces its old record in
    place, meetings skipped this run keep their carried-forward record, and
    brand-new meetings are appended. This keeps the output file cumulative,
    so meetings skipped via the scrape state stay visible to export_leads.py.
    """
    new_by_id = {str(r.get("meeting_id")): r for r in new}
    merged = []
    seen = set()
    for r in prior:
        mid = str(r.get("meeting_id"))
        merged.append(new_by_id.get(mid, r))
        seen.add(mid)
    merged.extend(r for r in new if str(r.get("meeting_id")) not in seen)
    return merged


def write_csv(records: list, path: Path):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["meeting_id", "date", "title", "turf_mentioned", "num_matches", "topic_types", "sentiments", "outcomes", "minutes_outcome", "summary", "pages", "error"]
        )
        for r in records:
            matches = r.get("matches") or []
            writer.writerow(
                [
                    r.get("meeting_id"),
                    r.get("date"),
                    r.get("title"),
                    r.get("turf_mentioned"),
                    len(matches),
                    "; ".join(sorted({m["topic_type"] for m in matches})),
                    "; ".join(sorted({m["sentiment"] for m in matches})),
                    "; ".join(sorted({m["outcome"] for m in matches})),
                    r.get("minutes_outcome") or "",
                    r.get("summary"),
                    r.get("pages"),
                    r.get("error") or "",
                ]
            )


def main():
    parser = argparse.ArgumentParser(description="Scrape and analyze BoardBook meeting PDFs for turf mentions.")
    parser.add_argument("--org", required=True, help="BoardBook organization ID, e.g. 795")
    parser.add_argument("--meeting-id", help="Process a single meeting ID only (for testing)")
    parser.add_argument("--limit", type=int, help="Limit number of meetings processed (most recent first)")
    parser.add_argument("--start-date", help="Only include meetings on/after this date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Only include meetings on/before this date (YYYY-MM-DD)")
    parser.add_argument("--keep-pdfs", action="store_true", help="Persist downloaded PDFs to disk instead of discarding them")
    parser.add_argument("--pdf-dir", default="output/pdfs", help="Directory to store PDFs if --keep-pdfs is set")
    parser.add_argument("--out-json", default="output/results.json", help="Path to write JSON results")
    parser.add_argument("--out-csv", default="output/results.csv", help="Path to write CSV summary")
    parser.add_argument("--sleep", type=float, default=0.5, help="Seconds to sleep between requests (politeness delay)")
    parser.add_argument("--skip-minutes", action="store_true",
                        help="Do not fetch the minutes to confirm outcomes for turf hits "
                             "(by default, meetings with a turf mention also pull the "
                             "minutes PDF, when one exists, to record the decision)")
    parser.add_argument("--state-file", default=str(scrape_state.DEFAULT_STATE_FILE),
                        help="Tracked scrape-state JSON that records what was scraped when, "
                             "so re-runs skip documents already captured (see scripts/scrape_state.py)")
    parser.add_argument("--no-state", action="store_true",
                        help="Do not read or write the scrape state (process everything, remember nothing)")
    parser.add_argument("--force-rescrape", action="store_true",
                        help="Ignore the scrape state's skip decisions but still record results into it")
    parser.add_argument("--minutes-recheck-days", type=int, default=scrape_state.DEFAULT_RECHECK_DAYS,
                        help="Stop rechecking a meeting whose document/minutes never appeared "
                             "once the meeting is older than this many days")
    args = parser.parse_args()

    session = get_session()

    state = None
    state_path = Path(args.state_file)
    if not args.no_state:
        state = scrape_state.load_state(state_path)
    now = scrape_state.utc_now()

    if args.meeting_id:
        meetings = [MeetingRef(meeting_id=args.meeting_id, title="(manual)", date_str="(manual)", parsed_date=None)]
    else:
        print(f"Fetching meeting list for org {args.org}...", file=sys.stderr)
        meetings = fetch_meeting_list(session, args.org)
        print(f"Found {len(meetings)} meetings.", file=sys.stderr)

        if args.start_date:
            start = datetime.strptime(args.start_date, "%Y-%m-%d")
            meetings = [m for m in meetings if m.parsed_date is None or m.parsed_date >= start]
        if args.end_date:
            end = datetime.strptime(args.end_date, "%Y-%m-%d")
            meetings = [m for m in meetings if m.parsed_date is None or m.parsed_date <= end]
        if args.limit:
            meetings = meetings[: args.limit]

    # Skip meetings the state says are fully captured. An explicit --meeting-id
    # is always processed (it is a deliberate re-run of one meeting).
    if state is not None and not args.meeting_id and not args.force_rescrape:
        to_process = []
        skipped = 0
        for m in meetings:
            entry = scrape_state.get_entry(state, args.org, m.meeting_id)
            process, reason = scrape_state.should_process(entry, now, args.minutes_recheck_days)
            if process:
                to_process.append(m)
                continue
            if reason in ("document_overdue", "minutes_overdue"):
                scrape_state.mark_final(state, args.org, m.meeting_id, reason, now)
            skipped += 1
        if skipped:
            print(
                f"Skipping {skipped} already-scraped meeting(s) per {state_path} "
                f"(--force-rescrape to override).",
                file=sys.stderr,
            )
        meetings = to_process

    pdf_dir = Path(args.pdf_dir)
    results = []
    for i, meeting in enumerate(meetings, 1):
        print(f"[{i}/{len(meetings)}] {meeting.date_str} - {meeting.title} (id={meeting.meeting_id})", file=sys.stderr)
        result = process_meeting(session, args.org, meeting, args.keep_pdfs, pdf_dir, args.skip_minutes)
        results.append(result)
        if state is not None:
            scrape_state.record_result(
                state, args.org, meeting.meeting_id, meeting.date_str,
                result.error, result.turf_mentioned, result.minutes_available, now,
            )
        if result.turf_mentioned:
            confirmed = ""
            if result.minutes_available:
                confirmed = f", minutes outcome: {result.minutes_outcome or 'not found'}"
            print(f"  -> TURF MENTIONED ({len(result.matches)} match(es){confirmed})", file=sys.stderr)
        if i < len(meetings):
            time.sleep(args.sleep)

    if state is not None:
        scrape_state.touch_org(state, args.org, now)
        scrape_state.save_state(state_path, state)

    # Merge into an existing output file so records for meetings skipped via
    # the scrape state are carried forward and export_leads.py still sees the
    # org's full turf-hit history, not just this run's new documents.
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    prior_records = []
    if out_json.exists():
        try:
            with out_json.open(encoding="utf-8") as f:
                prior_records = json.load(f)
        except (json.JSONDecodeError, OSError):
            prior_records = []
        if not isinstance(prior_records, list):
            prior_records = []
    records = merge_records(prior_records, [result_to_record(r) for r in results])
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)

    out_csv = Path(args.out_csv)
    write_csv(records, out_csv)

    report = format_report(results)
    print(report)
    print(f"\nJSON written to: {out_json} ({len(records)} record(s), {len(results)} from this run)", file=sys.stderr)
    print(f"CSV written to: {out_csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
