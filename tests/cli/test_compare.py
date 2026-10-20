"""Tests for the `fli compare` CLI command (offline — comparison is mocked)."""

import json
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from fli.cli.main import app
from fli.crosscheck import RegionComparison, RegionQuote

# A date safely in the future so filter validation never rejects it as
# "in the past" no matter when the suite runs.
TRAVEL_DATE = (date.today() + timedelta(days=60)).isoformat()


@pytest.fixture
def runner():
    return CliRunner()


def _canned_comparison() -> RegionComparison:
    return RegionComparison(
        origin="AMS",
        destination="JFK",
        departure_date=TRAVEL_DATE,
        currency="EUR",
        quotes=[
            RegionQuote(
                region="nl",
                label="Netherlands",
                country="NL",
                proxy="1.1.1.1:8080",
                currency="EUR",
                google_price=300.0,
                cheapest_airline="KLM",
                cheapest_airline_code="KL",
            ),
            RegionQuote(
                region="us",
                label="United States",
                country="US",
                proxy=None,
                currency="EUR",
                google_price=250.0,
                cheapest_airline="Delta Air Lines",
                cheapest_airline_code="DL",
            ),
        ],
        cheapest_region="us",
        most_expensive_region="nl",
        spread_abs=50.0,
        spread_pct=20.0,
    )


@pytest.fixture
def mock_compare(monkeypatch):
    """Replace the network-hitting compare_regions with a canned result."""
    captured = {}

    def fake(filters, regions, **kwargs):
        captured["regions"] = regions
        captured["kwargs"] = kwargs
        return _canned_comparison()

    monkeypatch.setattr("fli.cli.commands.compare.compare_regions", fake)
    return captured


def test_compare_text_output(runner, mock_compare):
    result = runner.invoke(app, ["compare", "AMS", "JFK", TRAVEL_DATE, "--regions", "nl,us"])
    assert result.exit_code == 0
    assert "AMS" in result.output and "JFK" in result.output
    assert "Spread" in result.output
    assert mock_compare["regions"] == ["nl", "us"]


def test_compare_json_output(runner, mock_compare):
    result = runner.invoke(
        app, ["compare", "AMS", "JFK", TRAVEL_DATE, "--regions", "nl,us", "--format", "json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["cheapest_region"] == "us"
    assert payload["spread_abs"] == 50.0
    assert len(payload["quotes"]) == 2


def test_compare_forwards_options(runner, mock_compare):
    runner.invoke(
        app,
        [
            "compare",
            "AMS",
            "JFK",
            TRAVEL_DATE,
            "--regions",
            "nl,eu,us",
            "--currency",
            "EUR",
            "--airline-sites",
        ],
    )
    kwargs = mock_compare["kwargs"]
    assert kwargs["currency"] == "EUR"
    assert kwargs["include_airline_sites"] is True


def test_compare_default_regions(runner, mock_compare):
    runner.invoke(app, ["compare", "AMS", "JFK", TRAVEL_DATE])
    assert mock_compare["regions"] == ["nl", "eu", "us"]


def test_compare_invalid_airport_errors_cleanly(runner):
    # No mock: resolve_airport rejects the bogus code before any network call.
    result = runner.invoke(app, ["compare", "ZZZ", "JFK", TRAVEL_DATE])
    assert result.exit_code == 1


def test_compare_empty_regions_errors(runner, mock_compare):
    result = runner.invoke(app, ["compare", "AMS", "JFK", TRAVEL_DATE, "--regions", " , "])
    assert result.exit_code == 1
