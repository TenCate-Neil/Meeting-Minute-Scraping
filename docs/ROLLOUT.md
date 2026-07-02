# Rollout guide: scaling from one district to all districts

This is the step-by-step procedure for taking the pipeline validated against
a single district (Leander ISD, org 795) and running it across every school
district listed on BoardBook.

## Step 1 - Refresh the district directory

```bash
python3 scripts/fetch_org_directory.py --out districts/org_directory.csv
```

This scrapes the ~1,700 organizations listed at
`https://meetings.boardbook.org/Public` and writes
`districts/org_directory.csv` with columns:

| column | meaning |
|---|---|
| `org_id` | BoardBook's numeric organization ID |
| `org_name` | Display name as listed on BoardBook |
| `likely_school_district` | Name-pattern heuristic (`ISD`, `CISD`, `school district`, `academy`, `charter`, `ESD`, `RESA`) |
| `include_in_rollout` | Defaults to the heuristic; **edit this column by hand** |
| `state` | 2-letter USPS abbreviation (or `DC`) |
| `county` | County name, or the correct local equivalent (Alaska borough/census area, etc.) |
| `notes` | Why a row is blank/excluded, when it is - see below |

`state`/`county` are not filled in by this step. Populating them was a
three-pass process, in increasing order of manual cost - re-run in this
order if the directory is ever refreshed from scratch:

1. `python3 scripts/enrich_org_directory.py --csv districts/org_directory.csv`
   - scrapes each org's BoardBook page for its posted meeting address, then
     geocodes it via the Census Bureau's free public geocoder. Two HTTP
     requests per org, ~20-30 minutes for the full directory. Saves every 25
     rows and skips rows that already have a `state` unless you pass
     `--force`, so it's safe to interrupt and resume.
2. Match remaining blanks by name against the NCES Common Core of Data
   school-district directory (a free, no-key API at
   `educationdata.urban.org` - see `docs/ARCHITECTURE.md`). Exact matches
   first; then fuzzy matches constrained to a row's already-known `state`
   with a high similarity threshold and a clear margin over the runner-up
   (unconstrained cross-state fuzzy matching produced wrong matches in
   testing - e.g. an Alaska "Petersburg School District" nearly matched a
   same-named Texas ISD - so state-constraining is not optional).
3. Whatever's still blank after that gets resolved by actual web search
   (one query per org), reserved for last because it's the only step with
   real per-row labor cost.

On the full run: **99% of orgs resolved a `state`, 99% resolved a `county`**.
Every row still blank has a reason recorded in `notes`:
- Confirmed BoardBook demo/placeholder orgs (fictional addresses like
  "123 Yellow Brick Road, Austin, TX 12345") - not real institutions.
- Explicit test/trial/demo entries (name contains "test", "trial", "demo",
  or "to be removed") - BoardBook's own sandbox accounts, never geocoded.
- DC-based orgs - Washington DC has no counties, so `county` is correctly
  empty, not missing.
- A couple of genuinely ambiguous same-name districts (e.g. "Mitchell
  School District" exists in both SD and OR, and BoardBook's own page for
  that org has no address to disambiguate with) - left blank rather than
  guessed.
- One org search couldn't confirm as a real, locatable entity at all.

None of these are guesses - if a row is blank, it's blank on purpose, and
`notes` says why.

## Step 2 - Curate the district list (human step, don't skip)

Open `districts/org_directory.csv` and review `include_in_rollout`:

- The heuristic catches ~1,480 of ~1,700 orgs. It over- and under-matches at
  the edges - e.g. regional education service agencies (`ESD`, `RESA`)
  aren't single districts and may need separate handling; some districts use
  naming conventions the regex misses (spot-check a sample).
- If you only care about a specific state, filter on the `state` column
  directly (now populated for ~93% of rows) and set `include_in_rollout` to
  `False` for everything outside scope. For the ~7% with a blank `state`,
  cross-reference `org_name` by hand if they matter to your scope.
- Commit the curated CSV so the rollout is reproducible and reviewable.

## Step 3 - Smoke-test on a handful of districts

Before running the full list, sanity-check on 3-5 districts with a small
per-district cap:

```bash
python3 scripts/run_all_districts.py \
  --districts-csv districts/org_directory.csv \
  --district-limit 5 \
  --limit-per-district 3 \
  --out-dir output/districts_smoketest \
  --summary-out output/rollout_summary_smoketest.json
```

Check `output/rollout_summary_smoketest.json` - `status` should be `"ok"`
for every district (a `"failed"` status means the district's org ID is
invalid or BoardBook returned an unexpected response, not necessarily "no
documents"; a document-level `download_failed` inside the per-district JSON
just means that one meeting had no PDF posted, which is normal).

## Step 4 - Decide on a date range

Some districts have 20+ years of meetings on BoardBook. Scraping full
history for ~1,480 districts is a lot of PDF downloads. Recommended default:
scope to a rolling window relevant to procurement/budget cycles, e.g. the
last 3 years:

```bash
python3 scripts/run_all_districts.py \
  --districts-csv districts/org_directory.csv \
  --start-date 2023-01-01
```

Widen the window later for specific districts once you've found evidence of
turf activity and want the full history.

## Step 5 - Run the full rollout

```bash
python3 scripts/run_all_districts.py \
  --districts-csv districts/org_directory.csv \
  --start-date 2023-01-01 \
  --out-dir output/districts \
  --summary-out output/rollout_summary.json \
  --sleep 0.5
```

- `--sleep` adds a politeness delay between requests within each district;
  raise it if BoardBook starts rate-limiting (watch for HTTP 429/503 in
  stderr).
- This will take a long time for the full list run sequentially - there is
  no built-in parallelism. If turnaround matters, shard
  `districts/org_directory.csv` into N files and run N instances of
  `run_all_districts.py` in parallel, each against its own shard and its own
  `--out-dir`.

## Step 6 - Triage results

```bash
python3 -c "
import json
data = json.load(open('output/rollout_summary.json'))
hits = [d for d in data if d.get('turf_hits', 0) > 0]
for d in sorted(hits, key=lambda x: -x['turf_hits']):
    print(d['org_id'], d['org_name'], d['turf_hits'], d['hit_meeting_ids'])
"
```

For each district with hits, open `output/districts/org_{id}.json`. Each
match already carries a heuristic `topic_type`, `sentiment`, and `outcome`
(and each document a `summary`) per `instructions/analysis_instructions.md`,
computed by keyword matching - fine for a first sort, but re-read the quoted
`context` yourself (or route it to an LLM) before treating the label as
final, especially for anything you'd cite externally.

## Handling failures and gaps

- **`status: "failed"` in the rollout summary** - the org ID may be wrong,
  or BoardBook changed something. Re-run `scrape_boardbook.py --org <id>
  --meeting-id <one_id>` directly to see the raw error.
- **`error: "download_failed"` on individual meetings** - normal; that
  meeting had no agenda/minutes document posted. No action needed.
- **Zero matches across an entire district with many meetings** - plausible
  and expected for most districts most of the time; turf procurement is not
  a frequent agenda item. Don't treat a zero-hit district as a scraping
  failure unless `documents_analyzed` is also suspiciously low or zero.
- **Scanned/image-only PDFs** - if a district consistently shows `pages: 0`
  extracted text despite the PDF clearly having pages, its packets are
  likely scanned images with no text layer. PyPDF2 won't extract text from
  these; OCR (e.g. `pytesseract`) would be a separate follow-up for those
  specific districts, not something to build in by default given the low
  frequency expected.
