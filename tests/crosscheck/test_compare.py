"""Tests for the cross-region comparison orchestrator (offline, no network)."""

from datetime import date, datetime, timedelta

import pytest

from fli.core import build_flight_segments, resolve_airport
from fli.crosscheck.compare import _itinerary_price, compare_regions
from fli.models import Airline, Airport, FlightLeg, FlightResult, FlightSearchFilters, PassengerInfo
from fli.proxy import Proxy, ProxyPool

# A date safely in the future so filter validation never rejects it as
# "in the past" no matter when the suite runs.
TRAVEL_DATE = (date.today() + timedelta(days=60)).isoformat()


def _result(price: float | None) -> FlightResult:
    """Build a minimal one-way FlightResult priced at ``price``, flown by Delta."""
    leg = FlightLeg(
        airline=Airline.DL,
        flight_number="DL1",
        departure_airport=Airport.AMS,
        arrival_airport=Airport.JFK,
        departure_datetime=datetime(2026, 10, 25, 10, 0),
        arrival_datetime=datetime(2026, 10, 25, 18, 0),
        duration=480,
    )
    return FlightResult(
        price=price,
        duration=480,
        stops=0,
        legs=[leg],
        primary_airline=Airline.DL,
        primary_airline_name="Delta Air Lines",
    )


class _FakeSearch:
    """Returns canned results per Google ``gl`` country, recording each call."""

    def __init__(self, by_country, calls):
        self._by_country = by_country
        self._calls = calls

    def search(self, filters, *, currency, language, country, proxy):
        self._calls.append({"country": country, "proxy": proxy, "currency": currency})
        spec = self._by_country.get(country)
        if isinstance(spec, Exception):
            raise spec
        if not spec:
            return None
        return [_result(p) for p in spec]


class _Resp:
    def __init__(self, text):
        self.text = text


class _FakeHTTP:
    """Serves DuckDuckGo markup on POST and an airline page on GET."""

    def __init__(self, ddg_html, page_html):
        self._ddg_html = ddg_html
        self._page_html = page_html

    def post(self, url, **kwargs):
        return _Resp(self._ddg_html)

    def get(self, url, **kwargs):
        return _Resp(self._page_html)


@pytest.fixture
def one_way_filters():
    segments, trip_type = build_flight_segments(
        origin=resolve_airport("AMS"),
        destination=resolve_airport("JFK"),
        departure_date=TRAVEL_DATE,
        return_date=None,
    )
    return FlightSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=segments,
    )


def _factory(by_country, calls):
    return lambda: _FakeSearch(by_country, calls)


class TestCompareRegions:
    def test_basic_spread_and_ordering(self, one_way_filters):
        # eu resolves to country=None, so key the prices on that.
        prices = {"NL": [300.0, 350.0], None: [320.0], "US": [250.0]}
        result = compare_regions(
            one_way_filters,
            ["nl", "eu", "us"],
            currency="EUR",
            search_factory=_factory(prices, []),
        )
        assert [q.region for q in result.quotes] == ["nl", "eu", "us"]
        assert result.cheapest_region == "us"
        assert result.most_expensive_region == "eu"
        assert result.spread_abs == 70.0
        assert result.spread_pct == 28.0
        assert result.currency == "EUR"

    def test_picks_cheapest_and_reports_airline(self, one_way_filters):
        result = compare_regions(
            one_way_filters,
            ["nl"],
            search_factory=_factory({"NL": [300.0, 350.0]}, []),
        )
        nl = result.quotes[0]
        assert nl.google_price == 300.0
        assert nl.cheapest_airline == "Delta Air Lines"
        assert nl.cheapest_airline_code == "DL"

    def test_unpriced_rows_are_ignored(self, one_way_filters):
        # Google can return rows with price=None (issue #165); they must not
        # crash min() and must not win the "cheapest" slot.
        result = compare_regions(
            one_way_filters,
            ["nl"],
            search_factory=_factory({"NL": [None, 410.0, None]}, []),
        )
        assert result.quotes[0].google_price == 410.0

    def test_all_unpriced_reports_error(self, one_way_filters):
        result = compare_regions(
            one_way_filters,
            ["nl"],
            search_factory=_factory({"NL": [None, None]}, []),
        )
        assert result.quotes[0].google_price is None
        assert result.quotes[0].error == "no priced results"
        assert result.cheapest_region is None

    def test_route_metadata_derived_from_filters(self, one_way_filters):
        result = compare_regions(
            one_way_filters, ["us"], search_factory=_factory({"US": [100.0]}, [])
        )
        assert (result.origin, result.destination) == ("AMS", "JFK")
        assert result.departure_date == TRAVEL_DATE

    def test_one_region_failure_does_not_abort_others(self, one_way_filters):
        prices = {"NL": ValueError("boom"), None: [], "US": [199.0]}
        result = compare_regions(
            one_way_filters,
            ["nl", "eu", "us"],
            search_factory=_factory(prices, []),
        )
        quotes = {q.region: q for q in result.quotes}
        assert quotes["nl"].google_price is None
        assert quotes["nl"].error.startswith("ValueError")
        assert quotes["eu"].error == "no results"
        assert quotes["us"].google_price == 199.0
        assert result.cheapest_region == "us"  # only the priced region counts

    def test_proxy_pool_routes_per_region(self, one_way_filters):
        calls: list[dict] = []
        pool = ProxyPool({"nl": [Proxy("nlhost", 1)], "us": [Proxy("ushost", 2)]})
        result = compare_regions(
            one_way_filters,
            ["nl", "eu", "us"],
            proxy_pool=pool,
            search_factory=_factory({"NL": [1.0], None: [2.0], "US": [3.0]}, calls),
        )
        by_country = {c["country"]: c["proxy"] for c in calls}
        assert by_country["NL"] == "http://nlhost:1"
        assert by_country["US"] == "http://ushost:2"
        assert by_country[None] is None  # eu has no proxy and no default
        quotes = {q.region: q for q in result.quotes}
        assert quotes["nl"].proxy == "nlhost:1"  # creds-free label
        assert quotes["eu"].proxy is None

    def test_no_results_anywhere_yields_no_spread(self, one_way_filters):
        result = compare_regions(one_way_filters, ["nl", "us"], search_factory=_factory({}, []))
        assert result.cheapest_region is None
        assert result.spread_abs is None
        assert all(q.error == "no results" for q in result.quotes)


class TestAirlineSiteCrossCheck:
    def test_discovers_official_site_and_reads_price(self, one_way_filters):
        ddg_html = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fwww.delta.com%2F">Delta Air Lines</a>'
        )
        page_html = (
            '<script type="application/ld+json">{"price":201.0,"priceCurrency":"USD"}</script>'
        )
        result = compare_regions(
            one_way_filters,
            ["us"],
            include_airline_sites=True,
            search_factory=_factory({"US": [250.0]}, []),
            ddg_client=_FakeHTTP(ddg_html, page_html),
        )
        check = result.quotes[0].airline_site
        assert check is not None
        assert check.site_url == "https://www.delta.com/"
        assert check.signals[0]["amount"] == 201.0
        assert check.error is None

    def test_no_official_site_found_is_reported(self, one_way_filters):
        # DDG returns only an aggregator -> filtered out -> no official site.
        ddg_html = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fwww.skyscanner.net%2Fx">Skyscanner</a>'
        )
        result = compare_regions(
            one_way_filters,
            ["us"],
            include_airline_sites=True,
            search_factory=_factory({"US": [250.0]}, []),
            ddg_client=_FakeHTTP(ddg_html, "<html></html>"),
        )
        check = result.quotes[0].airline_site
        assert check.site_url is None
        assert check.error == "no official site found"


class TestItineraryPrice:
    """Lock the price-location convention shared with serialize_flight_result."""

    def test_one_way_uses_own_price(self):
        assert _itinerary_price(_result(199.0)) == 199.0

    def test_round_trip_uses_outbound_price(self):
        # Google puts the full round-trip price on the OUTBOUND leg, not the return.
        outbound, return_leg = _result(450.0), _result(999.0)
        assert _itinerary_price((outbound, return_leg)) == 450.0

    def test_multi_city_uses_final_leg_price(self):
        # Multi-city (3+ legs) carries the combined price on the FINAL leg.
        first, middle, last = _result(100.0), _result(200.0), _result(640.0)
        assert _itinerary_price((first, middle, last)) == 640.0

    def test_unknown_price_is_none(self):
        assert _itinerary_price(_result(None)) is None
