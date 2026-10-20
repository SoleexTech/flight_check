"""Best-effort price extraction from airline (and OTA) web pages.

Reality check: airline booking flows are heterogeneous, JavaScript-heavy
and frequently sit behind anti-bot walls. We therefore do **not** try to
drive a generic booking flow — that path is fragile and never finished.
Instead this module reads the *price signals* a page is willing to hand a
crawler, in order of trustworthiness:

1. ``application/ld+json`` structured data — schema.org ``Offer`` /
   ``AggregateOffer`` (``price`` / ``lowPrice`` with ``priceCurrency``);
2. Open Graph / product price ``<meta>`` tags and ``itemprop="price"``;
3. a conservative currency-aware regex, used only when (1) and (2) find
   nothing.

When a specific carrier needs bespoke handling, register a site adapter
with :func:`register_adapter`. :func:`fetch_price_signals` dispatches to the
first adapter whose domain substring matches the URL host and otherwise
falls back to the generic extractor above. That keeps "scrape airline
sites" both honest (no false promises) and extensible (add carriers as
needed) without one brittle parser pretending to fit every site.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from fli.search.client import Client, get_client

_TAG_RE = re.compile(r"<[^>]+>")
_JSONLD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')

# Symbols/codes recognized by the regex fallback, mapped to ISO 4217.
_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY"}
_PRICE_NEAR_RE = re.compile(
    r"(US\$|€|\$|£|¥|EUR|USD|GBP|JPY)\s?([0-9][0-9.,]{0,9}[0-9]|[0-9])",
    re.IGNORECASE,
)

# Keys that carry a price / currency in meta tags and microdata.
_PRICE_META_KEYS = frozenset({"product:price:amount", "og:price:amount", "price", "og:price"})
_CURRENCY_META_KEYS = frozenset({"product:price:currency", "og:price:currency"})


@dataclass(frozen=True)
class PriceSignal:
    """One price observed on a page, with how it was found."""

    amount: float
    currency: str | None
    source_url: str
    method: str  # "json-ld", "meta" or "regex" — how confident to be

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation of the signal."""
        return {
            "amount": self.amount,
            "currency": self.currency,
            "source_url": self.source_url,
            "method": self.method,
        }


# A site adapter takes (markup, source_url) and returns extracted signals.
PriceAdapter = Callable[[str, str], list[PriceSignal]]
_ADAPTERS: list[tuple[str, PriceAdapter]] = []


def register_adapter(domain_substring: str, adapter: PriceAdapter) -> None:
    """Register a carrier-specific extractor for hosts containing ``domain_substring``.

    Adapters are tried in registration order; the first whose substring is
    found in the page host wins. Use this to add precise parsers for sites
    that don't expose structured price data.
    """
    _ADAPTERS.append((domain_substring.lower(), adapter))


def fetch_price_signals(
    url: str,
    *,
    proxy: str | None = None,
    client: Client | None = None,
) -> list[PriceSignal]:
    """Fetch ``url`` (optionally via ``proxy``) and extract its price signals.

    Routes through the shared impersonating client so it benefits from the
    same rate limiting and browser fingerprint as the flight search. A
    registered adapter for the host takes precedence over the generic
    extractor.
    """
    http = client or get_client()
    response = http.get(url, impersonate="chrome", proxy=proxy, allow_redirects=True)
    markup = response.text
    host = _host(url)
    for substring, adapter in _ADAPTERS:
        if substring in host:
            return adapter(markup, url)
    return extract_price_signals(markup, url)


def extract_price_signals(markup: str, source_url: str) -> list[PriceSignal]:
    """Extract price signals from page ``markup`` using the generic strategy.

    Tries structured data first (JSON-LD, then price meta tags) and only
    falls back to the noisier regex scan when neither yields anything.
    Returns a de-duplicated list (empty when no price is exposed).
    """
    signals: list[PriceSignal] = []
    signals.extend(_from_json_ld(markup, source_url))
    signals.extend(_from_meta(markup, source_url))
    if not signals:
        signals.extend(_from_regex(markup, source_url))
    return _dedupe(signals)


# ---------------------------------------------------------------------------
# Extraction strategies
# ---------------------------------------------------------------------------


def _from_json_ld(markup: str, source_url: str) -> list[PriceSignal]:
    """Pull schema.org Offer/AggregateOffer prices from ld+json blocks."""
    signals: list[PriceSignal] = []
    for block in _JSONLD_RE.findall(markup):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue  # Malformed or templated JSON-LD — skip it.
        pairs: list[tuple[float, str | None]] = []
        _walk_json_ld(data, pairs)
        signals.extend(
            PriceSignal(amount, _normalize_currency(cur), source_url, "json-ld")
            for amount, cur in pairs
        )
    return signals


def _walk_json_ld(node: object, out: list[tuple[float, str | None]]) -> None:
    """Recursively collect (price, currency) pairs from a JSON-LD tree."""
    if isinstance(node, dict):
        for price_key in ("price", "lowPrice"):
            if price_key in node:
                amount = _parse_amount(node[price_key])
                if amount is not None:
                    currency = node.get("priceCurrency") or node.get("currency")
                    out.append((amount, currency if isinstance(currency, str) else None))
        for value in node.values():
            _walk_json_ld(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_json_ld(item, out)


def _from_meta(markup: str, source_url: str) -> list[PriceSignal]:
    """Read price/currency from Open Graph, product, and microdata meta tags."""
    amounts: list[float] = []
    currency: str | None = None
    for tag in _META_TAG_RE.findall(markup):
        attrs = {key.lower(): value for key, value in _ATTR_RE.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        content = attrs.get("content")
        if not key or content is None:
            continue
        if key in _PRICE_META_KEYS:
            amount = _parse_amount(content)
            if amount is not None:
                amounts.append(amount)
        elif key in _CURRENCY_META_KEYS:
            currency = _normalize_currency(content)
    return [PriceSignal(amount, currency, source_url, "meta") for amount in amounts]


def _from_regex(markup: str, source_url: str) -> list[PriceSignal]:
    """Last-resort scan for ``<symbol><number>`` price-looking substrings."""
    # Strip tags, then unescape entities so "&euro;" / "&#8364;" become "€".
    text = html.unescape(_TAG_RE.sub(" ", markup))
    signals: list[PriceSignal] = []
    for symbol, number in _PRICE_NEAR_RE.findall(text)[:5]:
        amount = _parse_amount(number)
        if amount is not None and amount > 0:
            currency = _CURRENCY_SYMBOLS.get(symbol) or symbol.upper().replace("US$", "USD")
            signals.append(PriceSignal(amount, currency, source_url, "regex"))
    return signals


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _parse_amount(value: object) -> float | None:
    """Parse a price into a float, tolerating both ``1,234.56`` and ``1.234,56``."""
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[^\d.,-]", "", value.strip())
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        # The right-most separator is the decimal point; the other groups thousands.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Comma alone: treat "12,34" as decimal but "1,234" as a thousands group.
        if re.search(r",\d{2}$", cleaned):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_currency(value: object) -> str | None:
    """Upper-case a 3-letter ISO currency code, else return ``None``."""
    if isinstance(value, str):
        code = value.strip().upper()
        if len(code) == 3 and code.isalpha():
            return code
    return None


def _dedupe(signals: list[PriceSignal]) -> list[PriceSignal]:
    """Drop signals that repeat the same (amount, currency) pair, keeping order."""
    seen: set[tuple[float, str | None]] = set()
    unique: list[PriceSignal] = []
    for signal in signals:
        marker = (signal.amount, signal.currency)
        if marker not in seen:
            seen.add(marker)
            unique.append(signal)
    return unique


def _host(url: str) -> str:
    """Return the lower-cased hostname of ``url`` (no leading ``www.``)."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host
