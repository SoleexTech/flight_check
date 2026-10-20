"""Region → locale / search-market mapping for cross-region comparisons.

A *region* is just a short key (``nl``, ``eu``, ``us`` …) that selects:

* the Google locale params — ``hl`` (language) and ``gl`` (country) — so
  Google localizes the response the way a visitor from that region sees it;
* the DuckDuckGo market (``kl``) used when discovering region-specific
  airline sites;
* a preferred top-level domain hint (``.nl``, ``.com`` …) used to rank
  official-site candidates;
* a human-readable label for output.

Currency is deliberately *not* part of a region. A meaningful comparison
bills every region in one common currency so the numbers line up directly;
the region still drives country/language and — through the proxy pool — the
originating IP, which is what actually moves Google's prices region to
region. An unknown region key degrades gracefully (see :func:`resolve_region`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    """Locale and search-market settings for one comparison region."""

    key: str
    label: str
    language: str | None  # Google ``hl`` (BCP-47), e.g. "nl", "en-GB"
    country: str | None  # Google ``gl`` (ISO 3166-1 alpha-2), e.g. "NL"
    ddg_market: str | None  # DuckDuckGo ``kl`` market, e.g. "nl-nl"
    tld: str | None  # preferred official-site TLD hint, e.g. ".nl"


# A small, useful set of regions. The keys the user is most likely to reach
# for (nl / eu / us) are here alongside other common markets. "eu" is left
# country-agnostic on purpose: there is no single EU ``gl`` value, so we let
# the EU proxy's IP drive Google's localization and only fix the language.
REGIONS: dict[str, Region] = {
    "nl": Region("nl", "Netherlands", "nl", "NL", "nl-nl", ".nl"),
    "eu": Region("eu", "Europe", "en", None, None, None),
    "us": Region("us", "United States", "en", "US", "us-en", ".com"),
    "gb": Region("gb", "United Kingdom", "en-GB", "GB", "uk-en", ".co.uk"),
    "uk": Region("uk", "United Kingdom", "en-GB", "GB", "uk-en", ".co.uk"),
    "de": Region("de", "Germany", "de", "DE", "de-de", ".de"),
    "fr": Region("fr", "France", "fr", "FR", "fr-fr", ".fr"),
    "es": Region("es", "Spain", "es", "ES", "es-es", ".es"),
    "it": Region("it", "Italy", "it", "IT", "it-it", ".it"),
    "be": Region("be", "Belgium", "nl", "BE", "be-nl", ".be"),
    "ca": Region("ca", "Canada", "en", "CA", "ca-en", ".ca"),
    "au": Region("au", "Australia", "en", "AU", "au-en", ".com.au"),
    "in": Region("in", "India", "en", "IN", "in-en", ".in"),
    "jp": Region("jp", "Japan", "ja", "JP", "jp-jp", ".jp"),
    "br": Region("br", "Brazil", "pt-BR", "BR", "br-pt", ".com.br"),
}


def resolve_region(key: str) -> Region:
    """Return the :class:`Region` for ``key``, degrading gracefully.

    Known keys map to their full locale settings. An unknown two-letter key
    is treated as an ISO country code (used for ``gl``) so callers can pass
    arbitrary country codes without a table entry; anything else yields a
    bare region with no locale overrides (the proxy IP then drives Google's
    localization).
    """
    normalized = key.strip().lower()
    if normalized in REGIONS:
        return REGIONS[normalized]
    country = normalized.upper() if len(normalized) == 2 and normalized.isalpha() else None
    return Region(
        key=normalized,
        label=key.upper(),
        language=None,
        country=country,
        ddg_market=None,
        tld=None,
    )
