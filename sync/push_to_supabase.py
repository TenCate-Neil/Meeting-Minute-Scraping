#!/usr/bin/env python3
"""Push this repo's lead ledger into the shared Supabase `lead` table.

This is the meeting-minutes counterpart of the web-search agent repo's
sync/push_to_supabase.py. Both pipelines write into the SAME `lead` table
(created by that repo's sql/schema.sql); rows from this repo carry
source = "meeting-minutes". Only the lead table is synced from here — the
organization / search_area / source / run tables are owned by the agent repo.

Behavior, mirroring the agent sync:
  - Reads leads/ledger.json and sends only the pipeline-owned core columns
    plus the whole `evidence` block (one JSONB column).
  - Upserts on external_id via PostgREST merge-duplicates, so re-running only
    refreshes; it never duplicates.
  - NEVER sends the lifecycle columns (status, rejected_reason, assigned_bdm),
    so a BDM's edits in Retool survive every re-sync.

One extra step the agent sync does not need: lead.organization_id is a
foreign key to the organization table, which the agent side owns. Most of
this repo's districts are not registered there yet, so before pushing we
fetch the known organization_ids and send NULL for any lead whose id is not
(yet) registered — otherwise Postgres would reject the row outright. The
ledger keeps the local id; once the org is registered, a re-run fills the
column in. Each nulled id is reported so reconciliation work stays visible.

Setup: set two environment variables (or put them in sync/.env — see
sync/.env.example):

    SUPABASE_URL=https://<project>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=<service-role key>   # bypasses RLS; keep secret

Usage:
    python3 sync/push_to_supabase.py --dry-run     # transform + print, no network
    python3 sync/push_to_supabase.py               # push the ledger
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Repo root = the directory that contains this sync/ folder.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = ROOT / "leads" / "ledger.json"

# Pipeline-owned lead columns (must match the agent repo's sql/schema.sql).
# bid_due_date is a nullable column this pipeline does not extract yet; the
# ledger carries no such field, so the row sends null — same as the agent sync.
CORE_FIELDS = [
    "external_id", "source", "organization", "organization_id", "state",
    "county", "summary", "evidence_quote", "source_url", "discovered_at",
    "bid_due_date", "location_id",
]

BATCH_SIZE = 500  # PostgREST accepts row arrays; batch so a request never gets huge.
ORG_PAGE_SIZE = 1000


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from sync/.env into os.environ if not already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# Transform: ledger lead -> table row (only pipeline-owned columns)
# --------------------------------------------------------------------------- #

def lead_rows(ledger_path: Path) -> list[dict]:
    """Core lead columns + the evidence JSONB. Lifecycle columns are omitted on
    purpose so an upsert never clobbers a BDM's status / assignment in Retool."""
    with ledger_path.open(encoding="utf-8") as fh:
        doc = json.load(fh)
    rows = []
    for lead in doc.get("leads", []):
        row = {f: lead.get(f) for f in CORE_FIELDS}
        row["evidence"] = lead.get("evidence")  # whole nested block -> jsonb
        rows.append(row)
    return rows


def apply_org_preflight(rows: list[dict], known_org_ids: set) -> list[str]:
    """Null out organization_id values the Supabase organization table does not
    know, so the FK does not reject the row. Returns one warning per nulled id;
    the ledger itself is not modified, so a later re-run (after the org is
    registered on the agent side) restores the value."""
    warnings = []
    for row in rows:
        org_id = row.get("organization_id")
        if org_id and org_id not in known_org_ids:
            warnings.append(
                f"{row.get('external_id')}: organization_id '{org_id}' not in the "
                f"shared organization table; sent as null until it is registered"
            )
            row["organization_id"] = None
    return warnings


# --------------------------------------------------------------------------- #
# HTTP (PostgREST)
# --------------------------------------------------------------------------- #

def _headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _request_with_retry(req: urllib.request.Request, what: str, attempts: int = 4) -> bytes:
    delay = 2.0
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            # 4xx is a data/schema/auth problem — retrying will not help.
            if exc.code < 500:
                raise SystemExit(f"[{what}] failed ({exc.code}): {detail}")
            last = f"{exc.code}: {detail}"
        except urllib.error.URLError as exc:
            last = str(exc.reason)
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2
    raise SystemExit(f"[{what}] failed after {attempts} attempts: {last}")


def fetch_known_org_ids(base_url: str, key: str) -> set:
    """All organization_ids currently in the shared organization table."""
    ids: set = set()
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {"select": "organization_id", "limit": ORG_PAGE_SIZE, "offset": offset}
        )
        req = urllib.request.Request(
            f"{base_url}/rest/v1/organization?{query}", headers=_headers(key)
        )
        page = json.loads(_request_with_retry(req, "fetch organizations"))
        ids.update(r["organization_id"] for r in page)
        if len(page) < ORG_PAGE_SIZE:
            return ids
        offset += ORG_PAGE_SIZE


def upsert_leads(rows: list[dict], base_url: str, key: str) -> None:
    """POST rows to PostgREST with merge-duplicates so it becomes an upsert."""
    url = f"{base_url}/rest/v1/lead?on_conflict=external_id"
    headers = {**_headers(key), "Prefer": "resolution=merge-duplicates,return=minimal"}
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        body = json.dumps(batch).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        _request_with_retry(req, "lead upsert")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push this repo's lead ledger into the shared Supabase lead table."
    )
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                        help="Ledger to push (default: leads/ledger.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Transform and print row count + a sample row; no network calls.")
    args = parser.parse_args()

    load_env_file(ROOT / "sync" / ".env")

    rows = lead_rows(Path(args.ledger))
    if args.dry_run:
        print(f"lead: {len(rows)} row(s)")
        if rows:
            sample = json.dumps(rows[0], default=str)
            print(f"    e.g. {sample[:300]}{'...' if len(sample) > 300 else ''}")
        print("dry run complete — nothing was sent.")
        return 0

    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not key:
        raise SystemExit(
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (env or sync/.env), "
            "or pass --dry-run. See sync/.env.example."
        )

    if not rows:
        print("lead: 0 rows, nothing to push.")
        return 0

    known_org_ids = fetch_known_org_ids(base_url, key)
    warnings = apply_org_preflight(rows, known_org_ids)
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)

    upsert_leads(rows, base_url, key)
    print(f"lead: upserted {len(rows)} row(s) "
          f"({len(warnings)} with organization_id sent as null).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
