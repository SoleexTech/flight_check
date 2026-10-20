"""Tests for region-aware proxy parsing and pooling (offline)."""

import pytest

from fli.proxy import DEFAULT_REGION, Proxy, ProxyPool, load_pool, parse_proxy_line


class TestParseProxyLine:
    def test_host_port(self):
        proxy = parse_proxy_line("1.2.3.4:8080")
        assert (proxy.host, proxy.port, proxy.username) == ("1.2.3.4", 8080, None)
        assert proxy.url == "http://1.2.3.4:8080"

    def test_host_port_user_pass(self):
        proxy = parse_proxy_line("1.2.3.4:8080:bob:s3cret")
        assert (proxy.username, proxy.password) == ("bob", "s3cret")
        assert proxy.url == "http://bob:s3cret@1.2.3.4:8080"

    def test_inline_auth(self):
        proxy = parse_proxy_line("bob:s3cret@1.2.3.4:8080")
        assert proxy.url == "http://bob:s3cret@1.2.3.4:8080"

    def test_full_url_preserves_scheme(self):
        proxy = parse_proxy_line("socks5://u:p@1.2.3.4:1080")
        assert proxy.scheme == "socks5"
        assert proxy.url == "socks5://u:p@1.2.3.4:1080"

    def test_special_chars_in_url_auth_are_encoded(self):
        proxy = Proxy("h", 1, username="a@b", password="c d")
        assert "a%40b" in proxy.url and "c%20d" in proxy.url

    @pytest.mark.parametrize("blank", ["", "   ", "# a comment", "  # trailing"])
    def test_blank_and_comments_skipped(self, blank):
        assert parse_proxy_line(blank) is None

    def test_inline_comment_stripped(self):
        assert parse_proxy_line("1.2.3.4:8080  # main").port == 8080

    @pytest.mark.parametrize("bad", ["nonsense", "host:notaport", "1.2.3.4:99999"])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            parse_proxy_line(bad)

    def test_label_hides_credentials(self):
        assert parse_proxy_line("1.2.3.4:8080:bob:s3cret").label == "1.2.3.4:8080"


class TestProxyPool:
    def test_round_robin_within_region(self):
        pool = ProxyPool({"nl": [Proxy("a", 1), Proxy("b", 2)]})
        assert [pool.get("nl").host for _ in range(4)] == ["a", "b", "a", "b"]

    def test_fallback_to_default_region(self):
        pool = ProxyPool({DEFAULT_REGION: [Proxy("d", 9)]})
        assert pool.get("us").host == "d"

    def test_missing_region_and_no_default_returns_none(self):
        pool = ProxyPool({"nl": [Proxy("a", 1)]})
        assert pool.get("us") is None

    def test_url_helper(self):
        pool = ProxyPool({"nl": [Proxy("a", 1, "u", "p")]})
        assert pool.url("nl") == "http://u:p@a:1"
        assert pool.url("missing") is None

    def test_region_keys_are_lowercased(self):
        pool = ProxyPool({"NL": [Proxy("a", 1)]})
        assert pool.regions() == ["nl"]
        assert pool.get("nl").host == "a"

    def test_is_empty(self):
        assert ProxyPool().is_empty is True
        assert ProxyPool({"nl": [Proxy("a", 1)]}).is_empty is False

    def test_merge(self):
        a = ProxyPool({"nl": [Proxy("a", 1)]})
        a.merge(ProxyPool({"us": [Proxy("b", 2)], "nl": [Proxy("c", 3)]}))
        assert a.regions() == ["nl", "us"]
        assert {p.host for p in [a.get("nl"), a.get("nl")]} == {"a", "c"}


class TestProxyPoolLoaders:
    def test_from_dir(self, tmp_path):
        (tmp_path / "nl.txt").write_text("1.1.1.1:1\n# note\n2.2.2.2:2:u:p\n")
        (tmp_path / "us.txt").write_text("3.3.3.3:3\n")
        pool = ProxyPool.from_dir(tmp_path)
        assert pool.regions() == ["nl", "us"]
        assert pool.get("us").host == "3.3.3.3"

    def test_from_dir_skips_malformed_lines(self, tmp_path):
        (tmp_path / "nl.txt").write_text("good:8080\nnonsense-line\nalso:bad:format:too:many\n")
        pool = ProxyPool.from_dir(tmp_path)
        # Only the one valid line survives; bad ones are skipped, not fatal.
        assert pool.get("nl").host == "good"

    def test_from_dir_missing_directory(self, tmp_path):
        assert ProxyPool.from_dir(tmp_path / "nope").is_empty

    def test_from_env_single_proxy(self):
        pool = ProxyPool.from_env({"FLI_PROXY": "9.9.9.9:9"})
        assert pool.get("anything").host == "9.9.9.9"

    def test_from_env_combines_single_and_dir(self, tmp_path):
        (tmp_path / "nl.txt").write_text("1.1.1.1:1\n")
        pool = ProxyPool.from_env({"FLI_PROXY": "9.9.9.9:9", "FLI_PROXY_DIR": str(tmp_path)})
        assert pool.get("nl").host == "1.1.1.1"  # region-specific
        assert pool.get("zz").host == "9.9.9.9"  # falls back to FLI_PROXY default

    def test_from_env_empty(self):
        assert ProxyPool.from_env({}).is_empty

    def test_from_env_ignores_invalid_single(self):
        assert ProxyPool.from_env({"FLI_PROXY": "nonsense"}).is_empty

    def test_load_pool_uses_environment(self, monkeypatch):
        monkeypatch.setenv("FLI_PROXY", "7.7.7.7:7")
        monkeypatch.delenv("FLI_PROXY_DIR", raising=False)
        assert load_pool().get("nl").host == "7.7.7.7"
