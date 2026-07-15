#!/usr/bin/env python3
"""
Orchestrate scrape_boardbook.py across every district in
districts/org_directory.csv where include_in_rollout is true.

Produces one aggregated JSON/CSV covering all districts, plus a per-district
breakdown so a hit can be traced back to the specific organization it came
from. This is the entry point for the "rollout" described in
docs/ROLLOUT.md - point it at the curated directory CSV and let it run.

Usage:
    python3 run_all_districts.py --districts-csv districts/org_directory.csv --limit-per-district 10
    python3 run_all_districts.py --districts-csv districts/org_directory.csv --start-date 2024-01-01
"""
import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

# export_leads lives alongside this script; import it for the optional final
# export step (--export-leads). Kept as a plain import, no agents involved.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_leads  # noqa: E402


def load_districts(csv_path: Path) -> list:
    districts = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            include = row.get("include_in_rollout", "").strip().lower() in ("true", "1", "yes")
            if include:
                districts.append((row["org_id"], row["org_name"]))
    return districts


def run_one_district(org_id: str, org_name: str, args) -> dict:
    out_json = Path(args.out_dir) / f"org_{org_id}.json"
    out_csv = Path(args.out_dir) / f"org_{org_id}.csv"

    cmd = [
        sys.executable,
        str(Path(__file__).parent / "scrape_boardbook.py"),
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
        cmd += ["--keep-pdfs", "--pdf-dir", str(Path(args.out_dir) / "pdfs" / org_id)]
    if args.skip_minutes:
        cmd += ["--skip-minutes"]

    print(f"--- Running org {org_id} ({org_name}) ---", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FAILED: {result.stderr[-500:]}", file=sys.stderr)
        return {"org_id": org_id, "org_name": org_name, "status": "failed", "error": result.stderr[-500:]}

    try:
        with out_json.open(encoding="utf-8") as f:
            records = json.load(f)
    except Exception as e:
        return {"org_id": org_id, "org_name": org_name, "status": "failed", "error": f"could not read output: {e}"}

    hits = [r for r in records if r.get("turf_mentioned")]
    return {
        "org_id": org_id,
        "org_name": org_name,
        "status": "ok",
        "documents_analyzed": len(records),
        "turf_hits": len(hits),
        "hit_meeting_ids": [r["meeting_id"] for r in hits],
    }


def main():
    parser = argparse.ArgumentParser(description="Run the BoardBook scraper across every curated district.")
    parser.add_argument("--districts-csv", default="districts/org_directory.csv", help="Curated org directory CSV")
    parser.add_argument("--out-dir", default="output/districts", help="Directory for per-district JSON/CSV output")
    parser.add_argument("--summary-out", default="output/rollout_summary.json", help="Path for the aggregated summary")
    parser.add_argument("--limit-per-district", type=int, help="Cap meetings processed per district (e.g. for a smoke test)")
    parser.add_argument("--start-date", help="Only include meetings on/after this date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Only include meetings on/before this date (YYYY-MM-DD)")
    parser.add_argument("--keep-pdfs", action="store_true", help="Persist PDFs for districts with turf hits")
    parser.add_argument("--skip-minutes", action="store_true",
                        help="Forwarded to scrape_boardbook.py: skip the minutes pass that "
                             "confirms outcomes for turf hits")
    parser.add_argument("--sleep", type=float, default=0.5, help="Politeness delay between requests, per district run")
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
    if args.district_limit:
        districts = districts[: args.district_limit]

    print(f"Rolling out across {len(districts)} district(s).", file=sys.stderr)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = []
    for i, (org_id, org_name) in enumerate(districts, 1):
        print(f"[{i}/{len(districts)}]", file=sys.stderr)
        summary.append(run_one_district(org_id, org_name, args))

    summary_path = Path(args.summary_out)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    total_hits = sum(s.get("turf_hits", 0) for s in summary)
    failed = [s for s in summary if s["status"] == "failed"]
    print(f"\nDone. {len(districts)} districts processed, {total_hits} turf-hit document(s) total, {len(failed)} failed.", file=sys.stderr)
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
