# Architecture: How the scraper talks to the source platforms

This documents the endpoints the scripts depend on, per platform, so a
future change to a site can be diagnosed quickly instead of re-discovered
from scratch.

## The platform adapter layer

School boards publish meeting documents on a handful of central platforms.
Fetching is the ONLY platform-specific part of this pipeline; everything
after it - text extraction, turf-term matching, the minutes-outcome pass,
scrape state, lead export, Supabase sync - is shared. The split lives in
`scripts/platforms/`:

```
PlatformAdapter (scripts/platforms/base.py)
    list_meetings(org_ref)                 -> [MeetingRef: id, date, name]
    fetch_document(org_ref, meeting, kind) -> bytes | None   (kind: agenda|minutes)
    document_page_url / org_page_url       -> human-facing deep links
```

Adding a platform = one adapter subclass + a registry entry in
`scripts/platforms/__init__.py` + directory rows. Nothing in
analysis/export/sync changes.

| platform | adapter | org ref | documents |
|---|---|---|---|
| `boardbook` (meetings.boardbook.org) | `BoardBookFamilyAdapter` | numeric id or slug (`795`, `kcs`) | PDF |
| `sparq` (meeting.sparqdata.com, NE) | same, different base URL | numeric id or slug | PDF |
| `boeconnect` (meeting.boeconnect.net, TN) | same, different base URL | numeric id or slug | PDF |
| `boarddocs` (go.boarddocs.com) | `BoardDocsAdapter` | `{state}/{slug}` (`ny/albany`) | HTML |
| `agendaquick`, `diligent-community`, `apptegy` | **deferred** (directory rows may exist; the rollout reports and skips them) | | |

Documents are content-sniffed by the shared extractor
(`scrape_meetings.extract_text`): `%PDF` magic -> PyPDF2 per page, markup ->
one HTML text pass. Meeting ids are only unique within a platform, so all
scrape-state keys are namespaced `{platform}:{org_id}`
(`scripts/scrape_state.py`; legacy bare-id state files migrate in place on
load).

## The BoardBook family (boardbook / sparq / boeconnect)

BoardBook Premier, Sparq Meetings (NASB's eMeeting for Nebraska - ~70% of NE
districts) and BOEconnect (owned by TSBA; 40 Tennessee systems) are the same
white-labeled product on three domains, verified live 2026-07-20: identical
routes, identical HTML, one adapter parameterized by base URL. (Legacy
domain `meeting.assemblemeetings.com` serves the same app as
`meeting.sparqdata.com`; the canonical domain per platform is preferred.)
Everything below applies to all three; examples use BoardBook.

Two family-wide quirks the adapter handles:

- **Sparq spells the PDF content type `application/PDF`** (uppercase), so
  the content-type check is case-insensitive.
- **A meeting row may expose `Agenda`, `Minutes` and/or `PublicNotice` links
  in any combination.** Some orgs post only public notices (e.g.
  Papillion-La Vista on Sparq) or minutes without agendas. Discovery accepts
  any of the three link kinds; a meeting without an agenda PDF then follows
  the normal `download_failed`/retry path rather than being invisible.

## Site structure

BoardBook (`meetings.boardbook.org`) hosts board/committee meeting agendas
for ~1,700 organizations - mostly school districts, but also libraries,
colleges, and county-level bodies. Each organization has an ID - numeric on
BoardBook itself, but numeric OR slug (`kcs`, `papillion-lavista`) across
the family, so org ids are treated as opaque strings everywhere.

```
https://meetings.boardbook.org/Public
    -> directory of every organization, each linking to /Public/Organization/{orgId}

https://meetings.boardbook.org/Public/Organization/{orgId}
    -> full meeting history for one org (750 meetings for org 795, going
       back to 2003) - all on a single page, no pagination

https://meetings.boardbook.org/Public/Agenda/{orgId}?meeting={meetingId}
    -> HTML view of one meeting's agenda (human-facing page)

https://meetings.boardbook.org/Public/DownloadAgenda/{orgId}?meeting={meetingId}
    -> the agenda PDF, served directly, no login/cookies required

https://meetings.boardbook.org/Public/Minutes/{orgId}?meeting={meetingId}
    -> HTML view of one meeting's minutes (only for meetings with minutes posted)

https://meetings.boardbook.org/Public/DownloadMinutes/{orgId}?meeting={meetingId}
    -> the minutes PDF, same access model as DownloadAgenda
```

**Agenda vs minutes.** Every meeting has an agenda; only some have minutes
(on org 795, 756 meetings expose an `Agenda` link and 405 also expose a
`Minutes` link). The agenda is published *before* the meeting and bundles all
backup documents (so it is usually the *larger* PDF - e.g. 22 MB agenda vs
20 MB minutes for meeting 723526); the minutes are published *after* and
record what was actually decided (motions, votes, outcomes). This pipeline
uses the **agenda as the primary source for discovery** (earlier, 100%
coverage, richest detail) and consults the **minutes only to confirm the
outcome of a turf hit** - see "Confirming outcomes from the minutes" below.

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
(Leander ISD), and re-confirmed on Sparq org 120 (Omaha) and BOEconnect org
`kcs` (Kingsport) - see
`scripts/platforms/boardbook_family.py::BoardBookFamilyAdapter._parse_meeting_rows`.

## Parsing the organization directory

`/Public` renders every organization as `a[href*="/Public/Organization/"]`
with the org name as the link text, inside a flat `<ul class="list-unstyled">`
- no pagination, no search API needed, same markup on all three family
domains (302 orgs on Sparq, 47 on BOEconnect). `scripts/fetch_org_directory.py`
merges these into `districts/district_directory.csv`; see docs/ROLLOUT.md.

One BoardBook-specific gap: at least 36 Kansas districts are live on
BoardBook but **absent from the public directory** (org page responds, no
directory link). Their rows come from `districts/seeds/` instead, and
`scripts/probe_boardbook_orgs.py` can sweep the org-id space to find such
hidden orgs by page title.

## Deriving state and county

No platform exposes a state/county field in its directory or org APIs.
`scripts/enrich_org_directory.py` derives both, per org (the process below
was built on BoardBook and now covers the whole family the same way; for
BoardDocs the address AND the display name come from the client site's
header - `#SiteTitle1` / `#SiteTitle2` on the public page - since BoardDocs
has no directory to take names from):

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

The two follow-up passes below were run on the original BoardBook-only
directory (1,714 rows at the time); they are described here so they can be
repeated. BoardDocs rows have since had the pass-1 enrichment (and get
`state` from their org-ref prefix), but the Sparq and BOEconnect rows are
still largely un-enriched - current per-platform coverage numbers live in
docs/ROLLOUT.md step 1, not here.

After the address-scrape + geocoder pass, ~400 of 1,714 BoardBook rows were
still missing a `state` and/or `county` - mostly orgs with no posted
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
meeting-list page instead of returning a PDF - the adapter returns None and
`scrape_meetings.py` records `error: "download_failed"`.

Text is extracted per-page with `PyPDF2.PdfReader`. Packets range from a few
pages to 200+ (they often bundle every backup document for every agenda item
into one PDF), so page-by-page extraction (rather than whole-document) keeps
memory bounded and lets future work cite a specific page number per match.

## Turf-term matching

A single compiled regex (`TURF_PATTERN` in `scrape_meetings.py`, mirrored in
`instructions/analysis_instructions.md`) scans the extracted text
case-insensitively - the same pass regardless of which platform produced the
document. Each match records ~400 characters of surrounding context.

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

## Confirming outcomes from the minutes

The agenda tells you what was *proposed*; it cannot tell you what the board
*decided*, because it is published before the meeting. The per-match `outcome`
above is therefore a guess inferred from agenda wording (a "sample motion", a
recommendation), and it is often wrong or simply "Informational only".

To firm this up without doubling the crawl, `process_meeting()` runs a
**hybrid minutes pass**: only when a meeting is a turf hit, and only if that
meeting has a minutes PDF, it also downloads
`/Public/DownloadMinutes/{orgId}?meeting={meetingId}`, finds the turf mentions
in the minutes, classifies their outcomes, and records the single most
decisive one (`Approved` > `Denied` > `Tabled` > `Motion made` >
`Informational only`) as `minutes_outcome`, with a quoted `minutes_context`.

This is cheap because turf hits are rare: across a full rollout only a handful
of meetings trigger the extra download, so there is no second full pass over
every agenda. Meetings with no posted minutes get `minutes_available: false`
and no confirmed outcome; the flag distinguishes "no minutes existed" from
"minutes existed but the turf item wasn't found in them" (`minutes_available:
true`, `minutes_outcome: null`). Pass `--skip-minutes` to turn the pass off.

**Known limitation - `minutes_outcome` is a hint, not a verified vote.** It is
still keyword matching. It uses a wider window than the agenda (±500 chars vs
±200) because minutes open with a table-of-contents block and record the vote
farther from the turf term - a tight window lands in the TOC and returns
"Informational only" even when the item passed. But a wider window also means
the matched keyword can come from background narrative rather than the roll
call: on the Leander turf item it correctly returns "Approved", but the
triggering phrase is "the Bond Oversight Committee unanimously approved [its
recommendation]", not the board's own recorded vote (which these minutes phrase
without a nearby "motion carried"). So `minutes_outcome` narrows the answer and
`minutes_context` quotes the decision area for a human to read; a definitive
label would need a semantic pass (an LLM read of `minutes_context`), which is
better suited to the agent-side pipeline than to this keyword scraper.

Why keep the agenda as primary rather than switch to minutes: agendas exist
for every meeting and appear earlier (better for surfacing a lead before the
vote), and they carry the detailed backup material where the turf scope and
dollar figures actually live. The minutes are the authority on the *decision*,
not on the *detail* - so the two are complementary, and this pipeline uses
each for what it is best at.

## BoardDocs (go.boarddocs.com)

BoardDocs (Diligent) is a different architecture: a JS single-page app over
a Domino backend, one site per client at `go.boarddocs.com/{state}/{slug}`.
There is **no public client directory** - district rows come from
research-validated slug lists in `districts/seeds/boarddocs.csv` (NY, OH,
KS, NC, TN). Two machine-readable endpoints per client, verified live
(2026-07-20) on `ny/albany` and `oh/plsd`:

```
GET  /{state}/{slug}/Board.nsf/XML-ActiveMeetings
     -> XML list of every public meeting (full history: 334 meetings back to
        2015 for ny/albany, 378 for oh/plsd), with ids, dates, names, and
        inline agenda item names.

POST /{state}/{slug}/Board.nsf/BD-GetMeetingsList?open
     form: current_committee_id={id}
     -> JSON meeting list per committee (fallback when the XML route fails).
        Committee ids appear in <option value="..."> tags in the public page
        HTML (/{state}/{slug}/Board.nsf/Public).
```

Documents are fetched per meeting, as HTML (found by reading the public
app's own JavaScript - meetings.js/agenda.js/main.js):

```
POST /{state}/{slug}/Board.nsf/PRINT-AgendaDetailed?open   form: id={meetingId}
     -> the detailed agenda (item names + full item body text) as HTML
POST /{state}/{slug}/Board.nsf/BD-GetMinutes?open          form: id={meetingId}
     -> the approved minutes as HTML
```

An empty 200 body means "not posted" (verified for future meetings and for
bogus ids) - the adapter maps it to None, which the shared pipeline already
treats as the document-not-posted case. Item-attachment PDFs also exist
(`/{state}/{slug}/Board.nsf/files/{fileId}/$file/{name}.pdf`, public, no
auth) but are not needed: the detailed agenda HTML carries the item text.

Three BoardDocs facts the adapter encodes:

- **The XML feed is not well-formed.** Some meetings close a `<category>`
  that was never opened; a strict XML parse dies at the first mismatch and
  lxml's recover mode silently dropped 330 of 334 meetings in testing. The
  adapter extracts `<meeting>...</meeting>` blocks with a regex instead
  (100% of meetings recovered on both test orgs).
- **A browser User-Agent is required.** The default python-requests UA gets
  HTTP 403 from the WAF; a normal Chrome UA string is sent, plus a
  conservative 1s minimum interval between requests.
- **Meeting ids are Domino document ids** (`DUGMQR5C6405`), not numbers -
  one more reason ids are opaque strings namespaced by platform.

## Why no headless browser is needed

BoardBook-family `/Public/...` pages are server-rendered HTML, and although
BoardDocs serves a JS single-page app, its data endpoints above return
XML/JSON/HTML directly (unlike some other civic-agenda platforms, e.g.
CivicClerk, which require either reverse-engineering a backend API or a real
browser). A plain HTTP GET/POST with `requests` is sufficient for every
endpoint listed above.
