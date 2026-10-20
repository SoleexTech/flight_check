"""Cross-region price comparison.

Runs the *same* flight search from several regions at once — each through
that region's proxy and Google locale — bills them all in one common
currency so the numbers are directly comparable, and reports the spread.
Optionally cross-checks each region's cheapest carrier against its official
website (discovered via DuckDuckGo) for an independent, best-effort price
signal.

The interesting variable here is the originating region: holding currency,
route and date fixed, a query routed through an NL proxy vs. a US proxy can
come back at a different price, and this module surfaces that delta.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel

from fli.crosscheck.airline_sites import fetch_price_signals
from fli.crosscheck.duckduckgo import find_official_sites
from fli.crosscheck.regions import resolve_region
from fli.models import FlightResult, FlightSearchFilters
from fli.models.google_flights.base import TripType
from fli.proxy import ProxyPool
from fli.search import SearchFlights
from fli.search._concurrency import parallel_map

# Factory type for the search client (injected in tests; SearchFlights by default).
SearchFactory = Callable[[], SearchFlights]


class AirlineSiteCheck(BaseModel):
    """Result of cross-checking a carrier's official website for a price."""

    airline: str | None = None
    site_url: str | None = None
    signals: list[dict] = []  # serialized PriceSignal objects
    error: str | None = None


class RegionQuote(BaseModel):
    """The cheapest fare a single region returned, plus how it was fetched."""

    region: str
    label: str
    language: str | None = None
    country: str | None = None
    proxy: str | None = None  # creds-free "host:port", or None when direct
    currency: str
    google_price: float | None = None
    cheapest_airline: str | None = None
    cheapest_airline_code: str | None = None
    airline_site: AirlineSiteCheck | None = None
    error: str | None = None


class RegionComparison(BaseModel):
    """A full cross-region comparison with the headline spread."""

    origin: str | None = None
    destination: str | None = None
    departure_date: str | None = None
    return_date: str | None = None
    currency: str
    quotes: list[RegionQuote]
    cheapest_region: str | None = None
    most_expensive_region: str | None = None
    spread_abs: float | None = None  # most expensive - cheapest, in `currency`
    spread_pct: float | None = None  # spread as a percentage of the cheapest

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation (for MCP / CLI output)."""
        return self.model_dump()


def compare_regions(
    filters: FlightSearchFilters,
    regions: Sequence[str],
    *,
    currency: str = "USD",
    proxy_pool: ProxyPool | None = None,
    include_airline_sites: bool = False,
    origin: str | None = None,
    destination: str | None = None,
    departure_date: str | None = None,
    return_date: str | None = None,
    search_factory: SearchFactory = SearchFlights,
    ddg_client: object | None = None,
) -> RegionComparison:
    """Compare the cheapest fare for ``filters`` across several regions.

    Args:
        filters: The flight search to run identically in every region.
        regions: Region keys to compare (e.g. ``["nl", "eu", "us"]``).
        currency: Common ISO 4217 currency every region is billed in, so the
            prices are directly comparable.
        proxy_pool: Optional pool; each region is routed through its own
            proxy (falling back to the default/global one). ``None`` sends
            every request directly.
        include_airline_sites: When ``True``, also discover each region's
            cheapest carrier's official site and read a best-effort price.
        origin: Optional report-header origin; derived from ``filters`` when omitted.
        destination: Optional report-header destination; derived when omitted.
        departure_date: Optional report-header departure date; derived when omitted.
        return_date: Optional report-header return date; derived when omitted.
        search_factory: Builds a fresh :class:`SearchFlights` per region.
            ``SearchFlights`` is not thread-safe, so each parallel worker
            gets its own instance. Overridable for testing.
        ddg_client: HTTP client used for site discovery / fetching. Mainly a
            test injection point.

    Returns:
        A :class:`RegionComparison` with one :class:`RegionQuote` per region
        (in input order) and the computed price spread.

    """
    derived = _derive_route(filters)
    origin = origin or derived[0]
    destination = destination or derived[1]
    departure_date = departure_date or derived[2]
    return_date = return_date or derived[3]

    def run(region_key: str) -> RegionQuote:
        region = resolve_region(region_key)
        proxy = proxy_pool.get(region.key) if proxy_pool else None
        proxy_url = proxy.url if proxy else None
        quote = RegionQuote(
            region=region.key,
            label=region.label,
            language=region.language,
            country=region.country,
            proxy=proxy.label if proxy else None,
            currency=currency,
        )
        try:
            results = search_factory().search(
                filters,
                currency=currency,
                language=region.language,
                country=region.country,
                proxy=proxy_url,
            )
        except Exception as exc:  # noqa: BLE001 — one region must not abort the rest
            quote.error = _short_error(exc)
            return quote

        # Google sometimes returns rows without a pre-computed price (e.g.
        # premium-cabin round trips); those can't be compared, so skip them.
        priced = [r for r in results or [] if _itinerary_price(r) is not None]
        if not priced:
            quote.error = "no priced results" if results else "no results"
            return quote

        cheapest = min(priced, key=_itinerary_price)
        lead = cheapest[0] if isinstance(cheapest, tuple) else cheapest
        quote.google_price = round(_itinerary_price(cheapest), 2)
        quote.cheapest_airline, quote.cheapest_airline_code = _airline_label(lead)

        if include_airline_sites and quote.cheapest_airline:
            quote.airline_site = _check_airline_site(
                quote.cheapest_airline, region.key, proxy_url, ddg_client
            )
        return quote

    quotes = parallel_map(run, list(regions))
    return _summarize(origin, destination, departure_date, return_date, currency, quotes)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _check_airline_site(
    airline: str,
    region_key: str,
    proxy_url: str | None,
    client: object | None,
) -> AirlineSiteCheck:
    """Discover the carrier's official site and read a best-effort price."""
    try:
        sites = find_official_sites(airline, region=region_key, proxy=proxy_url, client=client)
    except Exception as exc:  # noqa: BLE001 — cross-check is best-effort
        return AirlineSiteCheck(airline=airline, error=_short_error(exc))
    if not sites:
        return AirlineSiteCheck(airline=airline, error="no official site found")

    url = sites[0]
    try:
        signals = fetch_price_signals(url, proxy=proxy_url, client=client)
    except Exception as exc:  # noqa: BLE001 — cross-check is best-effort
        return AirlineSiteCheck(airline=airline, site_url=url, error=_short_error(exc))
    return AirlineSiteCheck(
        airline=airline,
        site_url=url,
        signals=[signal.as_dict() for signal in signals],
        error=None if signals else "no price exposed on page",
    )


def _summarize(
    origin: str | None,
    destination: str | None,
    departure_date: str | None,
    return_date: str | None,
    currency: str,
    quotes: list[RegionQuote],
) -> RegionComparison:
    """Compute the cheapest/most-expensive regions and the price spread."""
    priced = [q for q in quotes if q.google_price is not None]
    cheapest_region = most_expensive_region = None
    spread_abs = spread_pct = None
    if priced:
        lo = min(priced, key=lambda q: q.google_price)
        hi = max(priced, key=lambda q: q.google_price)
        cheapest_region, most_expensive_region = lo.region, hi.region
        spread_abs = round(hi.google_price - lo.google_price, 2)
        if lo.google_price:
            spread_pct = round(spread_abs / lo.google_price * 100, 2)
    return RegionComparison(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        currency=currency,
        quotes=quotes,
        cheapest_region=cheapest_region,
        most_expensive_region=most_expensive_region,
        spread_abs=spread_abs,
        spread_pct=spread_pct,
    )


def _itinerary_price(item: FlightResult | tuple[FlightResult, ...]) -> float | None:
    """Return an itinerary's comparable total price, or ``None`` if unknown.

    Mirrors :func:`fli.cli.utils.serialize_flight_result`'s convention so the
    numbers line up with the rest of the tool: a round-trip pair carries the
    full price on the *outbound* (first) leg, while a multi-city itinerary
    (3+ legs) carries it on the *final* leg. One-way results price themselves.
    """
    if not isinstance(item, tuple):
        return item.price
    return item[0].price if len(item) == 2 else item[-1].price


def _airline_label(result: FlightResult) -> tuple[str | None, str | None]:
    """Return the (display name, IATA code) of a result's primary airline."""
    code_enum = result.primary_airline
    if code_enum is None and result.legs:
        code_enum = result.legs[0].airline
    code = code_enum.name.lstrip("_") if code_enum is not None else None
    name = result.primary_airline_name or (code_enum.value if code_enum is not None else None)
    return name, code


def _derive_route(
    filters: FlightSearchFilters,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Best-effort extraction of (origin, destination, dep date, return date)."""
    segments = filters.flight_segments
    if not segments:
        return None, None, None, None
    origin = _airport_code(segments[0].departure_airport)
    destination = _airport_code(segments[0].arrival_airport)
    departure_date = segments[0].travel_date
    return_date = None
    if filters.trip_type == TripType.ROUND_TRIP and len(segments) > 1:
        return_date = segments[1].travel_date
    return origin, destination, departure_date, return_date


def _airport_code(nested: object) -> str | None:
    """Pull the leading airport IATA code out of a segment's nested airport list."""
    try:
        airport = nested[0][0]  # type: ignore[index]
        return airport.name.lstrip("_")
    except (IndexError, TypeError, AttributeError):
        return None


def _short_error(exc: BaseException) -> str:
    """Render an exception as a compact one-line message for the report."""
    message = f"{type(exc).__name__}: {exc}".strip()
    return message if len(message) <= 200 else message[:197] + "..."
