# Rollout guide: scaling from one district to all districts

This is the step-by-step procedure for taking the pipeline validated against
a single district (Leander ISD, BoardBook org 795) and running it across
every curated district on every supported platform.

## Step 1 - Refresh the district directory

`districts/district_directory.csv` is the master input: **one row per
district-platform pair**, keyed `(platform, platform_org_id)`. It is built
by merge-upsert from three kinds of sources - re-running any of them never
clobbers hand-curated fields:

```bash
# live platform directories (only the BoardBook family has one)
python3 scripts/fetch_org_directory.py --platform boardbook
python3 scripts/fetch_org_directory.py --platform sparq
python3 scripts/fetch_org_directory.py --platform boeconnect

# research-validated seed rows (BoardDocs slug lists, hidden BoardBook orgs,
# East-TN/NE curation, deferred platforms) - already merged, re-run if edited
python3 scripts/fetch_org_directory.py --seed districts/seeds/boarddocs.csv
python3 scripts/fetch_org_directory.py --seed districts/seeds/boeconnect_east_tn.csv

# one-time migration of the legacy BoardBook-only directory (already done;
# districts/org_directory.csv stays in the repo as the pre-migration reference)
python3 scripts/fetch_org_directory.py --migrate-legacy districts/org_directory.csv
```

Columns:

| column | meaning |
|---|---|
| `organization_id` | Shared lead-platform slug for this org, **agent-leading** (e.g. `leander-isd-tx`). Filled from the web-search/agent side so both pipelines emit the same key; blank until an org is reconciled. Used by `scripts/export_leads.py`. |
| `org_name` | Display name (from the platform directory where one exists; BoardDocs names are filled by enrichment) |
| `state` | 2-letter USPS abbreviation (BoardDocs rows derive it from the `{state}/` org-ref prefix) |
| `county` | County name with the ` County` suffix (the export strips it), or the correct local equivalent |
| `counties`, `place` | Optional geography extras (multi-county districts, city anchors) per docs/SCHEMA_ALIGNMENT_PLAN.md |
| `platform` | `boardbook` / `sparq` / `boeconnect` / `boarddocs` / `agendaquick` / `diligent-community` / `apptegy` |
| `platform_org_id` | Platform-scoped org id: numeric or slug for the BoardBook family, `state/slug` for BoardDocs |
| `likely_school_district` | Name-pattern heuristic (`ISD`, `USD`, `school district`, `academy`, ...) |
| `include_in_rollout` | Defaults to the heuristic (or the seed's explicit value); **edit this column by hand** |
| `notes` | Why a row is blank/excluded, dual-hosting hints, validation provenance |

Dual-hosted districts (e.g. Kingsport TN on both BOEconnect and BoardDocs,
Blue Valley KS mid-migration) intentionally have one row per platform; the
lead layer collapses them because the same org + project produces the same
`external_id`. Same-name traps exist across states (a "Haywood County" on
BOEconnect is TN, not NC) - the `state` column is authoritative, never match
orgs by name alone.

Two platform-specific gaps to know about:

- **BoardDocs has no public client directory.** Its rows come from
  `districts/seeds/boarddocs.csv` (validated slug lists for OH/NY/KS/NC/TN);
  names/counties are filled by enrichment (below).
- **BoardBook hides some live orgs from its directory** (36 known Kansas
  districts). Known ids are seeded; to find more, sweep the id space:
  `python3 scripts/probe_boardbook_orgs.py --range 1 4000 --title-contains USD --state KS`.

`state`/`county` gaps (and BoardDocs `org_name` gaps) are filled by a
three-pass process, in increasing order of manual cost - re-run in this
order if the directory is ever refreshed from scratch:

1. `python3 scripts/enrich_org_directory.py --csv districts/district_directory.csv`
   - scrapes each org's platform page for its posted address (BoardBook
     family: the meeting-location maps link; BoardDocs: the site header,
     which also supplies the district name), then geocodes it via the Census
     Bureau's free public geocoder. Two HTTP requests per org. Saves every 25
     rows and skips rows that already have a `state` unless you pass
     `--force`, so it's safe to interrupt and resume. `--platform boarddocs`
     restricts a pass to one platform.
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

Open `districts/district_directory.csv` and review `include_in_rollout`:

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
  --district-limit 5 \
  --limit-per-district 3 \
  --out-dir output/districts_smoketest \
  --summary-out output/rollout_summary_smoketest.json
```

To smoke-test one platform at a time, add e.g. `--platforms sparq` or
`--platforms boarddocs,boeconnect`.

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
python3 scripts/run_all_districts.py --start-date 2023-01-01
```

Widen the window later for specific districts once you've found evidence of
turf activity and want the full history.

## Step 5 - Run the full rollout

```bash
python3 scripts/run_all_districts.py \
  --start-date 2023-01-01 \
  --out-dir output/districts \
  --summary-out output/rollout_summary.json \
  --sleep 0.5
```

- `--sleep` adds a politeness delay between requests within each district;
  raise it if a platform starts rate-limiting (watch for HTTP 429/503 in
  stderr). BoardDocs additionally enforces its own 1s-per-request minimum
  inside the adapter regardless of `--sleep`.
- Rows on platforms whose adapter is deferred (agendaquick,
  diligent-community, apptegy) are reported as `skipped_platform_deferred`
  in the summary - expected, not a failure.
- This will take a long time for the full list run sequentially - there is
  no built-in parallelism. If turnaround matters, shard
  `districts/district_directory.csv` into N files and run N instances of
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

For each district with hits, open `output/districts/{platform}_{org}.json`
(legacy `org_{id}.json` files are renamed to the new scheme on first touch). Each
match already carries a heuristic `topic_type`, `sentiment`, and `outcome`
(and each document a `summary`) per `instructions/analysis_instructions.md`,
computed by keyword matching - fine for a first sort, but re-read the quoted
`context` yourself (or route it to an LLM) before treating the label as
final, especially for anything you'd cite externally.

## Handling failures and gaps

- **`status: "failed"` in the rollout summary** - the org ID may be wrong,
  or the platform changed something. Re-run `scrape_meetings.py --platform
  <p> --org <id> --meeting-id <one_id>` directly to see the raw error.
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
