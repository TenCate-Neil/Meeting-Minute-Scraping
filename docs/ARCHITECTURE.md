# Architecture: How the scraper talks to BoardBook

This documents the BoardBook endpoints the scripts depend on, so a future
change to the site can be diagnosed quickly instead of re-discovered from
scratch.

## Site structure

BoardBook (`meetings.boardbook.org`) hosts board/committee meeting agendas
for ~1,700 organizations - mostly school districts, but also libraries,
colleges, and county-level bodies. Each organization has a numeric ID.

```
https://meetings.boardbook.org/Public
    -> directory of every organization, each linking to /Public/Organization/{orgId}

https://meetings.boardbook.org/Public/Organization/{orgId}
    -> full meeting history for one org (750 meetings for org 795, going
       back to 2003) - all on a single page, no pagination

https://meetings.boardbook.org/Public/Agenda/{orgId}?meeting={meetingId}
    -> HTML view of one meeting's agenda (human-facing page)

https://meetings.boardbook.org/Public/DownloadAgenda/{orgId}?meeting={meetingId}
    -> the actual PDF, served directly, no login/cookies required
```

**The important discovery:** the `/Public/...` routes are genuinely public.
There is a *different* route prefix, `/Meeting/Agenda/...`, that looks similar
but redirects into an OAuth login flow (`login.boardbook.org`) requiring
staff credentials - that path is a dead end for this project and should not
be used. Always use `/Public/DownloadAgenda/{orgId}?meeting={meetingId}` to
fetch documents.

## Parsing the meeting list

`/Public/Organization/{orgId}` renders a table where each meeting is a
`<tr class="row-for-board">`. Inside each row:

- The first `<td>`'s first `<div>` contains a string like
  `"June 18, 2026 at 6:15 PM - Regular Meeting with Public Hearing"` - this is
  split into date and title.
- A link matching `a[href*="/Public/Agenda/{orgId}"]` contains the
  `meeting=<id>` query parameter used everywhere else.

This structure was confirmed by inspecting the live HTML for org 795
(Leander ISD) - see `scripts/scrape_boardbook.py::fetch_meeting_list`.

## Parsing the organization directory

`/Public` renders every organization as `a[href*="/Public/Organization/"]`
with the org name as the link text, inside a flat `<ul class="list-unstyled">`
- no pagination, no search API needed. `scripts/fetch_org_directory.py`
scrapes this once to produce `districts/org_directory.csv`.

## Deriving state and county

BoardBook has no state/county field anywhere in the directory or org APIs.
`scripts/enrich_org_directory.py` derives both, per org:

1. Fetch `/Public/Organization/{orgId}` and take the first
   `maps.google.com/?q=<address>` link on the page - this is the physical
   meeting location BoardBook renders next to each meeting's date/time, and
   it's present on essentially any org with at least one posted meeting.
2. Parse the state straight out of that address string with a regex (either
   `", TX 79311"` or `", Texas 78613"` -style endings both appear across
   orgs) and normalize to the 2-letter USPS abbreviation.
3. Send the full address to the US Census Bureau's public one-line
   geocoder (`geocoding.geo.census.gov`, no API key required) to resolve the
   county via TIGER/Line address ranges.

Gaps are left blank, not guessed: some orgs have no posted meeting (no
address to scrape at all), and the Census geocoder's address-range coverage
has known holes in rural and tribal areas (e.g. an address in Belcourt, ND
returned zero matches on every address variant tried) - it's a data
availability gap in the Census dataset, not a bug in the parsing.

### Closing the remaining gap

After the address-scrape + geocoder pass, ~400 of 1,714 rows were still
missing a `state` and/or `county` - mostly orgs with no posted BoardBook
meeting (nothing to scrape an address from) or addresses the Census
geocoder's TIGER/Line coverage didn't resolve. Two further passes closed
most of that:

1. **NCES Common Core of Data** - the Urban Institute's Education Data
   Portal (`educationdata.urban.org/api/v1/school-districts/ccd/directory/`)
   mirrors NCES's official K-12 district directory for free, no API key,
   and includes a `county_name` field directly (no separate geocoding step
   needed). The full dataset is only ~19,700 rows across 2 pages at 10,000
   rows/page, so it's cheap to pull in full and match locally rather than
   querying per-org - the API's own name-filter query parameter doesn't
   actually filter server-side, so per-org queries would have been both
   slower and no more accurate anyway. Matching was applied in two stages:
   exact name match after normalizing common abbreviation differences
   (`ISD` vs `Independent School District`, etc.), then a fuzzy match
   **constrained to the row's already-known `state`** with a high
   similarity threshold and a required margin over the second-best
   candidate. The state constraint isn't optional - unconstrained fuzzy
   matching by name similarity alone produced at least one wrong cross-state
   match in testing (an Alaska "Petersburg School District" scored highest
   against a same-named Texas ISD, since generic suffix words like "School
   District" dominate the similarity score regardless of place).
2. **Targeted web search** for whatever NCES didn't cover - mainly colleges,
   education service centers, utility/protection districts, and a handful
   of K-12 districts whose BoardBook name doesn't match any NCES entry
   closely enough to trust automatically. Each of these was a genuine
   per-org lookup, verified individually rather than pattern-matched.

Every remaining blank row has a reason recorded in its `notes` column
(confirmed placeholder, genuine cross-state name ambiguity, no county
concept for DC, etc.) - see `docs/ROLLOUT.md` for the full breakdown.

## Downloading and parsing a document

`GET /Public/DownloadAgenda/{orgId}?meeting={meetingId}` returns
`Content-Type: application/pdf` directly (no redirect, no session cookie
needed) for meetings with a posted agenda/minutes packet. If no document was
ever posted for that meeting, the endpoint 302-redirects back to the org's
meeting-list page instead of returning a PDF - `scrape_boardbook.py` treats
any non-PDF response as `error: "download_failed"`.

Text is extracted per-page with `PyPDF2.PdfReader`. Packets range from a few
pages to 200+ (they often bundle every backup document for every agenda item
into one PDF), so page-by-page extraction (rather than whole-document) keeps
memory bounded and lets future work cite a specific page number per match.

## Turf-term matching

A single compiled regex (`TURF_PATTERN` in `scrape_boardbook.py`, mirrored in
`instructions/analysis_instructions.md`) scans the extracted text
case-insensitively. Each match records ~400 characters of surrounding
context.

Each match is then run through `classify_match()`, a keyword-based heuristic
that assigns `topic_type`, `sentiment`, and `outcome` per the categories
defined in `instructions/analysis_instructions.md`, plus a per-document
`summary`. This fills out the full output format mechanically, but it is
still substring matching, not semantic understanding - e.g. sarcasm,
negation ("no longer a concern"), or a topic keyword appearing outside its
turf context can misclassify. Treat these fields as a triage aid: fine for
sorting "which of 1,000 documents deserve a closer look" at scale, not a
substitute for reading the quoted context yourself before citing a finding
externally.

## Why no headless browser is needed

BoardBook's `/Public/...` pages are server-rendered HTML (unlike some other
civic-agenda platforms, e.g. CivicClerk, which serve a JS single-page app and
require either reverse-engineering a backend API or a real browser). A plain
HTTP GET with `requests` is sufficient for every endpoint listed above.
