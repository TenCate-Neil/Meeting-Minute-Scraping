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

## Step 2 - Curate the district list (human step, don't skip)

Open `districts/org_directory.csv` and review `include_in_rollout`:

- The heuristic catches ~1,480 of ~1,700 orgs. It over- and under-matches at
  the edges - e.g. regional education service agencies (`ESD`, `RESA`)
  aren't single districts and may need separate handling; some districts use
  naming conventions the regex misses (spot-check a sample).
- If you only care about a specific state or region, set
  `include_in_rollout` to `False` for everything outside scope. There is no
  state/region field from BoardBook directly - cross-reference `org_name`
  against a known district list if you need geographic filtering.
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
