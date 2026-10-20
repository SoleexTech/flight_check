"""Region-aware HTTP proxy configuration and pooling.

This module is the single place ``fli`` decides *which* proxy a request is
routed through, and it lets an operator pin a request to a region — i.e.
run a Google Flights query "from" the Netherlands, the wider EU, or the US
— so they can observe how Google's prices shift with the originating IP.

Two setup styles are supported, mirroring the project's env-var-first
configuration convention:

* **One proxy, zero files** — set ``FLI_PROXY`` to a single proxy. It
  becomes the *default*, used whenever a region has no dedicated list.

* **A mixed pool, one file per region** — point ``FLI_PROXY_DIR`` at a
  directory of ``<region>.txt`` files (``nl.txt``, ``us.txt``, ``eu.txt``
  …). Each non-empty line is one proxy and the filename (without its
  extension) is the region key. This is exactly the shape residential /
  datacenter proxy vendors export, so a vendor list can be pasted in
  verbatim without reformatting.

Accepted proxy line formats (blank lines and ``#`` comments are skipped)::

    host:port                      # no authentication
    host:port:username:password    # vendor "ip:port:user:pass" export
    username:password@host:port    # curl-style inline auth
    http://user:pass@host:port     # a full URL (scheme preserved)

Any line without an explicit ``scheme://`` is treated as ``http://`` — the
de-facto default for HTTP CONNECT proxies, which is what ``curl_cffi``
expects. Credentials are never logged or surfaced: :attr:`Proxy.label`
returns a creds-free ``host:port`` for display.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Region key used for a proxy that is not tied to any specific region — the
# single ``FLI_PROXY`` value lands here and acts as the fallback when a
# requested region has no proxies of its own.
DEFAULT_REGION = "default"


@dataclass(frozen=True)
class Proxy:
    """A single proxy endpoint, optionally authenticated.

    Immutable so instances can be shared freely across worker threads.
    """

    host: str
    port: int
    username: str | None = None
    password: str | None = None
    scheme: str = "http"

    @property
    def url(self) -> str:
        """Return the full ``scheme://[user:pass@]host:port`` proxy URL.

        Credentials are percent-encoded so passwords containing ``:``,
        ``@`` or other reserved characters don't corrupt the URL.
        """
        auth = ""
        if self.username:
            user = urllib.parse.quote(self.username, safe="")
            pw = urllib.parse.quote(self.password or "", safe="")
            auth = f"{user}:{pw}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def label(self) -> str:
        """Return a credential-free ``host:port`` label safe to log or display."""
        return f"{self.host}:{self.port}"


def parse_proxy_line(line: str) -> Proxy | None:
    """Parse one proxy line into a :class:`Proxy`, or ``None`` to skip it.

    Returns ``None`` for blank lines and ``#`` comments. Raises
    :class:`ValueError` for a non-empty line that does not match any
    supported format (the file loader downgrades that to a warning and
    skips the line, so a stray header row can't abort a whole pool load).
    """
    # Drop inline/whole-line comments and surrounding whitespace.
    text = line.split("#", 1)[0].strip()
    if not text:
        return None

    # Case 1: a full URL with an explicit scheme — let urllib do the work
    # and preserve whatever scheme the operator chose (http/https/socks5).
    if "://" in text:
        parts = urllib.parse.urlsplit(text)
        if not parts.hostname or not parts.port:
            raise ValueError(f"proxy URL missing host or port: {text!r}")
        return Proxy(
            host=parts.hostname,
            port=parts.port,
            username=urllib.parse.unquote(parts.username) if parts.username else None,
            password=urllib.parse.unquote(parts.password) if parts.password else None,
            scheme=parts.scheme or "http",
        )

    # Case 2: curl-style "user:pass@host:port" inline auth.
    if "@" in text:
        auth, _, hostport = text.rpartition("@")
        user, _, pw = auth.partition(":")
        host, port = _split_host_port(hostport)
        return Proxy(host=host, port=port, username=user or None, password=pw or None)

    # Case 3: the colon-delimited vendor formats.
    fields = text.split(":")
    if len(fields) == 2:
        host, port = _split_host_port(text)
        return Proxy(host=host, port=port)
    if len(fields) == 4:
        host, port_str, user, pw = fields
        return Proxy(host=host, port=_to_port(port_str), username=user or None, password=pw or None)

    raise ValueError(
        f"unrecognized proxy format: {text!r} "
        "(expected host:port, host:port:user:pass, user:pass@host:port, or a URL)"
    )


def _split_host_port(text: str) -> tuple[str, int]:
    """Split a bare ``host:port`` token, raising ``ValueError`` if malformed."""
    host, sep, port_str = text.rpartition(":")
    if not sep or not host:
        raise ValueError(f"expected host:port, got {text!r}")
    return host, _to_port(port_str)


def _to_port(port_str: str) -> int:
    """Convert a port string to an int in the valid 1-65535 range."""
    try:
        port = int(port_str)
    except ValueError:
        raise ValueError(f"invalid proxy port: {port_str!r}") from None
    if not 0 < port < 65536:
        raise ValueError(f"proxy port out of range: {port}")
    return port


class ProxyPool:
    """A region-keyed collection of proxies with round-robin selection.

    Lookups are thread-safe: :meth:`get` advances a per-region cursor under
    a lock so concurrent searches spread their load across a region's
    proxies rather than all hammering the first one. When a requested
    region has no proxies, :meth:`get` falls back to the
    :data:`DEFAULT_REGION` list (populated from ``FLI_PROXY``) and finally
    to ``None`` — meaning "send the request directly, no proxy".
    """

    def __init__(self, proxies: dict[str, list[Proxy]] | None = None):
        """Build a pool, optionally seeded from a ``{region: [Proxy, ...]}`` mapping."""
        self._by_region: dict[str, list[Proxy]] = {}
        self._cursors: dict[str, int] = {}
        self._lock = threading.Lock()
        for region, entries in (proxies or {}).items():
            self.add(region, entries)

    def add(self, region: str, proxies: list[Proxy]) -> None:
        """Append ``proxies`` to ``region``'s list (region keys are lower-cased)."""
        if not proxies:
            return
        self._by_region.setdefault(region.lower(), []).extend(proxies)

    def merge(self, other: ProxyPool) -> None:
        """Merge another pool's proxies into this one, region by region."""
        for region, entries in other._by_region.items():
            self.add(region, entries)

    def regions(self) -> list[str]:
        """Return the region keys that currently hold at least one proxy."""
        return sorted(self._by_region)

    @property
    def is_empty(self) -> bool:
        """Return ``True`` when the pool holds no proxies at all."""
        return not any(self._by_region.values())

    def get(self, region: str | None = None) -> Proxy | None:
        """Return the next proxy for ``region`` (round-robin), or ``None``.

        Falls back to the :data:`DEFAULT_REGION` list when ``region`` has no
        proxies, then to ``None`` when neither does — letting callers route
        the request directly.
        """
        for key in self._candidate_keys(region):
            entries = self._by_region.get(key)
            if entries:
                with self._lock:
                    idx = self._cursors.get(key, 0) % len(entries)
                    self._cursors[key] = idx + 1
                return entries[idx]
        return None

    def url(self, region: str | None = None) -> str | None:
        """Return :meth:`get`'s proxy as a ready-to-use URL, or ``None``."""
        proxy = self.get(region)
        return proxy.url if proxy else None

    @staticmethod
    def _candidate_keys(region: str | None) -> list[str]:
        """Region lookup order: the requested region, then the default."""
        keys = []
        if region:
            keys.append(region.lower())
        if DEFAULT_REGION not in keys:
            keys.append(DEFAULT_REGION)
        return keys

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_dir(cls, path: str | os.PathLike[str]) -> ProxyPool:
        """Load a pool from a directory of ``<region>.txt`` proxy lists.

        Each ``*.txt`` file becomes one region (keyed by its filename stem);
        malformed lines are logged and skipped rather than aborting the load.
        """
        pool = cls()
        directory = Path(path)
        if not directory.is_dir():
            logger.warning("Proxy directory does not exist: %s", directory)
            return pool
        for file in sorted(directory.glob("*.txt")):
            proxies = cls._read_file(file)
            if proxies:
                pool.add(file.stem, proxies)
        return pool

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ProxyPool:
        """Build a pool from ``FLI_PROXY`` and/or ``FLI_PROXY_DIR``.

        ``FLI_PROXY`` (a single proxy) seeds the default/fallback region;
        ``FLI_PROXY_DIR`` adds the per-region files on top. Either, both, or
        neither may be set — an unset environment yields an empty pool.
        """
        env = environ if environ is not None else os.environ
        pool = cls()

        single = (env.get("FLI_PROXY") or "").strip()
        if single:
            try:
                proxy = parse_proxy_line(single)
                if proxy is not None:
                    pool.add(DEFAULT_REGION, [proxy])
            except ValueError as exc:
                logger.warning("Ignoring invalid FLI_PROXY value: %s", exc)

        directory = (env.get("FLI_PROXY_DIR") or "").strip()
        if directory:
            pool.merge(cls.from_dir(directory))

        return pool

    @staticmethod
    def _read_file(file: Path) -> list[Proxy]:
        """Parse every valid proxy line in ``file`` (skipping bad ones)."""
        proxies: list[Proxy] = []
        for lineno, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                proxy = parse_proxy_line(raw)
            except ValueError as exc:
                logger.warning("%s:%d skipped — %s", file.name, lineno, exc)
                continue
            if proxy is not None:
                proxies.append(proxy)
        return proxies


def load_pool() -> ProxyPool:
    """Return a :class:`ProxyPool` built from the current environment.

    A thin convenience wrapper around :meth:`ProxyPool.from_env` for CLI and
    MCP entry points. Builds a fresh pool each call so picking up edited
    proxy files never needs a process restart.
    """
    return ProxyPool.from_env()
