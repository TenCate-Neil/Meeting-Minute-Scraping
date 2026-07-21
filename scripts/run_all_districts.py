#!/usr/bin/env python3
"""
Orchestrate scrape_meetings.py across every district in
districts/district_directory.csv where include_in_rollout is true, whatever
platform each district posts on.

Each directory row names a platform (boardbook / sparq / boeconnect /
boarddocs / ...); this script dispatches the platform-neutral scraper with
the row's platform + platform org id. Rows for platforms whose adapter is
deferred (agendaquick, diligent-community, apptegy) are reported and skipped,
not failed, so the directory can be seeded ahead of adapter work.

Produces one aggregated summary covering all districts, plus per-district
JSON/CSV files (output/districts/{platform}_{org}.json) so a hit can be
traced back to the specific organization AND platform it came from. Legacy
org_<id>.json output files from the BoardBook-only era are renamed to the
new scheme once, automatically, so their cumulative history carries over.

Usage:
    python3 run_all_districts.py --districts-csv districts/district_directory.csv --limit-per-district 10
    python3 run_all_districts.py --start-date 2024-01-01
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

# export_leads lives alongside this script; import it for the optional final
# export step (--export-leads). Kept as a plain import, no agents involved.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_leads  # noqa: E402
import scrape_state  # noqa: E402
from platforms import implemented_platforms  # noqa: E402

IMPLEMENTED = set(implemented_platforms())


def safe_org_filename(platform: str, org_ref: str) -> str:
    """Filesystem-safe stem for per-district output files. BoardDocs org refs
    contain a slash ("ny/albany" -> "boarddocs_ny-albany"). The stem is for
    humans and uniqueness only - the authoritative platform/org of a file's
    records is stored IN the records (see scrape_meetings.result_to_record),
    so this mapping never needs to be reversed."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(org_ref))
    return f"{platform}_{safe}"


def load_districts(csv_path: Path) -> list:
    """Rows to roll out: [{platform, org_id, org_name}, ...].

    Reads the multi-source directory (platform + platform_org_id columns).
    A legacy BoardBook-only CSV (bare org_id column, no platform column) still
    loads, as platform=boardbook, so pre-migration checkouts keep working.
    """
    districts = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        legacy = "platform" not in (reader.fieldnames or [])
        for row in reader:
            include = (row.get("include_in_rollout") or "").strip().lower() in ("true", "1", "yes")
            if not include:
                continue
            if legacy:
                platform, org_id = "boardbook", (row.get("org_id") or "").strip()
            else:
                platform = (row.get("platform") or "").strip().lower()
                org_id = (row.get("platform_org_id") or "").strip()
            if not platform or not org_id:
                continue
            districts.append({
                "platform": platform,
                "org_id": org_id,
                "org_name": (row.get("org_name") or "").strip(),
            })
    return districts


def migrate_legacy_output(out_dir: Path, platform: str, org_id: str, stem: str) -> None:
    """One-time rename of BoardBook-era output files (org_795.json) to the
    platform-namespaced scheme (boardbook_795.json), so the cumulative record
    history (and with it export_leads' view of past turf hits) survives."""
    if platform != "boardbook":
        return
    for ext in ("json", "csv"):
        legacy = out_dir / f"org_{org_id}.{ext}"
        new = out_dir / f"{stem}.{ext}"
        if legacy.exists() and not new.exists():
            legacy.rename(new)
            print(f"  (migrated {legacy.name} -> {new.name})", file=sys.stderr)


def run_one_district(district: dict, args) -> dict:
    platform, org_id, org_name = district["platform"], district["org_id"], district["org_name"]
    base = {"platform": platform, "org_id": org_id, "org_name": org_name}

    if platform not in IMPLEMENTED:
        print(f"--- Skipping {platform} org {org_id} ({org_name}): adapter deferred ---",
              file=sys.stderr)
        return {**base, "status": "skipped_platform_deferred"}

    stem = safe_org_filename(platform, org_id)
    out_dir = Path(args.out_dir)
    migrate_legacy_output(out_dir, platform, org_id, stem)
    out_json = out_dir / f"{stem}.json"
    out_csv = out_dir / f"{stem}.csv"

    cmd = [
        sys.executable,
        str(Path(__file__).parent / "scrape_meetings.py"),
        "--platform", platform,
        "--org", org_id,
        "--out-json", str(out_json),
        "--out-csv", str(out_csv),
        "--sleep", str(args.sleep),
    ]
    if args.limit_per_district:
        cmd += ["--limit", str(args.limit_per_district)]
    if args.start_date:
        cmd += ["--start-date", args.start_date]
    if args.end_date:
        cmd += ["--end-date", args.end_date]
    if args.keep_pdfs:
        cmd += ["--keep-pdfs", "--pdf-dir", str(out_dir / "pdfs" / stem)]
    if args.skip_minutes:
        cmd += ["--skip-minutes"]
    if args.no_state:
        cmd += ["--no-state"]
    else:
        cmd += ["--state-file", args.state_file,
                "--minutes-recheck-days", str(args.minutes_recheck_days)]
        if args.force_rescrape:
            cmd += ["--force-rescrape"]

    print(f"--- Running {platform} org {org_id} ({org_name}) ---", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FAILED: {result.stderr[-500:]}", file=sys.stderr)
        return {**base, "status": "failed", "error": result.stderr[-500:]}

    try:
        with out_json.open(encoding="utf-8") as f:
            records = json.load(f)
    except Exception as e:
        return {**base, "status": "failed", "error": f"could not read output: {e}"}

    hits = [r for r in records if r.get("turf_mentioned")]
    return {
        **base,
        "status": "ok",
        "documents_analyzed": len(records),
        "turf_hits": len(hits),
        "hit_meeting_ids": [r["meeting_id"] for r in hits],
    }


def main():
    parser = argparse.ArgumentParser(description="Run the meeting scraper across every curated district, all platforms.")
    parser.add_argument("--districts-csv", default="districts/district_directory.csv",
                        help="Curated multi-source district directory CSV")
    parser.add_argument("--out-dir", default="output/districts", help="Directory for per-district JSON/CSV output")
    parser.add_argument("--summary-out", default="output/rollout_summary.json", help="Path for the aggregated summary")
    parser.add_argument("--limit-per-district", type=int, help="Cap meetings processed per district (e.g. for a smoke test)")
    parser.add_argument("--start-date", help="Only include meetings on/after this date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Only include meetings on/before this date (YYYY-MM-DD)")
    parser.add_argument("--platforms",
                        help="Comma-separated platform filter (e.g. 'sparq,boeconnect'); default: all")
    parser.add_argument("--keep-pdfs", action="store_true", help="Persist documents for districts with turf hits")
    parser.add_argument("--skip-minutes", action="store_true",
                        help="Forwarded to scrape_meetings.py: skip the minutes pass that "
                             "confirms outcomes for turf hits")
    parser.add_argument("--sleep", type=float, default=0.5, help="Politeness delay between requests, per district run")
    parser.add_argument("--state-file", default=str(scrape_state.DEFAULT_STATE_FILE),
                        help="Forwarded to scrape_meetings.py: tracked scrape-state JSON so "
                             "re-runs skip meeting documents already captured")
    parser.add_argument("--no-state", action="store_true",
                        help="Forwarded to scrape_meetings.py: disable the scrape state entirely")
    parser.add_argument("--force-rescrape", action="store_true",
                        help="Forwarded to scrape_meetings.py: ignore skip decisions, still record state")
    parser.add_argument("--minutes-recheck-days", type=int, default=scrape_state.DEFAULT_RECHECK_DAYS,
                        help="Forwarded to scrape_meetings.py: stop rechecking missing "
                             "documents/minutes for meetings older than this many days")
    parser.add_argument("--district-limit", type=int, help="Only process the first N districts from the CSV (smoke testing)")
    parser.add_argument("--export-leads", action="store_true",
                        help="After the rollout, export turf-hit documents to the shared "
                             "core-lead shape (see scripts/export_leads.py). Off by default.")
    parser.add_argument("--ledger", default=str(export_leads.DEFAULT_LEDGER),
                        help="Ledger path for --export-leads")
    parser.add_argument("--exports-dir", default=str(export_leads.DEFAULT_EXPORTS),
                        help="Per-run export directory for --export-leads")
    parser.add_argument("--org-registry",
                        help="Optional shared organization registry for --export-leads "
                             "(see scripts/export_leads.py --org-registry)")
    args = parser.parse_args()

    districts = load_districts(Path(args.districts_csv))
    if args.platforms:
        wanted = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
        districts = [d for d in districts if d["platform"] in wanted]
    if args.district_limit:
        districts = districts[: args.district_limit]

    print(f"Rolling out across {len(districts)} district(s).", file=sys.stderr)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = []
    for i, district in enumerate(districts, 1):
        print(f"[{i}/{len(districts)}]", file=sys.stderr)
        summary.append(run_one_district(district, args))

    summary_path = Path(args.summary_out)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    total_hits = sum(s.get("turf_hits", 0) for s in summary)
    failed = [s for s in summary if s["status"] == "failed"]
    deferred = [s for s in summary if s["status"] == "skipped_platform_deferred"]
    print(f"\nDone. {len(districts)} districts processed, {total_hits} turf-hit document(s) total, "
          f"{len(failed)} failed, {len(deferred)} skipped (adapter deferred).", file=sys.stderr)
    print(f"Summary written to {summary_path}", file=sys.stderr)

    if args.export_leads:
        print("\n--- Exporting leads to the shared core-lead shape ---", file=sys.stderr)
        counts = export_leads.run_export(
            input_path=out_dir,
            districts_csv=Path(args.districts_csv),
            ledger_path=Path(args.ledger),
            exports_dir=Path(args.exports_dir),
            org_registry_path=Path(args.org_registry) if args.org_registry else None,
        )
        export_leads._print_summary(counts)


if __name__ == "__main__":
    main()
