"""Tests for best-effort airline-site price extraction (offline)."""

import pytest

from fli.crosscheck.airline_sites import (
    PriceSignal,
    _parse_amount,
    extract_price_signals,
    fetch_price_signals,
    register_adapter,
)


class _Resp:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    """Records the last GET and returns canned markup."""

    def __init__(self, text):
        self.text = text
        self.last_url = None
        self.last_kwargs = None

    def get(self, url, **kwargs):
        self.last_url = url
        self.last_kwargs = kwargs
        return _Resp(self.text)


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("199", 199.0),
            ("199.99", 199.99),
            ("1,234.56", 1234.56),  # US thousands + decimal
            ("1.234,56", 1234.56),  # EU thousands + decimal
            ("12,34", 12.34),  # EU decimal comma
            ("1,234", 1234.0),  # thousands comma
            ("€ 89", 89.0),
            (250, 250.0),
        ],
    )
    def test_parses(self, raw, expected):
        assert _parse_amount(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", None, object()])
    def test_unparseable_returns_none(self, raw):
        assert _parse_amount(raw) is None


class TestExtractPriceSignals:
    def test_json_ld_offer(self):
        markup = (
            '<script type="application/ld+json">'
            '{"@type":"Offer","price":"199.99","priceCurrency":"EUR"}</script>'
        )
        signals = extract_price_signals(markup, "https://x")
        assert signals == [PriceSignal(199.99, "EUR", "https://x", "json-ld")]

    def test_json_ld_aggregate_offer_nested(self):
        markup = (
            '<script type="application/ld+json">'
            '{"@type":"Product","offers":{"@type":"AggregateOffer",'
            '"lowPrice":120,"priceCurrency":"USD"}}</script>'
        )
        signals = extract_price_signals(markup, "u")
        assert (signals[0].amount, signals[0].currency) == (120.0, "USD")

    def test_meta_tags(self):
        markup = (
            '<meta property="product:price:amount" content="1.234,56">'
            '<meta property="product:price:currency" content="eur">'
        )
        signals = extract_price_signals(markup, "u")
        assert (signals[0].amount, signals[0].currency, signals[0].method) == (
            1234.56,
            "EUR",
            "meta",
        )

    def test_regex_fallback_only_when_no_structured_data(self):
        signals = extract_price_signals("<p>Fares from &euro; 89 now</p>", "u")
        assert signals[0].method == "regex"
        assert (signals[0].amount, signals[0].currency) == (89.0, "EUR")

    def test_structured_data_suppresses_regex(self):
        markup = (
            '<script type="application/ld+json">{"price":50,"priceCurrency":"GBP"}</script>'
            "<span>£999</span>"
        )
        methods = {s.method for s in extract_price_signals(markup, "u")}
        assert methods == {"json-ld"}

    def test_no_price_returns_empty(self):
        assert extract_price_signals("<html>nothing here</html>", "u") == []

    def test_malformed_json_ld_is_ignored(self):
        markup = '<script type="application/ld+json">{not valid json}</script>'
        assert extract_price_signals(markup, "u") == []

    def test_dedupes_repeated_pairs(self):
        markup = (
            '<script type="application/ld+json">[{"price":10,"priceCurrency":"EUR"},'
            '{"price":10,"priceCurrency":"EUR"}]</script>'
        )
        assert len(extract_price_signals(markup, "u")) == 1


class TestFetchPriceSignals:
    def test_routes_through_client_and_extracts(self):
        client = _FakeClient('<meta property="og:price:amount" content="42">')
        signals = fetch_price_signals("https://klm.nl/fare", proxy="http://p:1", client=client)
        assert signals[0].amount == 42.0
        assert client.last_url == "https://klm.nl/fare"
        assert client.last_kwargs["proxy"] == "http://p:1"
        assert client.last_kwargs["impersonate"] == "chrome"

    def test_registered_adapter_takes_precedence(self):
        sentinel = [PriceSignal(7.0, "USD", "u", "adapter")]
        register_adapter("adapter-test.example", lambda markup, url: sentinel)
        client = _FakeClient('<script type="application/ld+json">{"price":1}</script>')
        out = fetch_price_signals("https://adapter-test.example/x", client=client)
        assert out == sentinel
