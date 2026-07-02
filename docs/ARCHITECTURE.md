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
context. This is intentionally a simple substring/regex search, not an LLM
call - it is cheap enough to run across thousands of documents and only
flags candidates; a human (or a follow-up LLM pass per `instructions/
analysis_instructions.md`) should read the quoted context to judge topic
type, sentiment, and outcome before drawing conclusions.

## Why no headless browser is needed

BoardBook's `/Public/...` pages are server-rendered HTML (unlike some other
civic-agenda platforms, e.g. CivicClerk, which serve a JS single-page app and
require either reverse-engineering a backend API or a real browser). A plain
HTTP GET with `requests` is sufficient for every endpoint listed above.
