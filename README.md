# Trial Meeting Minute Scraping

Scrapes public school board meeting agendas/minutes from the central
platforms districts publish on - BoardBook Premier, Sparq Meetings (NE),
BOEconnect (TN), BoardDocs (Diligent) - and analyzes each document for
mentions of artificial/synthetic turf, so that discussion of turf topics
(procurement, budget, replacement, sentiment) can be tracked across many
school districts without manually opening every agenda PDF.

The process is designed to scale from **one district** to **every district
in the directory** (~2,500 district-platform rows across the pilot
geographies: Nebraska, Kansas, Ohio, New York, East Tennessee, Western North
Carolina, plus the original BoardBook footprint).

## How it works, in one sentence

For each district: fetch its public meeting list from its platform → download
each meeting's agenda/minutes document → extract text (PDF or HTML) →
regex-search for turf terms with surrounding context → write structured
JSON/CSV output → discard the document unless a match was found.

Only the *fetching* is platform-specific (see `scripts/platforms/`); the
analysis, scrape state, lead export and Supabase sync are one shared,
platform-agnostic pipeline. Adding a platform means one adapter class plus
directory rows - nothing downstream changes.

## Repository layout

```
scripts/
  platforms/               Platform adapters: the ONLY platform-specific code
    boardbook_family.py    BoardBook / Sparq / BOEconnect (one product, 3 domains)
    boarddocs.py           BoardDocs (different architecture; HTML documents)
  scrape_meetings.py       Scrape + analyze one district on any platform
  scrape_boardbook.py      Back-compat wrapper (BoardBook is the default platform)
  run_all_districts.py     Orchestrate scrape_meetings.py across the curated directory
  fetch_org_directory.py   Build/refresh districts/district_directory.csv (merge-upsert)
  enrich_org_directory.py  Fill org_name/state/county gaps from platform pages + geocoder
  probe_boardbook_orgs.py  Find BoardBook-family orgs hidden from the public directory
  export_leads.py          Convert turf-hit documents into the shared core-lead shape
  scrape_state.py          Document-level scrape state: skip/recheck decisions for re-runs
instructions/
  analysis_instructions.md What to search for, what to extract, and the output format
contracts/
  lead.schema.json         Shared lead contract (v2.0), copied from the web-search repo
docs/
  ARCHITECTURE.md          Platform endpoints + how the adapter layer is organized
  ROLLOUT.md               Step-by-step guide to running this across many districts
  DATA_STORAGE.md          What gets kept, what gets discarded, and why
  SCHEMA_ALIGNMENT_PLAN.md How leads align with the shared agent/Supabase schema
sync/
  push_to_supabase.py      Upsert the lead ledger into the shared Supabase table
tests/                     Offline test suite (fixture-based, no network needed)
districts/
  district_directory.csv   Master list, ONE ROW PER DISTRICT-PLATFORM PAIR (curated)
  seeds/                   Research-validated seed rows merged into the directory
  org_directory.csv        Legacy BoardBook-only list (kept as reference; migrated)
leads/
  ledger.json              Every lead ever exported, keyed by external_id (tracked)
state/
  scrape_state.json        What was scraped when, per platform:org/meeting (tracked)
exports/                   Per-run export snapshots (generated) - gitignored
output/                    Script output (JSON/CSV results, optionally PDFs) - gitignored
```

## Quickstart

```bash
pip install requests beautifulsoup4 lxml PyPDF2 jsonschema

# 1. One district, quick test (BoardBook is the default platform)
python3 scripts/scrape_meetings.py --org 795 --limit 5

# ... and the same pipeline on the other platforms:
python3 scripts/scrape_meetings.py --platform sparq --org 120 --limit 5        # Omaha PS
python3 scripts/scrape_meetings.py --platform boeconnect --org kcs --limit 5   # Kingsport
python3 scripts/scrape_meetings.py --platform boarddocs --org ny/albany --limit 5

# 2. Refresh the master district directory (merge-upsert; curation survives)
python3 scripts/fetch_org_directory.py --platform boardbook
python3 scripts/fetch_org_directory.py --platform sparq
python3 scripts/fetch_org_directory.py --platform boeconnect
# -> review the include_in_rollout column in districts/district_directory.csv

# 3. Roll out across every included district, all platforms
python3 scripts/run_all_districts.py

# 4. (Optional) also export turf hits to the shared lead shape in one go
python3 scripts/run_all_districts.py --export-leads

# 5. Run the offline test suite (fixture-based, no network needed)
pip install pytest && python3 -m pytest tests -q
```

See [docs/ROLLOUT.md](docs/ROLLOUT.md) for the full rollout procedure,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the per-platform endpoints
and the adapter layer, and [docs/DATA_STORAGE.md](docs/DATA_STORAGE.md) for
the document retention policy.

## Source platforms

| platform | hosts | org id form | documents |
|---|---|---|---|
| `boardbook` | meetings.boardbook.org (~1,700 orgs) | numeric or slug (`795`) | PDF |
| `sparq` | meeting.sparqdata.com (302 orgs, ~70% of NE districts) | numeric or slug (`120`, `almaschools`) | PDF |
| `boeconnect` | meeting.boeconnect.net (47 orgs, 40 TN systems) | numeric or slug (`571`, `kcs`) | PDF |
| `boarddocs` | go.boarddocs.com (per-client sites; no public directory) | `state/slug` (`ny/albany`) | HTML |
| `agendaquick`, `diligent-community`, `apptegy` | deferred - a few directory rows exist (none yet for apptegy), adapters do not | | |

BoardBook, Sparq and BOEconnect are the same white-labeled product, so one
adapter serves all three. A district can be listed on several platforms
(dual-hosting is real - e.g. Kingsport TN posts on BOEconnect *and*
BoardDocs); the directory keeps one row per district-platform pair, and the
lead layer deduplicates naturally because the same organization + project
yields the same `external_id` regardless of source platform.

## What "org 795" was

`795` is the BoardBook organization ID for **Leander ISD**, used as the pilot
district while building this pipeline (and still the regression baseline).
Org ids are platform-scoped strings: numeric or slug for the BoardBook
family, `state/slug` for BoardDocs - visible in the URL of the org's public
page (e.g. `/Public/Organization/795`, `/Public/Organization/kcs`,
`go.boarddocs.com/ny/albany/...`).

## Output format

Every run produces:
- `*.json` — one record per meeting document, with `turf_mentioned`,
  matched terms, and quoted context (matches the schema in
  `instructions/analysis_instructions.md`)
- `*.csv` — flat summary of the same, for quick spreadsheet review

`run_all_districts.py` additionally writes an aggregated
`output/rollout_summary.json` with a per-district hit count, so you can
triage which districts need a closer look without opening every file.

### Confirming outcomes from the minutes

The agenda shows what was *proposed*; the minutes show what was *decided*. For
every meeting that is a turf hit (and only those), the scraper also fetches the
meeting's minutes PDF, when one exists, and records the confirmed decision as
`minutes_outcome` (plus `minutes_available` / `minutes_context`). This adds
only a handful of downloads per rollout since turf hits are rare. Pass
`--skip-minutes` to turn it off. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ("Confirming outcomes from the
minutes") for the rationale and the agenda-vs-minutes trade-off.

### Incremental re-runs (no double scraping)

Re-running the scraper does not re-download documents it already captured.
`state/scrape_state.json` (tracked in git, unlike `output/`) records, per
org and meeting, when it was scraped and what was captured. Org keys are
platform-namespaced (`boardbook:795`, `sparq:120`, `boarddocs:ny/albany`)
because meeting ids are only unique within one platform; state files written
before the multi-platform change migrate in place on first load. On each run
the scraper skips a meeting unless something is still missing:

- a meeting never seen before is processed;
- a meeting whose PDF could not be fetched last time is **retried** (documents
  are often posted late) — until the meeting is older than
  `--minutes-recheck-days` (default 180), after which it is finalized;
- a turf-hit meeting whose minutes were not yet posted is **rechecked**, since
  the confirmed decision (`minutes_outcome`) only exists once minutes appear;
- everything else is skipped.

The per-org JSON output stays cumulative: records for skipped meetings are
carried forward from the existing output file, so `export_leads.py` always
sees the org's full turf-hit history. Each org also gets a `last_scraped_at`
stamp, answering "when did we last scrape this district".

Escape hatches: `--force-rescrape` re-processes everything (still updating the
state), `--no-state` ignores the mechanism entirely, and an explicit
`--meeting-id` run is always processed. This state covers the
meeting-minutes scraping only; the web-search agent pipeline keeps its own
re-run bookkeeping in its own repo.

## Exporting leads (shared platform shape)

The scrape/analysis output above is per-document and repo-specific. A separate
final step converts turf-hit documents into the **shared core-lead shape**
(`contracts/lead.schema.json`, contract v2.0) that both this pipeline and the
web-search agent pipeline feed into the same Supabase/Retool platform. This
step never changes the scraping/analysis logic or its output files, and it is a
plain deterministic script — no LLM/agent calls on the meeting-minutes side.

```bash
# A directory of per-district files (what run_all_districts.py writes -
# {platform}_{org}.json, plus legacy org_<id>.json):
python3 scripts/export_leads.py --input output/districts

# A single results file whose name does not encode the org id:
python3 scripts/export_leads.py --input output/leander_2026.json --org 795

# Use the shared registry as the authoritative organization_id source:
python3 scripts/export_leads.py --input output/districts \
  --org-registry ../Lead-Scrapper-Webpage/organizations/registry.json
```

How it behaves:

- Keeps only documents with `turf_mentioned: true`, and only leads whose turf is
  for **football / soccer / baseball-softball** (a stadium counts as
  football/soccer). Other turf (landscaping, courtyards) is dropped.
- **One lead per project:** an org's turf-hit documents are aggregated into a
  single project, so several meetings about the same initiative collapse to one
  lead rather than one-per-meeting. (Splitting a board action into per-facility
  leads — e.g. separate stadiums — needs a semantic pass and is a follow-up.)
- Joins `org_name` / `organization_id` / `state` / `county` from
  `districts/district_directory.csv` on `(platform, platform_org_id)`. Which
  platform/org a file belongs to is read from the records themselves (the
  scraper stamps every record); legacy `org_<id>.json` filenames map to
  `boardbook`.
- **Platform provenance without schema changes:** each lead's
  `evidence.details` ends with `platform=<platform> org=<org id>` (e.g.
  `platform=sparq org=120`), so Retool can filter the JSONB column per
  platform. The deep links (`source_url`, `evidence.source_urls`) point at
  the correct platform automatically because they come from the scraped
  records. No new lead fields, no new tables; `source` stays
  `"meeting-minutes"` — pipeline-level provenance is unchanged.
- Computes a deterministic `external_id` so the same project re-exported later
  produces the same id — Supabase upserts stay idempotent.
- **Ledger-first:** `leads/ledger.json` holds every lead ever exported. Each
  run appends only new ids and writes just the new leads to
  `exports/<UTC timestamp>/leads.json`. Re-running adds nothing new.
- Validates every record against the schema and refuses to write invalid ones
  (a project whose org has no enriched `state`, for example, cannot form a valid
  lead and is reported rather than written).

`leads/ledger.json` is committed; the per-run `exports/` snapshots are
gitignored (reproducible from the ledger). `source` is fixed: every lead from
this repo is `source: "meeting-minutes"`.

### Staying compatible with the web-search pipeline

Both pipelines feed one platform (Supabase, surfaced in Retool), so leads must
line up field-for-field with the web-search pipeline's ledger:

- **Schema** — `contracts/lead.schema.json` follows that repo's copy
  (`additionalProperties: false`). Since 2026-07-24 this copy differs in one
  place: `location_id` is optional here and no longer emitted (see below) —
  a coordination item until the agent repo adopts the same change.
- **`external_id`** — the **same recipe** as the web pipeline, verified
  byte-for-byte against its ledger:
  `sha1(organization | project_name | project_address)` truncated to 16 hex
  chars. `project_name` feeds the hash, so it is derived from the org + scope
  (not the meeting date) to stay stable across meetings.
- **`organization_id`** — the shared join key, **agent-leading**. It comes from
  the `organization_id` column in `districts/district_directory.csv`, which holds the
  id the web-search/agent side assigns (e.g. `leander-isd-tx`). `--org-registry`
  can override it as an authoritative source. Orgs with neither fall back to a
  locally-generated slug and are flagged `needs_review` for reconciliation. New
  organizations are defined once on the agent side; this repo reuses those ids
  rather than minting a parallel scheme.
- **`county`** — stored without the `" County"` suffix (e.g. `Williamson`) to
  match the shared convention.
- **`location_id`** — **not emitted.** Search areas are an agent-workflow
  concept; this pipeline has none, so leads carry no `location_id` (the sync
  sends the column as null). Geography resolves platform-side through
  `organization_id` → the shared `organization` / `organization_geography`
  tables, with the lead's own `state`/`county` columns covering orgs not yet
  registered there. (Older ledger records may still carry the retired
  `us-<state>-meeting-minutes` slug; it validates but is ignored.)

Note on deduplication: because `external_id` is a pure function of
`organization` + `project_name` + `project_address`, two pipelines only
auto-collide on the upsert key when all three match exactly. The meeting-minutes
side does not reproduce the agent's curated `project_name`/`project_address`, so
cross-pipeline dedup is **not** exact for now — leads should be checked against
existing rows at load time (a later, non-exact/agent step), and meeting-minutes
leads carry the meeting date in `evidence.details` to help decide new-vs-update.

## Pushing leads to Supabase

`sync/push_to_supabase.py` upserts the ledger into the shared Supabase
`lead_entry` staging table (the same table the web-search agent pipeline
writes to). One `lead_entry` row = one scrape. Resolving entries into the
`lead` table (one row per resolved opportunity, with `lead_id` / `dedup_key`)
is a separate platform job — this sync never creates or touches `lead` rows.
The organization / search_area / source / run tables are owned by the agent
repo.

```bash
python3 sync/push_to_supabase.py --dry-run   # transform + print, no network
python3 sync/push_to_supabase.py             # push (needs sync/.env, see .env.example)
```

How it behaves:

- **Idempotent:** upserts on `entry_id` (= the ledger's `external_id`, same
  value and recipe; only the column name differs); re-running only refreshes.
- **Never touches platform-owned columns.** The sync sends only its payload
  columns; PostgREST merge-duplicates updates nothing else, so these survive
  every re-sync:
  - `review_status` / `rejected_reason` / `reviewed_by` / `reviewed_at` —
    BDM-owned, managed in Retool (`review_status` defaults to `pending`);
  - `lead_id` / `observation_type` / `match_confidence` — set by the
    resolution job when it links entries to `lead` rows;
  - `lead_value_estimation` — filled by a platform-side enrichment agent
    that reads the summary/evidence for stated dollar amounts. Do not
    hand-fill pipeline-owned columns (`location_id`, `bid_due_date`) in
    Retool instead — the sync re-sends those as null.
- **`normalized_url`** is derived from `source_url` at push time (lowercase
  scheme/host, default port and fragment dropped, path case and query kept).
- **Foreign-key preflight:** `lead_entry.organization_id` references the
  shared `organization` table, which the agent side owns. A lead whose id is
  not registered there yet is pushed with `organization_id` null (and a
  warning), instead of the whole batch being rejected; the ledger keeps the
  local id, so a re-run after the org is registered fills the column in.
- Known quirk: `synced_at` is set on first insert only (database default);
  refreshing it on re-sync would need a platform-side trigger.

`.github/workflows/sync-supabase.yml` runs the same script automatically when
`leads/ledger.json` changes on `main` (requires the `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` repo secrets, the same ones the agent repo uses).

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
