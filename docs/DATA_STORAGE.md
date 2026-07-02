# Data storage policy

## Default: don't keep the PDFs

`scrape_boardbook.py` downloads each meeting's PDF into memory, extracts
text, searches it, and lets the bytes go out of scope. Nothing is written to
disk unless `--keep-pdfs` is passed. `run_all_districts.py` inherits the same
default.

Rationale:

- **Volume.** Agenda packets are large (11-60MB observed for a single
  Leander ISD meeting, often 100+ pages, because districts bundle every
  backup document for every agenda item into one PDF) and districts post
  dozens of meetings a year. Storing every packet for ~1,480 districts back
  to 2023+ would run into tens or hundreds of gigabytes for no analytical
  benefit once the text has been searched.
- **BoardBook is the durable copy.** These are public records the district
  is obligated to keep posted; re-downloading on demand is cheap and
  guarantees the current, authoritative version rather than a stale local
  copy that could drift from a corrected/amended posting.
- **The structured output already captures what matters.** `output/*.json`
  preserves the matched term, ~400 characters of surrounding quoted context,
  meeting date/title/ID, and page count - enough to cite a finding and to
  re-fetch the exact source PDF later (`/Public/DownloadAgenda/{orgId}?
  meeting={meetingId}`) if someone wants to verify it against the original
  formatting/pagination.

## Exception: keep PDFs where a turf mention was found

Pass `--keep-pdfs` when you want an audit trail for confirmed hits
specifically - not for the whole corpus. `run_all_districts.py --keep-pdfs`
saves PDFs per district under `output/districts/pdfs/{orgId}/`, but this
should be used selectively (e.g. re-run `scrape_boardbook.py --org <id>
--meeting-id <id> --keep-pdfs` for the handful of meetings that already
showed a match) rather than as the default rollout mode, to avoid the volume
problem above.

## What goes in `output/`

`output/` is gitignored - it is generated data, not source. Nothing under it
should be committed. If a specific finding needs to be preserved long-term
(e.g. to support a claim made externally), copy the relevant JSON record and
PDF out of `output/` into wherever your evidence/citation workflow lives -
don't rely on the scraper's working directory as permanent storage.

## Retention of the district directory CSV

`districts/org_directory.csv`, by contrast, **is** meant to be committed. It
is small (a few hundred KB for ~1,700 rows), it's the human-curated input to
the rollout (see `docs/ROLLOUT.md` step 2), and losing the curation work
(which orgs were manually included/excluded and why) would be more costly
than the storage it takes up.
