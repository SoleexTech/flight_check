# Proxy pools

This directory holds the region proxy lists used by `fli compare` and the
`compare_region_prices` MCP tool to route a search "from" a chosen region
(e.g. fetch prices as a visitor in NL vs. the US would see them).

## Setup

You have two options — pick whichever fits:

### 1. One proxy (quickest)

Set a single environment variable. It's used as the default for every
region that doesn't have its own list:

```bash
export FLI_PROXY="1.2.3.4:8080:username:password"
```

### 2. A mixed pool, one file per region (recommended for many proxies)

Drop one `<region>.txt` file per region in this directory and point
`FLI_PROXY_DIR` at it (or pass `--proxy-dir ./proxies` to `fli compare`):

```bash
export FLI_PROXY_DIR="./proxies"
```

The filename (without `.txt`) is the region key, so `nl.txt` → region `nl`,
`us.txt` → region `us`, and so on. See `nl.txt.example` / `us.txt.example`
for the format, then copy them to `nl.txt` / `us.txt` and fill in real
proxies. Known region keys (`nl`, `eu`, `us`, `gb`, `de`, `fr`, …) also set
the matching Google locale automatically.

## Line format

One proxy per line. Blank lines and `#` comments are ignored. All of these
work (vendor "ip:port:user:pass" exports paste in verbatim):

```
1.2.3.4:8080                    # no auth
1.2.3.4:8080:username:password  # vendor ip:port:user:pass
username:password@1.2.3.4:8080  # inline auth
http://username:password@1.2.3.4:8080
socks5://1.2.3.4:1080           # scheme preserved
```

Within a region, proxies are used round-robin so load is spread evenly.

## Security

**Never commit real proxy lists** — they contain credentials. `proxies/*.txt`
is git-ignored; only `*.example` templates and this README are tracked.
