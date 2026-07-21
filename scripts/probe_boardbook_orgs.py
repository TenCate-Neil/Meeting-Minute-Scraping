#!/usr/bin/env python3
"""
Probe BoardBook-family org ids that are NOT in the platform's public /Public
directory.

Why this exists: a research pass (2026-07-20) found 36 Kansas districts live
on BoardBook whose org pages respond but which the public directory does not
list, so fetch_org_directory.py --platform boardbook can never discover them.
For a handful the numeric org id is known (see
districts/seeds/boardbook_ks_hidden.csv); for the rest the only way to find
the id is to probe the id space and match the page title against the known
district names.

The org page <title> is "{Org Name} Public View - {Product Name}", so the
name can be read straight off a HEAD-less GET without parsing the meeting
table.

Usage:
    # verify specific ids and print seed-format rows:
    python3 scripts/probe_boardbook_orgs.py --ids 2345,3223,2712,3307

    # sweep an id range (slow - one polite request per id), keeping only
    # pages whose title contains one of the given words:
    python3 scripts/probe_boardbook_orgs.py --range 1 4000 --title-contains "USD,Kansas" \
        --out districts/seeds/boardbook_probe_results.csv

Output is CSV in district_directory seed format (merge it with
fetch_org_directory.py --seed). Non-existent ids redirect back to /Public or
render no title - they are skipped silently.
"""
import argparse
import csv
import re
import sys
import time

from platforms import FAMILY_BASE_URLS, get_adapter

TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.S | re.I)
TITLE_SUFFIX_RE = re.compile(r"\s*Public View\s*-.*$", re.S)


def probe_org(adapter, org_id: str):
    """Return the org display name when the org page exists, else None."""
    url = f"{adapter.base_url}/Public/Organization/{org_id}"
    try:
        resp = adapter._request("GET", url, timeout=30, allow_redirects=True)
    except OSError:
        return None
    if resp.status_code != 200:
        return None
    m = TITLE_RE.search(resp.text)
    if not m:
        return None
    title = TITLE_SUFFIX_RE.sub("", m.group(1)).strip()
    # A nonexistent org id lands back on the bare directory page, whose title
    # has no org-name prefix.
    if not title or title.lower() in ("public view", "boardbook premier",
                                      "sparq meetings", "boeconnect"):
        return None
    return title


def main():
    parser = argparse.ArgumentParser(description="Probe BoardBook-family org ids not in the public directory.")
    parser.add_argument("--platform", default="boardbook",
                        help="One of: " + ", ".join(sorted(FAMILY_BASE_URLS)))
    parser.add_argument("--ids", help="Comma-separated org ids to probe")
    parser.add_argument("--range", nargs=2, type=int, metavar=("START", "END"),
                        help="Probe every id in [START, END]")
    parser.add_argument("--title-contains",
                        help="Comma-separated words; keep only orgs whose name contains one (case-insensitive)")
    parser.add_argument("--state", default="", help="State to stamp on output rows (e.g. KS)")
    parser.add_argument("--sleep", type=float, default=0.3, help="Politeness delay between probes")
    parser.add_argument("--out", help="Write seed CSV here (default: stdout)")
    args = parser.parse_args()

    if not args.ids and not args.range:
        parser.error("one of --ids / --range is required")

    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    else:
        ids = [str(i) for i in range(args.range[0], args.range[1] + 1)]
    needles = [w.strip().lower() for w in (args.title_contains or "").split(",") if w.strip()]

    adapter = get_adapter(args.platform)
    rows = []
    for n, org_id in enumerate(ids, 1):
        name = probe_org(adapter, org_id)
        if name and (not needles or any(w in name.lower() for w in needles)):
            print(f"  [{n}/{len(ids)}] {org_id}: {name}", file=sys.stderr)
            rows.append({
                "platform": adapter.platform,
                "platform_org_id": org_id,
                "org_name": name,
                "state": args.state,
                "likely_school_district": "True",
                "include_in_rollout": "True",
                "notes": "live but not in the public directory (found by probe)",
            })
        if n < len(ids):
            time.sleep(args.sleep)

    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    writer = csv.DictWriter(out, fieldnames=["platform", "platform_org_id", "org_name",
                                             "state", "likely_school_district",
                                             "include_in_rollout", "notes"])
    writer.writeheader()
    writer.writerows(rows)
    if args.out:
        out.close()
        print(f"{len(rows)} org(s) written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
