"""Tests for region resolution (offline)."""

from fli.crosscheck.regions import REGIONS, resolve_region


def test_known_region_has_full_locale():
    nl = resolve_region("nl")
    assert (nl.country, nl.language, nl.ddg_market, nl.tld) == ("NL", "nl", "nl-nl", ".nl")


def test_case_and_whitespace_insensitive():
    assert resolve_region("  US ").country == "US"


def test_eu_is_country_agnostic():
    # No single EU gl value: language fixed, country left to the proxy IP.
    eu = resolve_region("eu")
    assert eu.country is None and eu.language == "en"


def test_unknown_two_letter_treated_as_country():
    assert resolve_region("pt").country == "PT"


def test_unknown_non_country_degrades_gracefully():
    region = resolve_region("zz9")
    assert region.country is None and region.language is None


def test_registry_keys_are_lowercase():
    assert all(key == key.lower() for key in REGIONS)
