#!/usr/bin/env python3
"""
Fill `org_name`, `state` and `county` gaps in the district directory.

None of the source platforms expose state/county fields (see
docs/ARCHITECTURE.md), so this script derives them from each org's own
posted address:

  - BoardBook family (boardbook/sparq/boeconnect): fetch the org page
    (/Public/Organization/{id}, base URL per the row's platform) and pull
    the first physical meeting address it links to (a maps.google.com URL
    embedded next to the meeting location).
  - BoardDocs: fetch the client's public page ({state}/{slug}/Board.nsf/
    Public); the site header carries the street address (#SiteTitle1) and
    the district's display name (#SiteTitle2) - this is also how blank
    org_name columns get filled, since BoardDocs has no public directory
    to take names from.
  - Deferred platforms (agendaquick, diligent-community, apptegy): skipped.

The state is parsed straight out of the address string; the county comes
from the US Census Bureau's free public geocoder (no API key required).
Rows whose platform page yields no address are left blank rather than
guessed, so gaps are visible for manual follow-up.

This is a slow, network-bound script (two HTTP requests per org) and writes
progress back to the CSV periodically, so it can be safely interrupted and
resumed - rows that already have a non-empty `state` (and, for BoardDocs,
an org_name) are skipped on the next run unless --force is passed.

Both directory formats load: districts/district_directory.csv (platform +
platform_org_id columns) and the legacy BoardBook-only org_directory.csv
(bare org_id column).

Usage:
    python3 scripts/enrich_org_directory.py --csv districts/district_directory.csv
    python3 scripts/enrich_org_directory.py --csv districts/district_directory.csv --platform boarddocs --limit 20
    python3 scripts/enrich_org_directory.py --csv districts/district_directory.csv --force
"""
import argparse
import csv
import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

from platforms import DEFERRED_PLATFORMS, FAMILY_BASE_URLS, get_adapter

BOARDBOOK_BASE = FAMILY_BASE_URLS["boardbook"]
CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
USER_AGENT = "Mozilla/5.0 (compatible; MeetingMinuteResearchBot/1.0; contact: n.basson@tencategrass.com)"

ADDRESS_LINK_RE = re.compile(r"maps\.google\.com/\?q=([^'\"]+)")

# Matches ", TX 79311" or ", Texas 78613" style endings - the two forms
# observed on BoardBook org pages - plus "Pickerington OH 43147" (no comma),
# the form BoardDocs site headers use.
STATE_ABBR_RE = re.compile(r"[,\s]\s*([A-Z]{2})\s+\d{5}(-\d{4})?\s*$")
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


def fetch_first_address(session: requests.Session, org_id: str,
                        base_url: str = BOARDBOOK_BASE) -> str | None:
    resp = session.get(f"{base_url}/Public/Organization/{org_id}", timeout=30)
    resp.raise_for_status()
    m = ADDRESS_LINK_RE.search(resp.text)
    if not m:
        return None
    return urllib.parse.unquote_plus(m.group(1))


# What makes a BoardDocs header line an address rather than a name: a ZIP, a
# phone number, or a leading street number. District names occasionally end in
# a number ("Olathe Public School District 233") but match none of these.
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-/]\s?\d{3}[\s.\-]\d{4}")
_STREET_START_RE = re.compile(r"^\d+\s+\S")


def _looks_like_address(text: str) -> bool:
    return bool(_ZIP_RE.search(text) or _PHONE_RE.search(text)
                or _STREET_START_RE.match(text))


def parse_boarddocs_header(html: str) -> tuple:
    """(org_name, address) from a BoardDocs client's public page header.

    The header has two lines: usually the street address in #SiteTitle1
    ("90 N. East Street | Pickerington OH 43147 | (614) 833-2110") and the
    district's display name in #SiteTitle2 ("Pickerington Local School
    District") - but a large minority of sites SWAP the two, so each line is
    classified by content instead of trusting the slot. Either may be missing
    on sparsely configured sites.
    """
    soup = BeautifulSoup(html, "lxml")
    lines = []
    for selector in ("#SiteTitle1", "#SiteTitle2"):
        el = soup.select_one(selector)
        lines.append(el.get_text(" ", strip=True) if el else "")

    name = next((t for t in reversed(lines) if t and not _looks_like_address(t)), "")
    raw_address = next((t for t in lines if t and _looks_like_address(t)), "")
    address = ""
    if raw_address:
        # street | City ST ZIP | phone [| fax] -> "street, City ST ZIP"
        # (separator is "|" or a bullet; phone/fax parts carry no real words)
        parts = [p.strip() for p in re.split(r"[|•]", raw_address)]
        keep = [p for p in parts if len(re.sub(r"[^A-Za-z]", "", p)) >= 3][:2]
        address = ", ".join(keep) if keep else raw_address
    return name, address


def fetch_boarddocs_header(session: requests.Session, org_ref: str) -> tuple:
    adapter = get_adapter("boarddocs")
    resp = session.get(f"{adapter.org_page_url(org_ref)}", timeout=60,
                       headers={"User-Agent": adapter.user_agent})
    resp.raise_for_status()
    return parse_boarddocs_header(resp.text)


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


def row_platform(row: dict) -> str:
    """Platform of a directory row; legacy org_directory.csv rows (bare
    org_id column) are all BoardBook."""
    return (row.get("platform") or "boardbook").strip().lower()


def row_org_ref(row: dict) -> str:
    return (row.get("platform_org_id") or row.get("org_id") or "").strip()


def needs_enrichment(row: dict, force: bool) -> bool:
    platform = row_platform(row)
    if platform in DEFERRED_PLATFORMS:
        return False
    if force:
        return True
    if not row.get("state") or not row.get("county"):
        return True
    # BoardDocs rows are seeded without names (no public directory); a
    # nameless org cannot form a valid lead, so a blank name counts as a gap.
    return platform == "boarddocs" and not row.get("org_name")


def main():
    parser = argparse.ArgumentParser(description="Fill org_name/state/county gaps in the district directory CSV.")
    parser.add_argument("--csv", default="districts/district_directory.csv", help="Directory CSV to enrich in place")
    parser.add_argument("--platform", help="Only enrich rows of this platform (e.g. boarddocs)")
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
        platform = row_platform(row)
        if args.platform and platform != args.platform.strip().lower():
            continue
        if not needs_enrichment(row, args.force):
            continue
        if args.limit and processed >= args.limit:
            break

        org_ref = row_org_ref(row)
        org_name = row.get("org_name", "")
        label = f"[{platform}:{org_ref}] {org_name}".rstrip()
        try:
            if platform == "boarddocs":
                name, address = fetch_boarddocs_header(session, org_ref)
                if name and (args.force or not row.get("org_name")):
                    row["org_name"] = name
            elif platform in FAMILY_BASE_URLS:
                address = fetch_first_address(session, org_ref, FAMILY_BASE_URLS[platform])
            else:  # unknown platform string in the CSV - leave untouched
                print(f"{label}: unknown platform, skipped", file=sys.stderr)
                continue

            if not address:
                print(f"{label}: no address found on org page", file=sys.stderr)
            else:
                state = parse_state(address) or ""
                county = ""
                try:
                    county = fetch_county(session, address) or ""
                except requests.RequestException as e:
                    print(f"{label}: geocoder request failed ({e})", file=sys.stderr)
                if state and (args.force or not row.get("state")):
                    row["state"] = state
                if county and (args.force or not row.get("county")):
                    row["county"] = county
                print(f"{label}: state={row['state']!r} county={row['county']!r} "
                      f"name={row.get('org_name','')!r} (from '{address}')", file=sys.stderr)
        except requests.RequestException as e:
            print(f"{label}: FAILED to fetch org page ({e})", file=sys.stderr)

        processed += 1
        if processed % 25 == 0:
            save()
        time.sleep(args.sleep)

    save()
    print(f"\nProcessed {processed} org(s). Wrote {args.csv}.", file=sys.stderr)


if __name__ == "__main__":
    main()
