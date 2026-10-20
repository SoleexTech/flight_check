"""Tests for the MCP compare_region_prices tool (offline — comparison mocked)."""

from datetime import date, timedelta

from fli.crosscheck import RegionComparison, RegionQuote
from fli.mcp.server import RegionCompareParams, _compare_regions_from_params

# A date safely in the future so filter validation never rejects it as
# "in the past" no matter when the suite runs.
TRAVEL_DATE = (date.today() + timedelta(days=60)).isoformat()


def _canned() -> RegionComparison:
    return RegionComparison(
        origin="AMS",
        destination="JFK",
        departure_date=TRAVEL_DATE,
        currency="USD",
        quotes=[
            RegionQuote(region="nl", label="Netherlands", currency="USD", google_price=320.0),
            RegionQuote(region="us", label="United States", currency="USD", google_price=300.0),
        ],
        cheapest_region="us",
        most_expensive_region="nl",
        spread_abs=20.0,
        spread_pct=6.67,
    )


def test_comparison_returns_structured_result(monkeypatch):
    monkeypatch.setattr("fli.mcp.server.compare_regions", lambda *a, **k: _canned())
    params = RegionCompareParams(
        origin="AMS", destination="JFK", departure_date=TRAVEL_DATE, regions=["nl", "us"]
    )
    result = _compare_regions_from_params(params)
    assert result["success"] is True
    assert result["cheapest_region"] == "us"
    assert len(result["quotes"]) == 2


def test_comparison_bills_in_common_currency(monkeypatch):
    captured = {}

    def fake(filters, regions, **kwargs):
        captured.update(kwargs)
        return _canned()

    monkeypatch.setattr("fli.mcp.server.compare_regions", fake)
    params = RegionCompareParams(
        origin="AMS", destination="JFK", departure_date=TRAVEL_DATE, currency="EUR"
    )
    _compare_regions_from_params(params)
    assert captured["currency"] == "EUR"


def test_comparison_invalid_airport_reports_error():
    params = RegionCompareParams(
        origin="ZZZ", destination="JFK", departure_date=TRAVEL_DATE, regions=["nl"]
    )
    result = _compare_regions_from_params(params)
    assert result["success"] is False
    assert "error" in result
