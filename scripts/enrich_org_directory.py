#!/usr/bin/env python3
"""
Add `state` and `county` columns to districts/org_directory.csv.

BoardBook's organization directory (/Public) has no state/county field of
its own (see docs/ARCHITECTURE.md). This script derives both by:

  1. Fetching each org's page (/Public/Organization/{id}) and pulling the
     first physical meeting address it links to (a maps.google.com URL
     embedded next to the meeting location).
  2. Parsing the state directly out of that address string.
  3. Sending the address to the US Census Bureau's free public geocoder
     (no API key required) to resolve the county.

Not every organization has a posted meeting with an address (some have no
meetings yet, or use a format this script's regex doesn't catch) - those
rows are left blank rather than guessed, so gaps are visible for manual
follow-up.

This is a slow, network-bound script (two HTTP requests per org: one to
BoardBook, one to the Census geocoder) and writes progress back to the CSV
after every row, so it can be safely interrupted and resumed - rows that
already have a non-empty `state` are skipped on the next run unless
--force is passed.

Usage:
    python3 scripts/enrich_org_directory.py --csv districts/org_directory.csv
    python3 scripts/enrich_org_directory.py --csv districts/org_directory.csv --limit 20   # smoke test
    python3 scripts/enrich_org_directory.py --csv districts/org_directory.csv --force      # re-resolve everything
"""
import argparse
import csv
import re
import sys
import time
import urllib.parse

import requests

BOARDBOOK_BASE = "https://meetings.boardbook.org"
CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
USER_AGENT = "Mozilla/5.0 (compatible; MeetingMinuteResearchBot/1.0; contact: n.basson@tencategrass.com)"

ADDRESS_LINK_RE = re.compile(r"maps\.google\.com/\?q=([^'\"]+)")

# Matches ", TX 79311" or ", Texas 78613" style endings - the two forms
# observed on BoardBook org pages.
STATE_ABBR_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}(-\d{4})?\s*$")
STATE_NAME_RE = re.compile(
    r",\s*(Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
    r"Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|"
    r"Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|"
    r"Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|"
    r"North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
    r"South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|"
    r"Wisconsin|Wyoming)\s+\d{5}(-\d{4})?\s*$",
    re.IGNORECASE,
)

STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}


def fetch_first_address(session: requests.Session, org_id: str) -> str | None:
    resp = session.get(f"{BOARDBOOK_BASE}/Public/Organization/{org_id}", timeout=30)
    resp.raise_for_status()
    m = ADDRESS_LINK_RE.search(resp.text)
    if not m:
        return None
    return urllib.parse.unquote_plus(m.group(1))


def parse_state(address: str) -> str | None:
    """Always returns the 2-letter USPS abbreviation, regardless of which
    form (abbreviation or full name) appeared in the source address."""
    m = STATE_ABBR_RE.search(address)
    if m:
        return m.group(1).upper()
    m = STATE_NAME_RE.search(address)
    if m:
        return STATE_NAME_TO_ABBR.get(m.group(1).lower())
    return None


def fetch_county(session: requests.Session, address: str) -> str | None:
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    resp = session.get(CENSUS_GEOCODER, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    counties = matches[0].get("geographies", {}).get("Counties", [])
    if not counties:
        return None
    return counties[0].get("NAME")


def main():
    parser = argparse.ArgumentParser(description="Add state/county columns to the org directory CSV.")
    parser.add_argument("--csv", default="districts/org_directory.csv", help="Directory CSV to enrich in place")
    parser.add_argument("--limit", type=int, help="Only process the first N rows needing enrichment (smoke test)")
    parser.add_argument("--sleep", type=float, default=0.3, help="Delay between orgs, in seconds")
    parser.add_argument("--force", action="store_true", help="Re-resolve rows that already have a state/county")
    args = parser.parse_args()

    with open(args.csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    fieldnames = list(rows[0].keys())
    for col in ("state", "county"):
        if col not in fieldnames:
            fieldnames.append(col)
        for row in rows:
            row.setdefault(col, "")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    def save():
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    processed = 0
    for row in rows:
        if not args.force and row.get("state"):
            continue
        if args.limit and processed >= args.limit:
            break

        org_id = row["org_id"]
        org_name = row["org_name"]
        try:
            address = fetch_first_address(session, org_id)
            if not address:
                print(f"[{org_id}] {org_name}: no address found on org page", file=sys.stderr)
                row["state"], row["county"] = "", ""
            else:
                state = parse_state(address) or ""
                county = ""
                try:
                    county = fetch_county(session, address) or ""
                except requests.RequestException as e:
                    print(f"[{org_id}] {org_name}: geocoder request failed ({e})", file=sys.stderr)
                row["state"], row["county"] = state, county
                print(f"[{org_id}] {org_name}: state={state!r} county={county!r} (from '{address}')", file=sys.stderr)
        except requests.RequestException as e:
            print(f"[{org_id}] {org_name}: FAILED to fetch org page ({e})", file=sys.stderr)

        processed += 1
        if processed % 25 == 0:
            save()
        time.sleep(args.sleep)

    save()
    print(f"\nProcessed {processed} org(s). Wrote {args.csv}.", file=sys.stderr)


if __name__ == "__main__":
    main()
