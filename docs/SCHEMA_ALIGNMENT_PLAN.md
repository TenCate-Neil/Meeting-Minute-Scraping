# Plan: aligning meeting-scraper output with the agent lead schema (Supabase)

Status: **planning only — no pipeline changes made.**

## The challenge

Both pipelines — the web-search lead agent (`Lead-Scrapper-Webpage`) and this
meeting-minutes scraper — write into the same Supabase tables (created by the
agent repo's `sql/schema.sql`; Retool sits on top). The agent's lead structure
is leading. This document records what was compared, what already lines up,
and what still has to change on this side.

Everything below was verified directly against the agent repo (provided
2026-07-20): its `contracts/lead.schema.json`, `sql/schema.sql`,
`sync/push_to_supabase.py`, `docs/SCHEMAS.md`, and its live
`leads/ledger.json` (11 leads).

## What already lines up (verified, no change needed)

- **Schema shape.** `contracts/lead.schema.json` here is structurally
  identical to the agent's copy — same properties, same types, same nested
  `evidence` block, `additionalProperties: false` — with two exceptions
  covered below (`bid_due_date`; missing description strings, cosmetic only).
- **`external_id` recipe.** This repo computes
  `sha1(organization | project_name | project_address)[:16]`. Recomputed
  against all 11 leads in the agent's ledger: **11/11 match.** (The agent's
  `docs/SCHEMAS.md` prose says the third hash input is `source_url`; its own
  data says otherwise. That is documentation drift on the agent side — worth
  reporting, but no change here.)
- **Supabase row shape.** The `lead` table stores the flat core as columns
  and the whole `evidence` block verbatim in one JSONB column
  (`sql/schema.sql`, `rows_lead()` in the sync script). This repo's export
  output maps onto that with no transformation beyond what the sync script
  already does. Lifecycle columns (`status`, `rejected_reason`,
  `assigned_bdm`) are Supabase-only and never written by any pipeline.
- **Ledger wrapper format.** Both repos use
  `{"schema_version": "2.0", "leads": [...]}` keyed on `external_id`;
  upsert on `external_id` is the shared idempotency contract.
- **Conventions.** `source: "meeting-minutes"` (in the schema enum and the
  table CHECK constraint), county without the `" County"` suffix,
  `location_id` slug pattern — all already implemented in
  `scripts/export_leads.py` and covered by `tests/test_export_leads.py`.

## Gaps and required changes

### 1. `bid_due_date` — contract divergence, agent schema is self-contradictory

- The agent's schema lists `bid_due_date` in `required` but does **not**
  define it under `properties`, while `additionalProperties: false`. As
  written, no lead can satisfy it: omitting the field fails `required`,
  including it fails `additionalProperties`. The agent's own 11 ledger leads
  omit it, and its `docs/SCHEMAS.md` field table does not mention it. The
  Supabase column exists and is nullable (`bid_due_date date`); the sync
  sends `null`.
- This repo's copy simply dropped it from `required`, so the copies diverge.
- Required change: **coordinate a fix with the agent side** (likely: define
  `bid_due_date` as an optional string property and remove it from
  `required`), then take their corrected schema verbatim into `contracts/`.
  Meeting-minutes leads would emit `bid_due_date` only if a bid deadline is
  ever extracted from an agenda; until then the column stays `null`.
- Do not adopt the required-field version: it would make every lead from
  both pipelines invalid.

### 2. `organization_id` — foreign key will reject unreconciled ids (highest risk)

- In Supabase, `lead.organization_id` is a **foreign key** to
  `organization(organization_id)`. An insert with an id that is not in the
  `organization` table fails outright.
- The agent's organization registry holds **2** organizations. This repo's
  directory holds 1,714 orgs, of which **1** carries the shared
  `organization_id` (Leander ISD). The exporter's current fallback — a
  locally generated slug — would produce leads that Supabase **rejects**.
- Required changes:
  1. `export_leads.py`: when an org is not in the shared registry, emit the
     lead **without** `organization_id` (it is optional in the schema and
     nullable in the table) instead of the slug fallback; keep the
     best-effort slug in `evidence.details` and keep `needs_review: true`.
  2. A reconciliation step that registers this repo's rollout districts in
     the shared `organizations/registry.json` (agent-leading; the org
     contract needs `organization_id`, `name`, `type`, `state`,
     `primary_county` — all derivable from `districts/org_directory.csv`),
     then back-fills the `organization_id` column here. Organizations must
     sync to Supabase **before** leads that reference them (the agent's sync
     already orders tables this way).
  3. Make `--org-registry` the default input for `run_all_districts.py
     --export-leads` once a shared registry export is available.

### 3. Delivery into Supabase — no sync path exists for this repo's ledger

**Status: implemented** (option a). `sync/push_to_supabase.py` +
`.github/workflows/sync-supabase.yml`, lead table only, upsert on
`external_id`, lifecycle columns never sent. **Updated 2026-07-24:** after the
platform's staging restructure the sync targets `lead_entry` on `entry_id`
(= the same external_id value); review/resolution/enrichment columns
(`review_status`, `lead_id`, `observation_type`, `match_confidence`,
`lead_value_estimation`) are never sent — see README "Pushing leads to
Supabase". It also implements gap 2's
FK safety at push time: organization_ids not yet registered in the shared
organization table are sent as null with a warning, so unreconciled leads
load instead of being rejected. Verified against the live project: push,
idempotent re-run, and the lead row visible with `source=meeting-minutes`
and `status=New`.

- Original analysis: the agent repo pushes via `sync/push_to_supabase.py` +
  a GitHub Actions workflow, but both read only **that repo's**
  `leads/ledger.json`.
- Options considered:
  - **(a) Per-repo sync (chosen):** trimmed sync (lead table only) plus a
    matching workflow in this repo, using the same Supabase secrets.
    Decoupled, idempotent, smallest blast radius.
  - (b) Extend the agent's sync to read both ledgers — couples the repos.
  - (c) Merge this ledger into the agent's ledger — single ingestion point,
    but their write-validation hook currently enforces the contradictory
    schema (gap 1), and cross-repo merges add churn.

### 4. `location_id` — valid but dangling

> **Superseded (2026-07-24):** decided with the platform side — this pipeline
> emits **no** `location_id` (the sync sends the column as null). Geography
> resolves via `organization_id` → `organization` / `organization_geography`;
> the entry's own `state`/`county` columns cover unregistered orgs. The
> county-level search-area idea below was not implemented.

- This repo derives `us-<state>-meeting-minutes`. The column has no FK, so
  inserts succeed, but the id does not exist in `search_area`, so any Retool
  filter or join on search areas will not resolve meeting-minutes leads.
- Required decision with the agent side: either register per-state
  meeting-minutes pseudo search-areas in `locations/registry.yaml` (they
  sync to `search_area`), or agree the dangling id is acceptable because
  `location_id` is search *input* and meeting-minutes leads are anchored by
  organization instead.

### 5. Lead granularity — one-per-org here, one-per-facility there

- Confirmed concretely: the same Leander project exists in both ledgers as
  different rows — the agent has per-facility leads (e.g. "Bible Stadium -
  turf replacement, football/soccer field", with address and start date);
  this repo has one aggregated lead ("Leander ISD - artificial turf
  replacement", address empty). Different `project_name`/`project_address`
  → different `external_id` → two Supabase rows.
- Required change (before any bulk historical load, since splitting changes
  `external_id`s): a semantic pass over the matched minutes text to split a
  board action into per-facility projects. Needs either an LLM extraction
  step or a curated facility list; the current one-project-per-org exporter
  is the documented interim.
- Trade-off to settle first: the 3-month trial is designed to **compare**
  the two pipelines (the `source` column and `lead_status_log` exist for
  exactly that), so both pipelines finding the same project may be a
  desired trial datapoint, not a bug. The agent repo's planned-but-unbuilt
  `staging_lead` diff layer is where non-exact cross-pipeline dedup belongs.
  Decide the trial policy before building dedup.

### 6. Evidence fields this scraper cannot fill

- `evidence.project_address`, `contact.*`, `projected_start_date` stay `""`
  (minutes rarely state them; nothing is fabricated — leads carry
  `needs_review: true`). `evidence.source_ids` (`SRC-###`) are keys into the
  agent's source registry and stay omitted here; `evidence.confidence` is an
  agent extraction concept and stays omitted. All are optional in the schema
  and harmless in the JSONB column.
- Optional follow-up, not blocking: an enrichment step for facility
  addresses, which would also improve cross-pipeline `external_id`
  convergence (gap 5).

## Follow-up decisions (agreed in planning, 2026-07-20)

### Multi-source platforms: provenance without schema changes (implemented)

The scraper now reads several source platforms (BoardBook / Sparq /
BOEconnect / BoardDocs; see docs/ARCHITECTURE.md). None of this touches the
shared contract:

- `contracts/lead.schema.json` is byte-identical; `source` stays
  `"meeting-minutes"` for every lead from this repo (pipeline-level
  provenance, as before); the `external_id` recipe is untouched - which is
  also what makes dual-hosted districts (Kingsport on BOEconnect *and*
  BoardDocs) collapse to one lead instead of two.
- Platform-level provenance is carried in existing structures:
  per-document output records hold `platform` / `platform_org_id` /
  document URLs (repo-internal, no contract), and each lead's
  `evidence.details` ends with `platform=<p> org=<id>` - queryable in
  Retool via the JSONB column (`evidence->>'details' LIKE '%platform=sparq%'`).
- **No new Supabase table and no new lead column were added, on purpose.**
  If per-platform reporting becomes a first-class Retool need, the right
  moves are (in order): propose an optional `evidence.platform` property to
  the agent side (schema addition on their side, adopted verbatim here), or
  register each platform as a row in the agent-owned `source` registry
  table (`SRC-###`) and reference it via `evidence.source_ids` - a data
  change, not a schema change, coordinated with the agent repo. A brand-new
  table remains unnecessary.
- `location_id` derivation and the org-registration path (gaps 2/4 above)
  are unchanged; new-platform districts flow through the same
  `districts/district_directory.csv` -> registry reconciliation as
  BoardBook districts. The directory's `organization_id` column stays the
  shared join key across platforms - one district, one organization_id, any
  number of platform rows.

### Organization geography: multi-county and city-based districts

Use the agent's organization contract as-is instead of restructuring the
directory: `primary_county` stays the single required county; many-to-many
coverage goes in the org's `geography[]` array (`{kind: county|place,
value}`), which syncs to the `organization_geography` join table.
Directory changes: keep `county` (= primary), add an optional delimited
`counties` column and a `place` column; the org-registration step maps them
to `geography[]`. City-anchored districts get `kind: place` rows, with
`primary_county` still filled (independent cities are their own
county-equivalent). BDM county-level filtering runs on
`organization_geography`, which shows a spanning district under all its
counties.

### location_id naming: county-level, not state-level

> **Superseded (2026-07-24):** see the note under gap 4 — no `location_id` is
> emitted at all; search areas stay an agent-workflow concept.

BDM territories are county-scale, so leads derive
`us-<state>-<primary-county-slug>-meeting-minutes`
(e.g. `us-tx-williamson-meeting-minutes`) instead of the state-level slug in
gap 4. Each such id is registered as a `search_area` row (`type: county`),
generated from the directory for counties that have rollout districts.
Known trade-off: a multi-county district's leads carry only the primary
county's location_id; full territory mapping comes from
`organization_geography`, with location_id as the coarse filter and
provenance marker.

### Scrape logging: document-level state + shared run tables

Two layers. Layer 1 is **implemented** (`scripts/scrape_state.py` +
`state/scrape_state.json`; see README "Incremental re-runs"). Layer 2 —
run-level visibility in the shared `run` / `location_state` tables — stays
planned; double-scrape prevention itself is entirely layer 1 and concerns
only this repo (the agent pipeline keeps its own re-run bookkeeping).

1. **Document-level (new in this repo):** a tracked `scrape_state.json`
   keyed by `(org_id, meeting_id)` with `scraped_at`, agenda-processed and
   minutes-captured flags. Skip a meeting only when the agenda was processed
   AND minutes were captured (or none are expected); a turf-hit meeting
   without minutes is rechecked on later runs, because `minutes_outcome`
   only exists once minutes are posted.
2. **Run-level (reuse agent infrastructure):** write a `run_manifest.json`
   per scrape (stage `scrape`, key `location_id + run_timestamp`) and
   maintain `location_state` rows (`last_scraped`, `next_due`) for the
   meeting-minutes search areas, so scrape recency is visible in the same
   Supabase tables (`run`, `location_state`) both pipelines already share.

## Suggested order of implementation

1. **Gap 1** — agree the corrected lead schema with the agent side and take
   it verbatim (cheap; unblocks validation everywhere).
2. **Gap 2, change 1** — stop emitting slug `organization_id`s (small code
   change; removes the FK failure mode immediately).
3. **Gap 3** — add the per-repo sync + workflow (leads become actually
   loadable; Leander lead is the end-to-end test).
4. **Gap 2, changes 2–3** — bulk org registration + directory back-fill
   (data work, no code risk; unblocks org-anchored filtering in Retool).
5. **Gap 4** — settle the `location_id` convention (one decision, tiny
   change either side).
6. **Gaps 5–6** — per-facility splitting and enrichment, only after the
   trial's duplicate policy is decided with the agent side.
