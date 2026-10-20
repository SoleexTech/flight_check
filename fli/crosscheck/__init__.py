"""Cross-region price cross-checking.

This package layers three small, independent capabilities on top of the
core flight search so prices can be sanity-checked across regions and
against the airlines' own websites:

* :mod:`fli.crosscheck.regions` — region → Google/DuckDuckGo locale mapping.
* :mod:`fli.crosscheck.duckduckgo` — discover an airline's official site.
* :mod:`fli.crosscheck.airline_sites` — best-effort price extraction.
* :mod:`fli.crosscheck.compare` — orchestrate a cross-region comparison.

Proxy configuration lives one level up in :mod:`fli.proxy`.
"""

from fli.crosscheck.airline_sites import PriceSignal, extract_price_signals, fetch_price_signals
from fli.crosscheck.compare import (
    AirlineSiteCheck,
    RegionComparison,
    RegionQuote,
    compare_regions,
)
from fli.crosscheck.duckduckgo import SearchResult, find_official_sites, search
from fli.crosscheck.regions import REGIONS, Region, resolve_region

__all__ = [
    "REGIONS",
    "AirlineSiteCheck",
    "PriceSignal",
    "Region",
    "RegionComparison",
    "RegionQuote",
    "SearchResult",
    "compare_regions",
    "extract_price_signals",
    "fetch_price_signals",
    "find_official_sites",
    "resolve_region",
    "search",
]
