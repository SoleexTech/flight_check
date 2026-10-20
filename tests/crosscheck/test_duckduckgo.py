"""Tests for DuckDuckGo source discovery (offline, no network)."""

from fli.crosscheck.duckduckgo import (
    _is_aggregator,
    _name_tokens,
    _parse_results,
    _resolve_href,
    find_official_sites,
    search,
)

# A trimmed snapshot of DuckDuckGo's HTML endpoint markup: two results, one
# official (klm.nl) and one aggregator (skyscanner), each wrapped in the
# /l/?uddg= redirect the endpoint uses for external links.
_KLM = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.klm.nl%2F&amp;rut=z"
_SKY = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.skyscanner.net%2Fklm"
SAMPLE_HTML = f"""
<a rel="nofollow" class="result__a" href="{_KLM}">
  KLM Royal Dutch Airlines
</a>
<a class="result__snippet">Book directly on the official KLM website.</a>
<a rel="nofollow" class="result__a" href="{_SKY}">
  Skyscanner — KLM flights
</a>
<a class="result__snippet">Compare KLM fares from many sites.</a>
"""


class _Resp:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    def __init__(self, text):
        self.text = text
        self.last_kwargs = None

    def post(self, url, **kwargs):
        self.last_kwargs = kwargs
        return _Resp(self.text)


class TestResolveHref:
    def test_unwraps_uddg_redirect(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.klm.nl%2F&rut=x"
        assert _resolve_href(href) == "https://www.klm.nl/"

    def test_promotes_scheme_relative(self):
        assert _resolve_href("//example.com/path") == "https://example.com/path"

    def test_decodes_html_entities(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2F&amp;rut=x"
        assert _resolve_href(href) == "https://a.com/"


class TestParseResults:
    def test_extracts_title_url_snippet(self):
        results = _parse_results(SAMPLE_HTML, max_results=10)
        assert len(results) == 2
        assert results[0].title == "KLM Royal Dutch Airlines"
        assert results[0].url == "https://www.klm.nl/"
        assert "official KLM" in results[0].snippet

    def test_respects_max_results(self):
        assert len(_parse_results(SAMPLE_HTML, max_results=1)) == 1


class TestHelpers:
    def test_name_tokens_drops_generic_words(self):
        assert _name_tokens("British Airways") == ["british"]
        assert "klm" in _name_tokens("KLM Royal Dutch Airlines")

    def test_aggregator_detection(self):
        assert _is_aggregator("skyscanner.net")
        assert _is_aggregator("kayak.co.uk")
        assert not _is_aggregator("klm.nl")


class TestSearch:
    def test_search_passes_market_and_proxy(self):
        client = _FakeClient(SAMPLE_HTML)
        results = search("klm", region="nl", proxy="http://p:1", client=client)
        assert results[0].url == "https://www.klm.nl/"
        assert client.last_kwargs["proxy"] == "http://p:1"
        assert client.last_kwargs["data"]["kl"] == "nl-nl"


class TestFindOfficialSites:
    def test_prefers_official_over_aggregator(self):
        client = _FakeClient(SAMPLE_HTML)
        sites = find_official_sites("KLM Royal Dutch Airlines", region="nl", client=client)
        assert sites[0] == "https://www.klm.nl/"
        assert all("skyscanner" not in s for s in sites)

    def test_empty_name_returns_empty(self):
        assert find_official_sites("   ", client=_FakeClient(SAMPLE_HTML)) == []

    def test_fallback_when_no_name_match(self):
        # A result whose host matches neither the name nor an aggregator is
        # still returned as a fallback so the caller has something to check.
        html = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fbooking-portal.example%2Fx">Portal</a>'
        )
        sites = find_official_sites("Nonexistent Air", client=_FakeClient(html))
        assert sites == ["https://booking-portal.example/x"]
