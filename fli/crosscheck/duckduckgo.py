"""DuckDuckGo source discovery — find an airline's official site per region.

The goal is to *cross-check* fares against a "valid source": the airline's
own website, for the right region (``klm.nl`` vs ``klm.com``). Rather than
pull in a third-party DuckDuckGo SDK, we query DuckDuckGo's lightweight HTML
endpoint through the same impersonating, rate-limited
:class:`~fli.search.client.Client` the rest of the project already uses — so
region proxies and the global request budget apply for free, and there is
no extra dependency to maintain.

This is intentionally a *discovery* helper: it ranks likely-official URLs
and filters out the usual aggregators (Skyscanner, Kayak, …). It does not
itself read prices — that is :mod:`fli.crosscheck.airline_sites`.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass

from fli.crosscheck.regions import Region, resolve_region
from fli.search.client import Client, get_client

# DuckDuckGo's no-JavaScript HTML endpoint. It returns plain server-rendered
# markup, which is cheap and reliable to parse without a headless browser.
HTML_ENDPOINT = "https://html.duckduckgo.com/html/"

# Result anchors and snippets in the HTML endpoint's markup carry stable
# ``result__a`` / ``result__snippet`` classes.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")

# Hosts that are never "the airline's official site" — travel aggregators,
# OTAs, metasearch, encyclopaedias and social networks. Matched as
# substrings of the result host, so "skyscanner" catches every TLD variant.
_AGGREGATORS = frozenset(
    {
        "google.",
        "bing.",
        "duckduckgo.",
        "yahoo.",
        "wikipedia.",
        "wikimedia.",
        "facebook.",
        "instagram.",
        "twitter.",
        "x.com",
        "linkedin.",
        "youtube.",
        "reddit.",
        "quora.",
        "tripadvisor.",
        "skyscanner.",
        "kayak.",
        "expedia.",
        "booking.",
        "momondo.",
        "kiwi.com",
        "opodo.",
        "edreams.",
        "trip.com",
        "ctrip.",
        "cheapflights.",
        "flightaware.",
        "wego.",
        "hopper.",
        "secretflying.",
        "google.com/travel",
    }
)

# Generic words dropped when turning an airline name into match tokens, so
# "British Airways" matches britishairways.com via the token "british".
_GENERIC_WORDS = frozenset(
    {
        "air",
        "airline",
        "airlines",
        "airways",
        "aviation",
        "co",
        "company",
        "express",
        "fly",
        "flights",
        "group",
        "international",
        "limited",
        "lines",
        "ltd",
        "royal",
        "the",
    }
)


@dataclass(frozen=True)
class SearchResult:
    """One DuckDuckGo result: a resolved URL with its title and snippet."""

    title: str
    url: str
    snippet: str = ""


def search(
    query: str,
    *,
    region: str | None = None,
    proxy: str | None = None,
    client: Client | None = None,
    max_results: int = 10,
) -> list[SearchResult]:
    """Run a DuckDuckGo search and return the top results.

    Args:
        query: The search query.
        region: Optional region key; sets DuckDuckGo's ``kl`` market so
            results are biased to that locale (e.g. ``nl`` → ``nl-nl``).
        proxy: Optional proxy URL to route the request through.
        client: HTTP client to use; defaults to the shared singleton. Mainly
            an injection point for tests.
        max_results: Maximum number of results to return.

    Returns:
        A list of :class:`SearchResult`, de-duplicated by URL.

    """
    http = client or get_client()
    data = {"q": query, "kp": "-2"}  # kp=-2 disables safe-search re-ranking.
    market = _ddg_market(region)
    if market:
        data["kl"] = market

    response = http.post(
        HTML_ENDPOINT,
        data=data,
        impersonate="chrome",
        proxy=proxy,
        allow_redirects=True,
    )
    return _parse_results(response.text, max_results)


def find_official_sites(
    airline_name: str,
    *,
    region: str | None = None,
    proxy: str | None = None,
    client: Client | None = None,
    max_results: int = 5,
) -> list[str]:
    """Return likely official-website URLs for ``airline_name``, best first.

    Results are filtered against :data:`_AGGREGATORS` and ranked by how well
    the host matches the airline name and the region's preferred TLD. When no
    candidate matches the name, the top non-aggregator results are returned
    as a fallback so the caller still has something to cross-check.
    """
    if not airline_name or not airline_name.strip():
        return []

    region_obj = resolve_region(region) if region else None
    results = search(
        f"{airline_name} official website",
        region=region,
        proxy=proxy,
        client=client,
        max_results=max(max_results * 3, 10),
    )

    tokens = _name_tokens(airline_name)
    scored: list[tuple[int, str]] = []
    fallback: list[str] = []
    seen_hosts: set[str] = set()

    for result in results:
        host = _host(result.url)
        if not host or _is_aggregator(host) or host in seen_hosts:
            continue
        seen_hosts.add(host)
        score = _score_host(host, result.url, tokens, region_obj)
        if score > 0:
            scored.append((score, result.url))
        else:
            fallback.append(result.url)

    scored.sort(key=lambda item: item[0], reverse=True)
    ranked = [url for _, url in scored]
    if not ranked:
        ranked = fallback
    return ranked[:max_results]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_results(markup: str, max_results: int) -> list[SearchResult]:
    """Extract result anchors (and snippets) from DuckDuckGo HTML markup."""
    snippets = [_clean_text(s) for s in _SNIPPET_RE.findall(markup)]
    results: list[SearchResult] = []
    seen: set[str] = set()
    for index, (href, inner) in enumerate(_RESULT_RE.findall(markup)):
        url = _resolve_href(href)
        if not url or url in seen:
            continue
        seen.add(url)
        snippet = snippets[index] if index < len(snippets) else ""
        results.append(SearchResult(title=_clean_text(inner), url=url, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


def _resolve_href(href: str) -> str:
    """Resolve a DuckDuckGo result href to its real destination URL.

    The HTML endpoint wraps external links in a ``/l/?uddg=<encoded>``
    redirect; we unwrap that to the underlying target. Scheme-relative
    ``//host`` hrefs are promoted to ``https://``.
    """
    href = html.unescape(href.strip())
    if "uddg=" in href:
        query = urllib.parse.urlsplit(href).query
        target = urllib.parse.parse_qs(query).get("uddg", [])
        if target:
            return target[0]
    if href.startswith("//"):
        return f"https:{href}"
    return href


def _clean_text(markup: str) -> str:
    """Strip HTML tags and unescape entities into plain whitespace-trimmed text."""
    return html.unescape(_TAG_RE.sub("", markup)).strip()


def _host(url: str) -> str:
    """Return the lower-cased hostname of ``url`` without a leading ``www.``."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _is_aggregator(host: str) -> bool:
    """Return ``True`` when ``host`` belongs to a known non-official source."""
    return any(marker in host for marker in _AGGREGATORS)


def _name_tokens(airline_name: str) -> list[str]:
    """Reduce an airline name to lower-case match tokens (drops generic words)."""
    raw = re.split(r"[^a-z0-9]+", airline_name.lower())
    return [tok for tok in raw if len(tok) >= 3 and tok not in _GENERIC_WORDS]


def _score_host(host: str, url: str, tokens: list[str], region: Region | None) -> int:
    """Score a candidate host: name match dominates, TLD and HTTPS break ties."""
    score = 0
    if any(tok in host for tok in tokens):
        score += 3
    if region and region.tld and host.endswith(region.tld):
        score += 2
    if url.startswith("https://"):
        score += 1
    return score


def _ddg_market(region: str | None) -> str | None:
    """Map a region key to DuckDuckGo's ``kl`` market parameter, if known."""
    return resolve_region(region).ddg_market if region else None
