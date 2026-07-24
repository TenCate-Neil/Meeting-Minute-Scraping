#!/usr/bin/env python3
"""
Export step: convert this repo's per-document turf analysis output into the
shared "core lead" shape (contract v2.0) written by the web-search agent
pipeline to the same Supabase `lead` table.

This does NOT change the scraping/analysis pipeline or its output files. It is
an additional, final step that reads the per-document JSON that
scrape_boardbook.py / run_all_districts.py already produce and emits leads that
line up field-for-field with the agent pipeline's ledger.

What it does, per run:
  1. Read per-document analysis output (a single JSON file, or a directory of
     org_<id>.json files as run_all_districts.py writes to output/districts/).
  2. Keep only documents with turf_mentioned == true.
  3. Join organization metadata (org_name, organization_id, state, county) from
     the curated districts/org_directory.csv on the BoardBook org_id.
  4. Group an org's turf hits into a PROJECT and map each project to one lead
     (see MAPPING below), then compute a deterministic external_id.
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

MAPPING (per project -> one core lead):
  external_id  sha1(organization|project_name|project_address)[:16] -- the EXACT
               recipe the web-search pipeline uses (verified byte-for-byte
               against its ledger; see tests/test_export_leads.py). project_name
               feeds the hash, so it is derived deterministically from the org +
               scope and does NOT include the meeting date -- that way several
               meetings about one project collapse to a single lead.
  source       constant "meeting-minutes".
  organization org display name from the directory.
  organization_id  agent-leading: taken from the directory's organization_id
               column (or the shared registry via --org-registry) so both
               pipelines emit the same key. Orgs with neither fall back to a
               locally-generated slug and are flagged needs_review.
  state/county from the directory (county without the " County" suffix).
  location_id  NOT emitted: this pipeline has no search areas. Geography
               resolves platform-side via organization_id -> organization /
               organization_geography; the sync sends the column as null.
  source_url   deep link to the decision meeting's minutes page, falling back to
               its agenda page when no minutes were posted.
  summary      one plain line (<=400): what the turf project is, sport, status.
  evidence_quote  the single best verbatim line from the minutes ("" if none).
  evidence     agent-shaped block (project_name, project_address, details,
               contact, source_urls, matched_terms, needs_review, run_timestamp).

Only football / soccer / baseball-softball turf produces a lead. Nothing is
fabricated: project_address is left "" until an enrichment step resolves it (so
these leads carry needs_review), contacts/dates stay "".

KNOWN LIMITATION -- granularity: an org's turf hits are aggregated into ONE
project. Splitting one board action into per-facility leads (e.g. the agent's
separate "Bible Stadium" and "Monroe Stadium" leads) needs a semantic pass over
the minutes and is a deliberate follow-up, not done here.
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
LEDGER_SCHEMA_VERSION = "2.0"
EXTERNAL_ID_LEN = 16                # web pipeline truncates the sha1 hex to 16
SUMMARY_MAX = 400                   # core lead summary maxLength (see schema)
PROJECT_NAME_MAX = 255              # evidence.project_name maxLength (see schema)
EVIDENCE_QUOTE_MAX = 500            # keep the verbatim quote to one readable line

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = REPO_ROOT / "contracts" / "lead.schema.json"
DEFAULT_DISTRICTS = REPO_ROOT / "districts" / "org_directory.csv"
DEFAULT_LEDGER = REPO_ROOT / "leads" / "ledger.json"
DEFAULT_EXPORTS = REPO_ROOT / "exports"

# org_<id>.json (as run_all_districts.py writes); capture the org id.
ORG_FILENAME_RE = re.compile(r"^org_([A-Za-z0-9]+)\.json$")

# Decisiveness of a confirmed minutes outcome, most decisive first. Used to pick
# the meeting whose minutes best represent the project's status/decision.
OUTCOME_RANK = {
    "Approved": 4,
    "Denied": 3,
    "Tabled": 2,
    "Motion made (pending/unspecified result)": 1,
}


# --- small helpers ---------------------------------------------------------

def slugify(*parts: str) -> str:
    """Deterministic ascii slug: lowercase, hyphen-joined, [a-z0-9-] only.

    Empty parts are dropped, so slugify("Leander ISD", "") -> "leander-isd"
    (no trailing hyphen) and slugify("Leander ISD", "TX") -> "leander-isd-tx".
    Only the fallback for orgs not yet reconciled with the agent side.
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


def compute_external_id(organization: str, project_name: str, project_address: str) -> str:
    """sha1(organization|project_name|project_address) truncated to 16 hex chars.

    This reproduces the web-search pipeline's external_id byte-for-byte (verified
    against its ledger). Fields are stripped, joined by '|', hashed with SHA-1,
    and the hex digest is truncated to 16 characters. It is the idempotent upsert
    key: the same project re-exported later yields the same id. It is also the
    Supabase lead_entry.entry_id (the sync maps the name at push time), so any
    change here would re-key every already-synced row — do not change it.
    """
    basis = "|".join(str(v).strip() for v in (organization, project_name, project_address))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:EXTERNAL_ID_LEN]


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


def parse_meeting_date(label: str) -> Optional[datetime]:
    """Parse a cleaned 'January 22, 2026' label; None if it doesn't parse."""
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(label.strip(), fmt)
        except ValueError:
            continue
    return None


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


# --- turf classification ---------------------------------------------------

def detect_sports(text: str) -> list:
    """Sports whose turf this project touches, from the aggregated evidence text.

    Only football / soccer / baseball / softball qualify a lead. A stadium is
    treated as a football/soccer facility. Returns e.g.
    ['football/soccer', 'baseball/softball'] or [] (no qualifying sport -> the
    project does not produce a lead). Heuristic: it reads sport words wherever
    they appear, so it can pick up a sport named as a funding source rather than
    the turfed field -- the lead is flagged needs_review for a human read.
    """
    t = (text or "").lower()
    sports = []
    if any(k in t for k in ("football", "soccer", "stadium")):
        sports.append("football/soccer")
    if "baseball" in t or "softball" in t:
        sports.append("baseball/softball")
    return sports


def derive_scope(text: str) -> str:
    """One-word scope for the project name, from the aggregated evidence text."""
    t = (text or "").lower()
    if any(k in t for k in ("replace", "replacement", "end of life",
                            "expected service", "degrad", "aging")):
        return "replacement"
    if any(k in t for k in ("install", "construct", "new turf", "new artificial", "build")):
        return "installation"
    return "project"


def status_phrase(agenda_outcomes: list, minutes_outcomes: list) -> str:
    """A short, honest status line from the classified outcomes.

    Prefers the confirmed minutes outcome (what was decided) over the agenda's
    proposed outcome. Everything here is the scraper's keyword heuristic, so the
    wording stays hedged ('per board minutes').
    """
    decided = None
    for label in ("Approved", "Denied", "Tabled"):
        if label in minutes_outcomes or label in agenda_outcomes:
            decided = label
            break
    if decided == "Approved":
        return "board approved the item"
    if decided == "Denied":
        return "board denied the item"
    if decided == "Tabled":
        return "item tabled/deferred"
    if "Motion made (pending/unspecified result)" in agenda_outcomes:
        return "motion made, result not confirmed"
    return "under discussion"


# --- loading ---------------------------------------------------------------

def load_org_directory(csv_path: Path) -> dict:
    """org_id -> {'org_name', 'organization_id', 'state', 'county'}.

    organization_id is the agent-leading shared key filled into the directory's
    organization_id column; it is "" for orgs not yet reconciled with the agent.
    """
    directory = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            org_id = (row.get("org_id") or "").strip()
            if not org_id:
                continue
            directory[org_id] = {
                "org_name": (row.get("org_name") or "").strip(),
                "organization_id": (row.get("organization_id") or "").strip(),
                "state": (row.get("state") or "").strip().upper(),
                "county": (row.get("county") or "").strip(),
            }
    return directory


def load_org_registry(path: Optional[Path]) -> Optional[dict]:
    """Load the shared organization registry (the web-search repo's
    organizations/registry.json, or a Supabase export in the same shape) into
    an index keyed by (name-slug, state) -> {'organization_id', 'county'}.

    Returns None if no path is given. When present it is the authoritative source
    for organization_id for the run: an org it does not list is flagged
    unreconciled even if the local directory carries an id for it.
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


# --- org identity resolution ----------------------------------------------

def resolve_org_identity(org_meta: dict, registry_index: Optional[dict]) -> dict:
    """Resolve organization_id + county and whether the org is reconciled.

    Precedence:
      - If a shared registry was supplied (--org-registry), it is authoritative:
        a match supplies id/county; a miss marks the org unreconciled (but we
        still emit a best-effort id from the directory column or a slug).
      - Otherwise the directory's organization_id column (agent-leading) supplies
        the id; if it is empty we fall back to a locally-generated slug.
    Returns {organization_id, county, unreconciled, oid_source}.
    """
    org_name = org_meta.get("org_name", "")
    state = org_meta.get("state", "")
    county = normalize_county(org_meta.get("county", ""))
    csv_oid = (org_meta.get("organization_id") or "").strip()

    registry_entry = (
        registry_index.get((slugify(org_name), state)) if registry_index else None
    )

    if registry_index is not None:
        if registry_entry:
            organization_id = registry_entry.get("organization_id") or csv_oid or slugify(org_name, state)
            if registry_entry.get("county"):
                county = registry_entry["county"]
            return {"organization_id": organization_id, "county": county,
                    "unreconciled": False, "oid_source": "registry"}
        # Authoritative source doesn't list this org.
        organization_id = csv_oid or slugify(org_name, state)
        return {"organization_id": organization_id, "county": county,
                "unreconciled": True, "oid_source": "csv" if csv_oid else "slugify"}

    if csv_oid:
        return {"organization_id": csv_oid, "county": county,
                "unreconciled": False, "oid_source": "csv"}
    return {"organization_id": slugify(org_name, state), "county": county,
            "unreconciled": False, "oid_source": "slugify"}


# --- project aggregation ---------------------------------------------------

def collect_hits(records: list) -> list:
    """Turf-hit documents: turf_mentioned true AND at least one match."""
    return [
        r for r in records
        if r.get("turf_mentioned") is True and (r.get("matches") or [])
    ]


def derive_projects(hits: list) -> list:
    """Group an org's turf-hit documents into projects.

    One project per org for now: several meetings about the same turf initiative
    are aggregated into a single lead. Per-facility / per-initiative splitting
    needs a semantic pass and is a deliberate follow-up (see module docstring).
    """
    return [hits] if hits else []


def _best_document(hits: list) -> dict:
    """Pick the meeting that best represents the project's decision/status:
    prefer one with posted minutes and the most decisive confirmed outcome, then
    the most recent, then the highest meeting id (deterministic tie-break).
    """
    def key(rec):
        has_minutes = 1 if rec.get("minutes_available") else 0
        outcome_rank = OUTCOME_RANK.get(rec.get("minutes_outcome") or "", 0)
        when = parse_meeting_date(clean_meeting_date(rec.get("date", ""))) or datetime.min
        mid = rec.get("meeting_id", "")
        mid_num = int(mid) if str(mid).isdigit() else 0
        return (has_minutes, outcome_rank, when, mid_num)

    return max(hits, key=key)


# --- mapping ---------------------------------------------------------------

def build_lead(hits: list, org_id: str, org_meta: dict, discovered_at: str,
               run_stamp: str, registry_index: Optional[dict] = None):
    """Map one project (an org's aggregated turf-hit documents) to a core lead.

    Returns (lead, ident) where lead is None if the project has no qualifying
    sport (football/soccer/baseball-softball) and so produces no lead; ident is
    the org-identity resolution result (used for run counters).
    """
    ident = resolve_org_identity(org_meta, registry_index)

    org_name = org_meta.get("org_name", "")
    state = org_meta.get("state", "")
    county = ident["county"]
    organization_id = ident["organization_id"]

    all_matches = [m for rec in hits for m in (rec.get("matches") or [])]
    combined_text = " ".join(
        [(m.get("context") or "") for m in all_matches]
        + [(rec.get("title") or "") for rec in hits]
        + [(rec.get("summary") or "") for rec in hits]
    )

    # Sport gate: only football/soccer/baseball-softball turf becomes a lead.
    sports = detect_sports(combined_text)
    if not sports:
        return None, ident

    scope = derive_scope(combined_text)

    terms = sorted({(m.get("term") or "").strip().lower() for m in all_matches if m.get("term")})
    topics = sorted({m.get("topic_type") for m in all_matches if m.get("topic_type")})
    sentiments = sorted({m.get("sentiment") for m in all_matches if m.get("sentiment")})
    agenda_outcomes = sorted({m.get("outcome") for m in all_matches if m.get("outcome")})
    minutes_outcomes = sorted({rec.get("minutes_outcome") for rec in hits if rec.get("minutes_outcome")})

    # project_name feeds the external_id, so keep it stable across meetings:
    # derive it from org + scope only (no meeting date, no per-doc match set).
    project_name = trim_one_line(f"{org_name} - artificial turf {scope}", PROJECT_NAME_MAX)

    # project_address is left empty until an enrichment step resolves the real
    # facility address -- we do not fabricate it, so the lead carries needs_review.
    project_address = ""

    # The decision meeting anchors source_url and the evidence quote.
    primary = _best_document(hits)
    primary_id = str(primary.get("meeting_id", "")).strip()
    primary_date = clean_meeting_date(primary.get("date", ""))
    if primary.get("minutes_available"):
        source_url = f"{BOARDBOOK_BASE}/Public/Minutes/{org_id}?meeting={primary_id}"
    else:
        source_url = f"{BOARDBOOK_BASE}/Public/Agenda/{org_id}?meeting={primary_id}"

    # evidence_quote: the single best verbatim line -- the confirmed decision
    # context from the minutes if we have it, else the longest match context.
    quote = (primary.get("minutes_context") or "").strip()
    if not quote:
        contexts = [(m.get("context") or "").strip() for m in all_matches]
        quote = max(contexts, key=len) if contexts else ""
    evidence_quote = trim_one_line(quote, EVIDENCE_QUOTE_MAX)

    status = status_phrase(agenda_outcomes, minutes_outcomes)
    sport_label = " and ".join(sports)
    summary = trim_one_line(
        f"{org_name}: artificial turf {scope} for {sport_label} field(s); "
        f"{status} (per board minutes, {primary_date}).",
        SUMMARY_MAX,
    )

    needs_review = (state == "") or (county == "") or ident["unreconciled"] or (ident["oid_source"] == "slugify")

    # source_urls: org page + a deep link per hit meeting (minutes when posted).
    source_urls = [f"{BOARDBOOK_BASE}/Public/Organization/{org_id}"]
    for rec in sorted(hits, key=lambda r: str(r.get("meeting_id", ""))):
        mid = str(rec.get("meeting_id", "")).strip()
        source_urls.append(f"{BOARDBOOK_BASE}/Public/Agenda/{org_id}?meeting={mid}")
        if rec.get("minutes_available"):
            source_urls.append(f"{BOARDBOOK_BASE}/Public/Minutes/{org_id}?meeting={mid}")
    source_urls = list(dict.fromkeys(source_urls))  # dedup, keep order

    # evidence.details: the fuller context, demoted from the core.
    meetings = "; ".join(
        f"{clean_meeting_date(rec.get('date',''))} (meeting {rec.get('meeting_id','')})"
        for rec in sorted(hits, key=lambda r: str(r.get("meeting_id", "")))
    )
    detail_bits = [
        f"Turf discussion across {len(hits)} meeting document(s): {meetings}",
        f"Matched terms: {', '.join(terms)}" if terms else "",
        f"Topics (heuristic): {'; '.join(t for t in topics if t)}" if topics else "",
        f"Sentiments (heuristic): {'; '.join(s for s in sentiments if s)}" if sentiments else "",
        f"Agenda outcome(s) (heuristic): {'; '.join(o for o in agenda_outcomes if o)}" if agenda_outcomes else "",
        # Confirmed outcome from the minutes (hybrid pass). Present only when a
        # minutes PDF was posted; distinguishes "no outcome found in minutes"
        # from "no minutes existed" (which adds no line at all).
        (f"Outcome per minutes (heuristic): "
         f"{'; '.join(o for o in minutes_outcomes if o) or 'turf item not located in minutes'}")
            if any(rec.get("minutes_available") for rec in hits) else "",
        f"Sport (heuristic, from evidence text): {sport_label}",
        "project_address not resolved from the minutes; left blank for an enrichment step.",
    ]
    if ident["unreconciled"]:
        detail_bits.append(
            "Org not found in the shared registry; organization_id is best-effort "
            "and must be reconciled before merge."
        )
    elif ident["oid_source"] == "slugify":
        detail_bits.append(
            "organization_id generated locally (org not yet in the shared "
            "registry / directory column); reconcile before merge."
        )
    details = ". ".join(b.rstrip(".") for b in detail_bits if b) + "."

    evidence = {
        "project_name": project_name,
        "project_address": project_address,
        "projected_start_date": "",
        "details": details,
        "contact": {
            "first_name": "", "last_name": "", "title": "", "phone": "", "email": "",
        },
        "source_urls": source_urls,
        "matched_terms": terms,
        "needs_review": needs_review,
        "run_timestamp": run_stamp,
    }

    lead = {
        "external_id": compute_external_id(org_name, project_name, project_address),
        "source": SOURCE,
        "organization": org_name,
        "organization_id": organization_id,
        "state": state,
        "county": county,
        "summary": summary,
        "evidence_quote": evidence_quote,
        "source_url": source_url,
        "discovered_at": discovered_at,
        "evidence": evidence,
    }
    return lead, ident


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
        "candidates": 0, "new": 0, "already_known": 0, "invalid": 0,
        "skipped_no_org": 0, "skipped_non_sport": 0, "unreconciled": 0,
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
        for hits in derive_projects(collect_hits(records)):
            lead, ident = build_lead(
                hits, org_id, org_meta, discovered_at, run_stamp, registry_index
            )
            if lead is None:
                counts["skipped_non_sport"] += 1
                continue

            counts["candidates"] += 1
            if ident["unreconciled"]:
                counts["unreconciled"] += 1

            ext_id = lead["external_id"]
            errors = sorted(validator.iter_errors(lead), key=lambda e: e.path)
            if errors:
                counts["invalid"] += 1
                warnings.append(f"{org_id}: refused (invalid): {errors[0].message}")
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
        f"skipped(non-sport): {counts['skipped_non_sport']} | "
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
                        help="Curated org directory CSV (org_id -> org_name/organization_id/state/county)")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                        help="Ledger of every lead ever exported")
    parser.add_argument("--exports-dir", default=str(DEFAULT_EXPORTS),
                        help="Directory for per-run export files")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA),
                        help="Path to contracts/lead.schema.json")
    parser.add_argument("--org-registry",
                        help="Optional shared organization registry (the web-search "
                             "repo's organizations/registry.json, or a Supabase export). "
                             "When given it is authoritative for organization_id/county; "
                             "orgs absent from it are flagged needs_review.")
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
