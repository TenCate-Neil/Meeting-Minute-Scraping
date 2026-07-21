#!/usr/bin/env python3
"""
Platform adapter registry.

Adding a source platform to the pipeline means: write one PlatformAdapter
subclass (see base.py), register it in ADAPTER_FACTORIES below, and add the
districts to districts/district_directory.csv with the new platform name.
Nothing in analysis, export or sync changes per platform.
"""
from typing import List

from .base import DOCUMENT_KINDS, MeetingRef, PlatformAdapter, parse_display_date
from .boardbook_family import BoardBookFamilyAdapter
from .boarddocs import BoardDocsAdapter

# The BoardBook family: one white-labeled product on three canonical domains
# (legacy domains meetings.boardbook.org aliases meeting.assemblemeetings.com;
# prefer the canonical domain per platform).
FAMILY_BASE_URLS = {
    "boardbook": "https://meetings.boardbook.org",
    "sparq": "https://meeting.sparqdata.com",
    "boeconnect": "https://meeting.boeconnect.net",
}

ADAPTER_FACTORIES = {
    "boardbook": lambda: BoardBookFamilyAdapter("boardbook", FAMILY_BASE_URLS["boardbook"]),
    "sparq": lambda: BoardBookFamilyAdapter("sparq", FAMILY_BASE_URLS["sparq"]),
    "boeconnect": lambda: BoardBookFamilyAdapter("boeconnect", FAMILY_BASE_URLS["boeconnect"]),
    "boarddocs": lambda: BoardDocsAdapter(),
}

# Known platforms whose adapters are deliberately deferred. Directory rows may
# exist for them already; run_all_districts.py reports and skips these instead
# of failing, so the directory can be seeded ahead of adapter work.
DEFERRED_PLATFORMS = ("agendaquick", "diligent-community", "apptegy")

KNOWN_PLATFORMS = tuple(ADAPTER_FACTORIES) + DEFERRED_PLATFORMS

DEFAULT_PLATFORM = "boardbook"


def get_adapter(platform: str) -> PlatformAdapter:
    """Instantiate the adapter for a platform name (fresh instance per call -
    adapters hold a requests.Session and rate-limit state)."""
    name = (platform or "").strip().lower()
    factory = ADAPTER_FACTORIES.get(name)
    if factory is None:
        if name in DEFERRED_PLATFORMS:
            raise ValueError(
                f"platform {name!r} is known but its adapter is not implemented yet "
                f"(deferred; see docs/ARCHITECTURE.md)"
            )
        raise ValueError(
            f"unknown platform {name!r}; implemented: {', '.join(sorted(ADAPTER_FACTORIES))}"
        )
    return factory()


def implemented_platforms() -> List[str]:
    return sorted(ADAPTER_FACTORIES)


__all__ = [
    "ADAPTER_FACTORIES",
    "BoardBookFamilyAdapter",
    "BoardDocsAdapter",
    "DEFAULT_PLATFORM",
    "DEFERRED_PLATFORMS",
    "DOCUMENT_KINDS",
    "FAMILY_BASE_URLS",
    "KNOWN_PLATFORMS",
    "MeetingRef",
    "PlatformAdapter",
    "get_adapter",
    "implemented_platforms",
    "parse_display_date",
]
