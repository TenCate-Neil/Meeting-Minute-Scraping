#!/usr/bin/env python3
"""
Fetch the master list of BoardBook organizations from the public directory
page and classify each as a likely school district or "other" entity
(library, college, county government, etc.).

BoardBook hosts many entity types on the same platform - this script exists
so a human can review/curate districts/org_directory.csv before the rollout
scraper (run_all_districts.py) is pointed at it.

Usage:
    python3 fetch_org_directory.py --out districts/org_directory.csv
"""
import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://meetings.boardbook.org"
USER_AGENT = "Mozilla/5.0 (compatible; MeetingMinuteResearchBot/1.0; contact: n.basson@tencategrass.com)"

# Heuristic only - always spot-check districts/org_directory.csv by hand
# before a production rollout. False positives/negatives are expected
# (e.g. "Anderson-Shiro CISD" needs the CISD/UISD variants below; entries
# like "Alpena-Montmorency-Alcona ESD" are regional education agencies,
# not single districts, and may need separate handling).
SCHOOL_PATTERN = re.compile(
    r"\bI\.?S\.?D\.?\b|\bC\.?I\.?S\.?D\.?\b|\bU\.?I\.?S\.?D\.?\b|"
    r"school district|\bschools?\b|academy|charter|\bESD\b|\bRESA\b",
    re.IGNORECASE,
)


def fetch_directory(session: requests.Session) -> list:
    resp = session.get(f"{BASE_URL}/Public", timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    orgs = []
    for link in soup.select('a[href*="/Public/Organization/"]'):
        href = link.get("href", "")
        m = re.search(r"Organization/(\d+)", href)
        if not m:
            continue
        org_id = m.group(1)
        name = link.get_text(strip=True)
        orgs.append((org_id, name))

    return orgs


def main():
    parser = argparse.ArgumentParser(description="Fetch and classify the BoardBook organization directory.")
    parser.add_argument("--out", default="districts/org_directory.csv", help="Output CSV path")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print("Fetching organization directory from https://meetings.boardbook.org/Public ...", file=sys.stderr)
    orgs = fetch_directory(session)
    print(f"Found {len(orgs)} organizations.", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    school_count = 0
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["org_id", "org_name", "likely_school_district", "include_in_rollout"])
        for org_id, name in orgs:
            is_school = bool(SCHOOL_PATTERN.search(name))
            if is_school:
                school_count += 1
            # include_in_rollout mirrors the heuristic by default; a human
            # reviewer edits this column directly to opt entities in/out.
            writer.writerow([org_id, name, is_school, is_school])

    print(f"Wrote {len(orgs)} rows to {out_path} ({school_count} flagged as likely school districts).", file=sys.stderr)
    print("Review the 'include_in_rollout' column before running run_all_districts.py.", file=sys.stderr)


if __name__ == "__main__":
    main()
