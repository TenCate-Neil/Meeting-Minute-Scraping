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
    matches: list = field(default_factory=list)  # list of {term, context}
    error: Optional[str] = None
    pages: int = 0
    pdf_bytes: int = 0


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
    """Download the agenda/minutes PDF for a given meeting. Returns raw PDF bytes or None."""
    url = f"{BASE_URL}/Public/DownloadAgenda/{org_id}?meeting={meeting_id}"
    resp = session.get(url, timeout=60)
    if resp.status_code != 200:
        return None
    if "application/pdf" not in resp.headers.get("Content-Type", ""):
        return None
    return resp.content


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
        matches.append({"term": m.group(), "context": text[start:end].strip()})

    return AnalysisResult(
        meeting=meeting,
        turf_mentioned=len(matches) > 0,
        matches=matches,
        pages=pages,
        pdf_bytes=pdf_bytes,
    )


def process_meeting(
    session: requests.Session,
    org_id: str,
    meeting: MeetingRef,
    keep_pdfs: bool,
    pdf_dir: Path,
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
                lines.append(f"  [{i}] Term: '{match['term']}'")
                lines.append(f"      Context: ...{match['context']}...")
        lines.append("")

    return "\n".join(lines)


def write_csv(results: list, path: Path):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["meeting_id", "date", "title", "turf_mentioned", "num_matches", "pages", "error"])
        for r in results:
            writer.writerow(
                [
                    r.meeting.meeting_id,
                    r.meeting.date_str,
                    r.meeting.title,
                    r.turf_mentioned,
                    len(r.matches),
                    r.pages,
                    r.error or "",
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
    args = parser.parse_args()

    session = get_session()

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

    pdf_dir = Path(args.pdf_dir)
    results = []
    for i, meeting in enumerate(meetings, 1):
        print(f"[{i}/{len(meetings)}] {meeting.date_str} - {meeting.title} (id={meeting.meeting_id})", file=sys.stderr)
        result = process_meeting(session, args.org, meeting, args.keep_pdfs, pdf_dir)
        results.append(result)
        if result.turf_mentioned:
            print(f"  -> TURF MENTIONED ({len(result.matches)} match(es))", file=sys.stderr)
        if i < len(meetings):
            time.sleep(args.sleep)

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "meeting_id": r.meeting.meeting_id,
                    "date": r.meeting.date_str,
                    "title": r.meeting.title,
                    "turf_mentioned": r.turf_mentioned,
                    "matches": r.matches,
                    "error": r.error,
                    "pages": r.pages,
                }
                for r in results
            ],
            f,
            indent=2,
        )

    out_csv = Path(args.out_csv)
    write_csv(results, out_csv)

    report = format_report(results)
    print(report)
    print(f"\nJSON written to: {out_json}", file=sys.stderr)
    print(f"CSV written to: {out_csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
