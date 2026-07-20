#!/usr/bin/env python3
"""
Build and maintain districts/district_directory.csv - the multi-source
district directory with ONE ROW PER DISTRICT-PLATFORM PAIR.

A district can appear under several platforms (dual-hosting is real: e.g.
Kingsport TN posts on both BOEconnect and BoardDocs); rows are keyed by
(platform, platform_org_id). De-duplication of *leads* happens naturally at
the lead layer: the same organization + project yields the same external_id
regardless of which platform surfaced it.

Columns:
    organization_id   shared org id (agent-repo naming, e.g. leander-isd-tx);
                      join key across pipelines; empty until reconciled
    org_name          display name (from the platform directory where one exists)
    state, county     as before; county keeps the " County" suffix, the export
                      strips it (see export_leads.normalize_county)
    counties, place   optional geography extras per docs/SCHEMA_ALIGNMENT_PLAN.md
    platform          boardbook | sparq | boeconnect | boarddocs | agendaquick |
                      diligent-community | apptegy
    platform_org_id   BoardBook-family org id or slug; BoardDocs "state/slug";
                      AgendaQuick numeric id; etc.
    likely_school_district / include_in_rollout   heuristic default; a human
                      curates include_in_rollout (see docs/ROLLOUT.md)
    notes             why a row is excluded/blank, dual-hosting hints, etc.

Three merge sources, all idempotent (safe to re-run):

    # live platform directory (BoardBook family only - the three /Public pages)
    python3 scripts/fetch_org_directory.py --platform sparq
    python3 scripts/fetch_org_directory.py --platform boeconnect
    python3 scripts/fetch_org_directory.py --platform boardbook

    # one-time migration of the legacy BoardBook-only org_directory.csv
    python3 scripts/fetch_org_directory.py --migrate-legacy districts/org_directory.csv

    # curated seed rows (research-validated orgs; see districts/seeds/)
    python3 scripts/fetch_org_directory.py --seed districts/seeds/boarddocs.csv

Merge rules, per column:
  - new (platform, platform_org_id) rows are appended;
  - a live fetch is authoritative for org_name (it refreshes renamed orgs) and
    only sets the heuristics for rows it creates - it NEVER touches curated
    fields (include_in_rollout, state, county, organization_id, notes);
  - legacy migration and seeds fill empty fields; seeds additionally OVERRIDE
    include_in_rollout and notes when they provide a value (a seed is curated
    input, so an explicit True/False wins over a heuristic default);
  - boarddocs rows with an empty state get it derived from the org ref's
    "{state}/" prefix - that prefix IS the state, not a guess.

BoardDocs has no public client directory; its rows come from seeds
(research-validated slug lists). Deferred platforms (agendaquick,
diligent-community, apptegy) are seeded the same way so the rollout can see
them, and run_all_districts.py skips them until an adapter exists.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

from platforms import FAMILY_BASE_URLS, KNOWN_PLATFORMS, get_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRECTORY = REPO_ROOT / "districts" / "district_directory.csv"

FIELDNAMES = [
    "organization_id", "org_name", "state", "county", "counties", "place",
    "platform", "platform_org_id", "likely_school_district",
    "include_in_rollout", "notes",
]

# Fields a curated seed may override even when already set; everything else
# only fills blanks. include_in_rollout/notes are exactly the fields a human
# (or a validated research pass) curates.
SEED_OVERRIDE_FIELDS = ("include_in_rollout", "notes")

# Heuristic only - always spot-check the directory by hand before a production
# rollout (see docs/ROLLOUT.md). False positives/negatives are expected.
SCHOOL_PATTERN = re.compile(
    r"\bI\.?S\.?D\.?\b|\bC\.?I\.?S\.?D\.?\b|\bU\.?I\.?S\.?D\.?\b|\bUSD\b|"
    r"school district|\bschools?\b|academy|charter|\bESD\b|\bRESA\b",
    re.IGNORECASE,
)


def empty_row(platform: str, platform_org_id: str) -> dict:
    row = {f: "" for f in FIELDNAMES}
    row["platform"] = platform
    row["platform_org_id"] = platform_org_id
    return row


def row_key(row: dict):
    return (row.get("platform", "").strip(), str(row.get("platform_org_id", "")).strip())


def load_directory(path: Path) -> "dict[tuple, dict]":
    """Load the directory as an ordered {(platform, platform_org_id): row} map."""
    directory = {}
    if not path.exists():
        return directory
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            normalized = {f: (row.get(f) or "").strip() for f in FIELDNAMES}
            key = row_key(normalized)
            if key[0] and key[1]:
                directory[key] = normalized
    return directory


def save_directory(path: Path, directory: "dict[tuple, dict]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(directory.values(), key=lambda r: (r["platform"], r["org_name"].lower(), r["platform_org_id"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def derive_boarddocs_state(row: dict) -> None:
    """A BoardDocs org ref is '{state}/{slug}' - the prefix IS the state."""
    if row.get("platform") == "boarddocs" and not row.get("state"):
        prefix = row.get("platform_org_id", "").split("/", 1)[0]
        if len(prefix) == 2 and prefix.isalpha():
            row["state"] = prefix.upper()


def merge_row(directory: dict, incoming: dict, source: str) -> str:
    """Merge one incoming row. source: 'live' | 'legacy' | 'seed'.
    Returns 'new' | 'updated' | 'unchanged'."""
    incoming = {f: str(incoming.get(f, "") or "").strip() for f in FIELDNAMES}
    derive_boarddocs_state(incoming)
    key = row_key(incoming)
    existing = directory.get(key)
    if existing is None:
        directory[key] = incoming
        return "new"

    changed = False
    for field in FIELDNAMES:
        value = incoming[field]
        if not value:
            continue
        overrides = (
            (source == "live" and field == "org_name")
            or (source == "seed" and field in SEED_OVERRIDE_FIELDS)
        )
        if (overrides and existing[field] != value) or not existing[field]:
            existing[field] = value
            changed = True
    return "updated" if changed else "unchanged"


# --- the three merge sources -------------------------------------------------

def rows_from_live(platform: str):
    """Every org in a BoardBook-family /Public directory, with the school
    heuristic applied for NEW rows' defaults."""
    if platform not in FAMILY_BASE_URLS:
        raise SystemExit(
            f"--platform must be one of {sorted(FAMILY_BASE_URLS)} "
            f"(only the BoardBook family has a public client directory; "
            f"boarddocs and the deferred platforms are seeded from "
            f"districts/seeds/ instead)"
        )
    adapter = get_adapter(platform)
    print(f"Fetching organization directory from {adapter.base_url}/Public ...", file=sys.stderr)
    orgs = adapter.list_organizations()
    print(f"Found {len(orgs)} organizations.", file=sys.stderr)
    for org_ref, name in orgs:
        row = empty_row(platform, org_ref)
        row["org_name"] = name
        is_school = bool(SCHOOL_PATTERN.search(name))
        row["likely_school_district"] = str(is_school)
        row["include_in_rollout"] = str(is_school)
        yield row


def rows_from_legacy(path: Path):
    """The pre-multi-platform districts/org_directory.csv (BoardBook only,
    keyed by bare org_id). Kept in the repo for reference; this migration is
    how its curation (organization_id, state, county, include_in_rollout,
    notes) enters the new directory."""
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            org_id = (row.get("org_id") or "").strip()
            if not org_id:
                continue
            new = empty_row("boardbook", org_id)
            for field in ("organization_id", "org_name", "state", "county",
                          "likely_school_district", "include_in_rollout", "notes"):
                new[field] = (row.get(field) or "").strip()
            yield new


def rows_from_seed(path: Path):
    """A curated seed CSV using district_directory.csv columns (subset ok;
    platform + platform_org_id required per row)."""
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            platform = (row.get("platform") or "").strip()
            org_ref = (row.get("platform_org_id") or "").strip()
            if not platform or not org_ref:
                continue
            if platform not in KNOWN_PLATFORMS:
                print(f"  WARNING: {path.name}: unknown platform {platform!r} for "
                      f"{org_ref!r}; row still merged", file=sys.stderr)
            new = empty_row(platform, org_ref)
            for field in FIELDNAMES:
                if field in row and (row.get(field) or "").strip():
                    new[field] = row[field].strip()
            yield new


def main():
    parser = argparse.ArgumentParser(
        description="Build/maintain the multi-source district directory (merge-upsert)."
    )
    parser.add_argument("--out", default=str(DEFAULT_DIRECTORY),
                        help="Directory CSV to merge into (default: districts/district_directory.csv)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--platform",
                        help="Fetch a live platform directory (BoardBook family: "
                             + ", ".join(sorted(FAMILY_BASE_URLS)) + ")")
    source.add_argument("--migrate-legacy", metavar="CSV",
                        help="Merge the legacy BoardBook org_directory.csv as platform=boardbook rows")
    source.add_argument("--seed", metavar="CSV",
                        help="Merge a curated seed CSV (see districts/seeds/)")
    args = parser.parse_args()

    if args.platform:
        rows, source_kind = rows_from_live(args.platform.strip().lower()), "live"
    elif args.migrate_legacy:
        rows, source_kind = rows_from_legacy(Path(args.migrate_legacy)), "legacy"
    else:
        rows, source_kind = rows_from_seed(Path(args.seed)), "seed"

    out_path = Path(args.out)
    directory = load_directory(out_path)
    before = len(directory)
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    for row in rows:
        counts[merge_row(directory, row, source_kind)] += 1

    save_directory(out_path, directory)
    print(
        f"{out_path}: {before} -> {len(directory)} rows "
        f"({counts['new']} new, {counts['updated']} updated, {counts['unchanged']} unchanged).",
        file=sys.stderr,
    )
    if source_kind == "live":
        print("Review the include_in_rollout column before running run_all_districts.py.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
