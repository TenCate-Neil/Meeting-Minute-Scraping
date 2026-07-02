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
instructions/
  analysis_instructions.md What to search for, what to extract, and the output format
docs/
  ARCHITECTURE.md          How the BoardBook site works and how the scraper talks to it
  ROLLOUT.md               Step-by-step guide to running this across many districts
  DATA_STORAGE.md          What gets kept, what gets discarded, and why
districts/
  org_directory.csv        Master list of BoardBook orgs (generated, then human-curated)
output/                    Script output (JSON/CSV results, optionally PDFs) - gitignored
```

## Quickstart

```bash
pip install requests beautifulsoup4 lxml PyPDF2

# 1. One district, quick test (uses a known BoardBook org ID)
python3 scripts/scrape_boardbook.py --org 795 --limit 5

# 2. Build/refresh the master district list
python3 scripts/fetch_org_directory.py --out districts/org_directory.csv
# -> open districts/org_directory.csv and review the include_in_rollout column

# 3. Roll out across every included district
python3 scripts/run_all_districts.py --districts-csv districts/org_directory.csv
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
