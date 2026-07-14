# Trial Meeting Minute Scraping

Scrapes public school board meeting agendas/minutes from
[BoardBook](https://meetings.boardbook.org/Public) and analyzes each document
for mentions of artificial/synthetic turf, so that discussion of turf topics
(procurement, budget, replacement, sentiment) can be tracked across many
school districts without manually opening every agenda PDF.

The process is designed to scale from **one district** to **all districts
listed on BoardBook** (~1,700 organizations, ~1,480 of which look like school
districts by name).

## How it works, in one sentence

For each district: fetch its public meeting list → download each meeting's
agenda/minutes PDF → extract text → regex-search for turf terms with
surrounding context → write structured JSON/CSV output → discard the PDF
unless a match was found.

## Repository layout

```
scripts/
  fetch_org_directory.py   Scrape the BoardBook org directory into a CSV you curate
  scrape_boardbook.py      Scrape + analyze one district (one BoardBook org ID)
  run_all_districts.py     Orchestrate scrape_boardbook.py across a curated district list
  export_leads.py          Convert turf-hit documents into the shared core-lead shape
instructions/
  analysis_instructions.md What to search for, what to extract, and the output format
contracts/
  lead.schema.json         Shared lead contract (v2.0), copied from the web-search repo
docs/
  ARCHITECTURE.md          How the BoardBook site works and how the scraper talks to it
  ROLLOUT.md               Step-by-step guide to running this across many districts
  DATA_STORAGE.md          What gets kept, what gets discarded, and why
districts/
  org_directory.csv        Master list of BoardBook orgs (generated, then human-curated)
leads/
  ledger.json              Every lead ever exported, keyed by external_id (tracked)
exports/                   Per-run export snapshots (generated) - gitignored
output/                    Script output (JSON/CSV results, optionally PDFs) - gitignored
```

## Quickstart

```bash
pip install requests beautifulsoup4 lxml PyPDF2 jsonschema

# 1. One district, quick test (uses a known BoardBook org ID)
python3 scripts/scrape_boardbook.py --org 795 --limit 5

# 2. Build/refresh the master district list
python3 scripts/fetch_org_directory.py --out districts/org_directory.csv
# -> open districts/org_directory.csv and review the include_in_rollout column

# 3. Roll out across every included district
python3 scripts/run_all_districts.py --districts-csv districts/org_directory.csv

# 4. (Optional) also export turf hits to the shared lead shape in one go
python3 scripts/run_all_districts.py --districts-csv districts/org_directory.csv --export-leads
```

See [docs/ROLLOUT.md](docs/ROLLOUT.md) for the full rollout procedure,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the BoardBook endpoints
were reverse-engineered, and [docs/DATA_STORAGE.md](docs/DATA_STORAGE.md) for
the document retention policy.

## What "org 795" was

`795` is the BoardBook organization ID for **Leander ISD**, used as the pilot
district while building this pipeline. Every district on BoardBook has its
own numeric org ID, visible in the URL when you open its page from
https://meetings.boardbook.org/Public (e.g. `/Public/Organization/795`).

## Output format

Every run produces:
- `*.json` — one record per meeting document, with `turf_mentioned`,
  matched terms, and quoted context (matches the schema in
  `instructions/analysis_instructions.md`)
- `*.csv` — flat summary of the same, for quick spreadsheet review

`run_all_districts.py` additionally writes an aggregated
`output/rollout_summary.json` with a per-district hit count, so you can
triage which districts need a closer look without opening every file.

## Exporting leads (shared platform shape)

The scrape/analysis output above is per-document and repo-specific. A separate
final step converts turf-hit documents into the **shared core-lead shape**
(`contracts/lead.schema.json`, contract v2.0) that both this pipeline and the
web-search agent pipeline feed into the same Supabase/Retool platform. This
step never changes the scraping/analysis logic or its output files.

```bash
# A directory of org_<id>.json files (what run_all_districts.py writes):
python3 scripts/export_leads.py --input output/districts

# A single results file whose name does not encode the org id:
python3 scripts/export_leads.py --input output/leander_2026.json --org 795
```

How it behaves:

- Keeps only documents with `turf_mentioned: true`; one lead per document.
- Joins `org_name` / `state` / `county` from `districts/org_directory.csv` on
  the BoardBook `org_id` (the per-document JSON does not carry these itself).
- Computes a deterministic `external_id` so the same document re-analyzed later
  produces the same id — Supabase upserts stay idempotent.
- **Ledger-first:** `leads/ledger.json` holds every lead ever exported. Each
  run appends only new ids and writes just the new leads to
  `exports/<UTC timestamp>/leads.json`. Re-running adds nothing new.
- Validates every record against the schema and refuses to write invalid ones
  (a document whose org has no enriched `state`, for example, cannot form a
  valid lead and is reported rather than written).

`leads/ledger.json` is committed; the per-run `exports/` snapshots are
gitignored (reproducible from the ledger). Both `source` values are fixed:
every lead from this repo is `source: "meeting-minutes"`.

## Known limitations

- Not every meeting has a document posted — BoardBook returns a redirect
  instead of a PDF in that case. The scraper records this as
  `error: "download_failed"` rather than a crash; it is expected, not a bug.
- The school-district filter in `fetch_org_directory.py` is a name-based
  heuristic (`ISD`, `CISD`, `school district`, etc.) and should be
  spot-checked, not trusted blindly — some regional education agencies and
  a handful of non-school entities slip through in either direction.
- PDF text extraction (PyPDF2) works on native/text-based PDFs. A scanned
  image-only PDF with no embedded text layer will yield no extractable text
  and therefore no matches even if turf is discussed on the page. See
  docs/ROLLOUT.md for how to spot this.
