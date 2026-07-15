#!/usr/bin/env python3
"""
Export step: convert this repo's per-document turf analysis output into the
shared "core lead" shape (contract v2.0) used by the Supabase/Retool platform.

This does NOT change the scraping/analysis pipeline or its output files. It is
an additional, final step that reads the per-document JSON that
scrape_boardbook.py / run_all_districts.py already produce and emits leads.

What it does, per run:
  1. Read per-document analysis output (a single JSON file, or a directory of
     org_<id>.json files as run_all_districts.py writes to output/districts/).
  2. Keep only documents with turf_mentioned == true.
  3. Join organization metadata (org_name, state, county) from the curated
     districts/org_directory.csv on the BoardBook org_id.
  4. Map each surviving document to one lead (see MAPPING below) and compute a
     deterministic external_id.
  5. Ledger-first dedup: leads/ledger.json holds every lead ever exported,
     keyed by external_id. New ids are appended; ids already present are left
     untouched (their discovered_at is preserved).
  6. Write the new leads to a timestamped run file
     exports/<UTC YYYYMMDDTHHMMSSZ>/leads.json.
  7. Validate EVERY lead against contracts/lead.schema.json before writing.
     Records that fail validation are refused (not written) and reported.

Usage:
    # Directory produced by run_all_districts.py (org_<id>.json per district):
    python3 scripts/export_leads.py --input output/districts

    # A single results file whose name does NOT encode the org id:
    python3 scripts/export_leads.py --input output/leander_2026.json --org 795

See MAPPING below and the brief for the field-by-field rules.
"""
import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - dependency hint
    print(
        "ERROR: this script needs the 'jsonschema' package. Install it with:\n"
        "    pip install jsonschema",
        file=sys.stderr,
    )
    raise

# --- constants -------------------------------------------------------------

BOARDBOOK_BASE = "https://meetings.boardbook.org"
SOURCE = "meeting-minutes"          # never anything else in this repo
LOCATION_ID = "us-xx-boardbook"     # this repo has no per-lead location registry
LEDGER_SCHEMA_VERSION = "2.0"
EXTERNAL_ID_SALT = "mm-turf"        # frozen: changing this duplicates every lead
SUMMARY_MAX = 400                   # core lead summary maxLength (see schema)
EVIDENCE_QUOTE_MATCHES = 5          # join the first N match contexts into one quote

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = REPO_ROOT / "contracts" / "lead.schema.json"
DEFAULT_DISTRICTS = REPO_ROOT / "districts" / "org_directory.csv"
DEFAULT_LEDGER = REPO_ROOT / "leads" / "ledger.json"
DEFAULT_EXPORTS = REPO_ROOT / "exports"

# org_<id>.json (as run_all_districts.py writes); capture the org id.
ORG_FILENAME_RE = re.compile(r"^org_([A-Za-z0-9]+)\.json$")


# --- small helpers ---------------------------------------------------------

def slugify(*parts: str) -> str:
    """Deterministic ascii slug: lowercase, hyphen-joined, [a-z0-9-] only.

    Empty parts are dropped, so slugify("Leander ISD", "") -> "leander-isd"
    (no trailing hyphen) and slugify("Leander ISD", "TX") -> "leander-isd-tx".
    """
    pieces = []
    for part in parts:
        if not part:
            continue
        ascii_ = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode()
        cleaned = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
        if cleaned:
            pieces.append(cleaned)
    return "-".join(pieces)


def compute_external_id(org_id: str, meeting_id: str) -> str:
    """SHA-1 hex of 'lower(trim(org_id))|lower(trim(meeting_id))|mm-turf'.

    Frozen recipe. The same document analyzed again must yield the same id so
    Supabase upserts stay idempotent. One lead per document -> no topic suffix.
    """
    basis = f"{org_id.strip().lower()}|{meeting_id.strip().lower()}|{EXTERNAL_ID_SALT}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def normalize_county(county: str) -> str:
    """Match the web-search repo's county convention: bare county name, no
    ' County' suffix (their registry stores 'Williamson', not 'Williamson
    County'). Keeps the two pipelines' county values consistent in Supabase.
    """
    c = (county or "").strip()
    if c.endswith(" County"):
        c = c[: -len(" County")].strip()
    return c


def clean_meeting_date(date_str: str) -> str:
    """Turn a BoardBook display date into a plain human date label.

    'January 22, 2026 at 6:15 PM'          -> 'January 22, 2026'
    'CancelledJanuary 27, 2026 at 6:15 PM' -> 'January 27, 2026'
    """
    s = (date_str or "").strip()
    if s.startswith("Cancelled"):
        s = s[len("Cancelled"):].strip()
    s = s.split(" at ")[0].strip()
    return s


def trim_one_line(text: str, limit: int) -> str:
    """Collapse whitespace to one line and hard-cap at `limit` characters."""
    one_line = re.sub(r"\s+", " ", (text or "").strip())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1].rstrip() + "…"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    """RFC 3339 date-time with a trailing Z, e.g. 2026-07-14T09:12:00Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_z(dt: datetime) -> str:
    """Compact UTC run id used for the exports/ folder, e.g. 20260714T091200Z."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --- loading ---------------------------------------------------------------

def load_org_directory(csv_path: Path) -> dict:
    """org_id -> {'org_name', 'state', 'county'} from the curated directory."""
    directory = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            org_id = (row.get("org_id") or "").strip()
            if not org_id:
                continue
            directory[org_id] = {
                "org_name": (row.get("org_name") or "").strip(),
                "state": (row.get("state") or "").strip().upper(),
                "county": (row.get("county") or "").strip(),
            }
    return directory


def load_org_registry(path: Optional[Path]) -> Optional[dict]:
    """Load the shared organization registry (the web-search repo's
    organizations/registry.json, or a Supabase export in the same shape) into
    an index keyed by (name-slug, state) -> {'organization_id', 'county'}.

    Returns None if no path is given, which switches the export to
    standalone slug generation. When present, it is the source of truth for
    organization_id so both pipelines emit the same key for the same org.
    """
    if path is None:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    orgs = data.get("organizations", data) if isinstance(data, dict) else data
    index = {}
    for entry in orgs:
        name = (entry.get("name") or "").strip()
        state = (entry.get("state") or "").strip().upper()
        org_id = (entry.get("organization_id") or "").strip()
        if not (name and org_id):
            continue
        county = entry.get("primary_county") or entry.get("county") or ""
        index[(slugify(name), state)] = {
            "organization_id": org_id,
            "county": normalize_county(county),
        }
    return index


def discover_input_files(input_path: Path) -> list:
    """Return the list of per-document JSON files to process."""
    if input_path.is_dir():
        return sorted(input_path.glob("org_*.json"))
    return [input_path]


def org_id_for_file(path: Path, override: Optional[str]) -> Optional[str]:
    """Determine the BoardBook org id for a per-document file.

    An explicit --org override wins (for files whose name doesn't encode it).
    Otherwise parse it from the org_<id>.json filename convention.
    """
    if override:
        return override.strip()
    m = ORG_FILENAME_RE.match(path.name)
    return m.group(1) if m else None


# --- mapping ---------------------------------------------------------------

def build_lead(record: dict, org_id: str, org_meta: dict, discovered_at: str,
               run_stamp: str, registry_index: Optional[dict] = None) -> dict:
    """Map one per-document analysis record + org metadata to a core lead.

    Assumes record["turf_mentioned"] is true and record["matches"] is non-empty
    (the caller filters). One lead per document.

    If registry_index is provided (the shared org registry), organization_id
    and county are taken from it for matched orgs so both pipelines agree; orgs
    not in the registry keep a locally-generated slug and are flagged
    needs_review for reconciliation.
    """
    meeting_id = str(record.get("meeting_id", "")).strip()
    matches = record.get("matches") or []

    org_name = org_meta.get("org_name", "")
    state = org_meta.get("state", "")
    county = normalize_county(org_meta.get("county", ""))

    # organization_id: prefer the shared registry's key for this org (so both
    # pipelines emit the same id); otherwise derive it deterministically.
    organization_id = slugify(org_name, state)
    registry_entry = registry_index.get((slugify(org_name), state)) if registry_index else None
    if registry_entry:
        organization_id = registry_entry.get("organization_id") or organization_id
        if registry_entry.get("county"):
            county = registry_entry["county"]
    unreconciled = registry_index is not None and registry_entry is None

    # Aggregations over the document's matches.
    terms = sorted({(m.get("term") or "").strip().lower() for m in matches if m.get("term")})
    topics = sorted({m.get("topic_type") for m in matches if m.get("topic_type")})
    sentiments = sorted({m.get("sentiment") for m in matches if m.get("sentiment")})
    outcomes = sorted({m.get("outcome") for m in matches if m.get("outcome")})

    # evidence_quote: the first N match contexts, verbatim, joined by "; ".
    contexts = [(m.get("context") or "").strip() for m in matches if m.get("context")]
    evidence_quote = "; ".join(contexts[:EVIDENCE_QUOTE_MATCHES])

    # summary: prefer the document summary; synthesize only if it's empty.
    raw_summary = (record.get("summary") or "").strip()
    if raw_summary:
        summary = trim_one_line(raw_summary, SUMMARY_MAX)
    else:
        meeting_title = (record.get("title") or "").strip()
        synthesized = (
            f"Turf discussion at {org_name}"
            + (f" - {meeting_title}" if meeting_title else "")
            + (f"; terms: {', '.join(terms)}" if terms else "")
            + (f"; topics: {', '.join(topics)}" if topics else "")
        )
        summary = trim_one_line(synthesized or f"Turf mention at {org_name}", SUMMARY_MAX)

    meeting_date = clean_meeting_date(record.get("date", ""))

    # Deep link a BDM can open to see the mention (human-facing agenda page).
    source_url = f"{BOARDBOOK_BASE}/Public/Agenda/{org_id}?meeting={meeting_id}"

    needs_review = (state == "") or (county == "") or unreconciled

    # evidence.details: fuller context, demoted from the core.
    detail_bits = []
    if record.get("title"):
        detail_bits.append(f"Meeting: {record['title']}")
    if record.get("date"):
        detail_bits.append(f"Date: {record['date']}")
    detail_bits.append(f"{len(matches)} match(es)")
    if topics:
        detail_bits.append("Topics: " + "; ".join(topics))
    if sentiments:
        detail_bits.append("Sentiments: " + "; ".join(sentiments))
    if outcomes:
        detail_bits.append("Agenda outcome(s) (heuristic): " + "; ".join(outcomes))
    # Outcome per the minutes (hybrid pass in scrape_boardbook.py). Present only
    # for turf hits whose meeting had a posted minutes PDF. Heuristic hint, not
    # a verified vote result - read minutes_context to confirm.
    if record.get("minutes_available"):
        confirmed = record.get("minutes_outcome") or "turf item not located in minutes"
        detail_bits.append(f"Outcome per minutes (heuristic): {confirmed}")
    if record.get("pages"):
        detail_bits.append(f"Pages: {record['pages']}")
    # Keep the full summary here only if the core one was trimmed.
    if raw_summary and raw_summary != summary:
        detail_bits.append(f"Full summary: {raw_summary}")
    if unreconciled:
        detail_bits.append(
            "Org not found in shared registry; organization_id generated "
            "locally - reconcile before merge"
        )
    details = ". ".join(detail_bits) + "."

    project_name = trim_one_line(
        f"{org_name} - board minutes turf mention {meeting_date}".strip(), 255
    )

    evidence = {
        "project_name": project_name,
        "details": details,
        "matched_terms": terms,
        "source_urls": [
            f"{BOARDBOOK_BASE}/Public/Organization/{org_id}",
            f"{BOARDBOOK_BASE}/Public/DownloadAgenda/{org_id}?meeting={meeting_id}",
        ],
        "needs_review": needs_review,
        "run_timestamp": run_stamp,
    }

    lead = {
        "external_id": compute_external_id(org_id, meeting_id),
        "source": SOURCE,
        "organization": org_name,
        "state": state,
        "county": county,
        "summary": summary,
        "evidence_quote": evidence_quote,
        "source_url": source_url,
        "discovered_at": discovered_at,
        "location_id": LOCATION_ID,
        "evidence": evidence,
    }

    if organization_id:
        lead["organization_id"] = organization_id

    return lead


# --- ledger ----------------------------------------------------------------

def load_ledger(path: Path) -> dict:
    """Load the ledger wrapper, or start a fresh empty one."""
    if path.exists():
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("schema_version", LEDGER_SCHEMA_VERSION)
        data.setdefault("leads", [])
        return data
    return {"schema_version": LEDGER_SCHEMA_VERSION, "leads": []}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


# --- orchestration ---------------------------------------------------------

def run_export(
    input_path: Path,
    districts_csv: Path = DEFAULT_DISTRICTS,
    ledger_path: Path = DEFAULT_LEDGER,
    exports_dir: Path = DEFAULT_EXPORTS,
    schema_path: Path = DEFAULT_SCHEMA,
    org_override: Optional[str] = None,
    run_timestamp: Optional[str] = None,
    org_registry_path: Optional[Path] = None,
) -> dict:
    """Run the export. Returns a counts dict; raises nothing for bad records
    (they are refused and reported). Writes the ledger and a run file.
    """
    now = datetime.strptime(run_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    ) if run_timestamp else utc_now()
    discovered_at = iso_z(now)
    run_stamp = stamp_z(now)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    directory = load_org_directory(districts_csv)
    registry_index = load_org_registry(org_registry_path)
    ledger = load_ledger(ledger_path)
    known_ids = {lead["external_id"] for lead in ledger["leads"]}

    counts = {
        "candidates": 0, "new": 0, "already_known": 0,
        "invalid": 0, "skipped_no_org": 0, "unreconciled": 0,
    }
    warnings = []
    new_leads = []
    seen_this_run = set()

    for file_path in discover_input_files(input_path):
        org_id = org_id_for_file(file_path, org_override)
        if not org_id:
            warnings.append(
                f"{file_path.name}: cannot determine org id "
                f"(not an org_<id>.json name and no --org given); skipped"
            )
            continue
        org_meta = directory.get(org_id)
        if org_meta is None or not org_meta.get("org_name"):
            counts["skipped_no_org"] += 1
            warnings.append(f"{file_path.name}: org id {org_id} not in {districts_csv.name}; skipped")
            continue

        records = json.loads(file_path.read_text(encoding="utf-8"))
        for record in records:
            if record.get("turf_mentioned") is not True:
                continue
            if not (record.get("matches") or []):
                continue
            counts["candidates"] += 1

            lead = build_lead(record, org_id, org_meta, discovered_at, run_stamp, registry_index)
            ext_id = lead["external_id"]
            if registry_index is not None:
                reg_key = (slugify(org_meta.get("org_name", "")), org_meta.get("state", ""))
                if reg_key not in registry_index:
                    counts["unreconciled"] += 1

            errors = sorted(validator.iter_errors(lead), key=lambda e: e.path)
            if errors:
                counts["invalid"] += 1
                reason = errors[0].message
                warnings.append(f"{org_id}/{record.get('meeting_id')}: refused (invalid): {reason}")
                continue

            if ext_id in known_ids or ext_id in seen_this_run:
                counts["already_known"] += 1
                continue

            # Preserve discovered_at across runs (frozen on first sight).
            new_leads.append(lead)
            seen_this_run.add(ext_id)
            counts["new"] += 1

    # Append new leads to the ledger and write a timestamped run file.
    ledger["leads"].extend(new_leads)
    write_json(ledger_path, ledger)

    run_file = exports_dir / run_stamp / "leads.json"
    write_json(run_file, {"schema_version": LEDGER_SCHEMA_VERSION, "leads": new_leads})

    counts["run_file"] = str(run_file)
    counts["ledger_total"] = len(ledger["leads"])
    counts["warnings"] = warnings
    return counts


def _print_summary(counts: dict) -> None:
    print(
        f"Candidates: {counts['candidates']} | "
        f"new: {counts['new']} | "
        f"already-known: {counts['already_known']} | "
        f"invalid(refused): {counts['invalid']} | "
        f"skipped(no org): {counts['skipped_no_org']} | "
        f"unreconciled(not in registry): {counts.get('unreconciled', 0)}",
        file=sys.stderr,
    )
    print(f"Ledger now holds {counts['ledger_total']} lead(s).", file=sys.stderr)
    print(f"Run file: {counts['run_file']}", file=sys.stderr)
    for w in counts.get("warnings", []):
        print(f"  WARNING: {w}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export turf-mention documents to the shared core-lead shape."
    )
    parser.add_argument("--input", required=True,
                        help="Per-document JSON file, or a directory of org_<id>.json files")
    parser.add_argument("--org",
                        help="Override the BoardBook org id (for a single file whose "
                             "name does not encode it, e.g. output/leander_2026.json)")
    parser.add_argument("--districts-csv", default=str(DEFAULT_DISTRICTS),
                        help="Curated org directory CSV (org_id -> org_name/state/county)")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                        help="Ledger of every lead ever exported")
    parser.add_argument("--exports-dir", default=str(DEFAULT_EXPORTS),
                        help="Directory for per-run export files")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA),
                        help="Path to contracts/lead.schema.json")
    parser.add_argument("--org-registry",
                        help="Optional shared organization registry (the web-search "
                             "repo's organizations/registry.json, or a Supabase export). "
                             "When given, organization_id/county come from it so both "
                             "pipelines agree; orgs absent from it are flagged needs_review.")
    parser.add_argument("--run-timestamp",
                        help="Override run time as UTC ISO (YYYY-MM-DDTHH:MM:SSZ); "
                             "mainly for deterministic tests")
    args = parser.parse_args()

    counts = run_export(
        input_path=Path(args.input),
        districts_csv=Path(args.districts_csv),
        ledger_path=Path(args.ledger),
        exports_dir=Path(args.exports_dir),
        schema_path=Path(args.schema),
        org_override=args.org,
        run_timestamp=args.run_timestamp,
        org_registry_path=Path(args.org_registry) if args.org_registry else None,
    )
    _print_summary(counts)
    # Non-zero exit if any record was refused, so a timer/operator notices.
    return 1 if counts["invalid"] else 0


if __name__ == "__main__":
    sys.exit(main())
