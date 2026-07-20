#!/usr/bin/env python3
"""
Back-compat wrapper: the scraper is platform-neutral now and lives in
scripts/scrape_meetings.py (BoardBook stays the default platform, so every
existing `scrape_boardbook.py --org 795` invocation behaves exactly as
before). BoardBook-specific fetching moved into
scripts/platforms/boardbook_family.py.

Prefer scrape_meetings.py for new work:

    python3 scripts/scrape_meetings.py --platform boardbook --org 795 --limit 5
"""
from scrape_meetings import *  # noqa: F401,F403 - re-export the module surface
from scrape_meetings import main

if __name__ == "__main__":
    main()
