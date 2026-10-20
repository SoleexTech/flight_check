"""Cross-region price comparison CLI command.

``fli compare`` runs the same search from several regions — each through
its own proxy and Google locale, all billed in one currency — and prints
the per-region price plus the spread, so regional price differences are
visible at a glance. With ``--airline-sites`` it also cross-checks each
region's cheapest carrier against its official website.
"""

from typing import Annotated

import typer
from rich.table import Table

from fli.cli.console import console
from fli.cli.enums import OutputFormat
from fli.cli.utils import emit_json, normalize_cli_date, validate_currency
from fli.core import (
    build_flight_segments,
    parse_airlines,
    parse_cabin_class,
    parse_max_stops,
    resolve_airport,
)
from fli.core.currency import format_price
from fli.core.parsers import ParseError
from fli.crosscheck import RegionComparison, RegionQuote, compare_regions
from fli.models import FlightSearchFilters, PassengerInfo
from fli.proxy import ProxyPool, load_pool


def compare(
    origin: Annotated[str, typer.Argument(help="Departure airport IATA code (e.g., AMS)")],
    destination: Annotated[str, typer.Argument(help="Arrival airport IATA code (e.g., JFK)")],
    departure_date: Annotated[str, typer.Argument(help="Travel date (YYYY-MM-DD)")],
    regions: Annotated[
        str,
        typer.Option(
            "--regions",
            "-R",
            help="Comma-separated region keys to compare (e.g., nl,eu,us).",
        ),
    ] = "nl,eu,us",
    return_date: Annotated[
        str | None,
        typer.Option("--return", "-r", help="Return date (YYYY-MM-DD) for a round trip."),
    ] = None,
    currency: Annotated[
        str,
        typer.Option(
            "--currency",
            callback=validate_currency,
            help="Common currency every region is billed in, so prices are comparable.",
        ),
    ] = "USD",
    cabin_class: Annotated[
        str,
        typer.Option("--class", "-c", help="Cabin class (ECONOMY, BUSINESS, ...)."),
    ] = "ECONOMY",
    max_stops: Annotated[
        str,
        typer.Option("--stops", "-s", help="Maximum stops (ANY, NON_STOP, ONE_STOP, ...)."),
    ] = "ANY",
    airlines: Annotated[
        list[str] | None,
        typer.Option("--airlines", "-a", help="Restrict to these airline IATA codes."),
    ] = None,
    airline_sites: Annotated[
        bool,
        typer.Option(
            "--airline-sites",
            help="Also cross-check each region's cheapest carrier's official website.",
        ),
    ] = False,
    proxy_dir: Annotated[
        str | None,
        typer.Option(
            "--proxy-dir",
            help="Directory of <region>.txt proxy lists (defaults to $FLI_PROXY_DIR).",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", case_sensitive=False, help="Output format: text or json."),
    ] = OutputFormat.TEXT,
):
    """Compare a flight's price across regions, each via its own proxy/locale.

    Example:
        fli compare AMS JFK 2026-10-25 --regions nl,eu,us
        fli compare AMS JFK 2026-10-25 -R nl,us --airline-sites
        fli compare LHR JFK 2026-10-25 --regions gb,us --format json

    """
    try:
        region_keys = [r.strip() for r in regions.split(",") if r.strip()]
        if not region_keys:
            raise ParseError("No regions provided. Use e.g. --regions nl,eu,us")

        departure_date = normalize_cli_date(departure_date)
        return_date = normalize_cli_date(return_date)

        segments, trip_type = build_flight_segments(
            origin=resolve_airport(origin),
            destination=resolve_airport(destination),
            departure_date=departure_date,
            return_date=return_date,
        )
        filters = FlightSearchFilters(
            trip_type=trip_type,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=segments,
            stops=parse_max_stops(max_stops),
            seat_type=parse_cabin_class(cabin_class),
            airlines=parse_airlines(airlines),
        )

        comparison = compare_regions(
            filters,
            region_keys,
            currency=currency,
            proxy_pool=_build_pool(proxy_dir),
            include_airline_sites=airline_sites,
            origin=origin.upper(),
            destination=destination.upper(),
            departure_date=departure_date,
            return_date=return_date,
        )
    except ParseError as e:
        if output_format == OutputFormat.JSON:
            emit_json({"success": False, "error": str(e)})
            raise typer.Exit(1) from e
        typer.echo(f"Error: {e}")
        raise typer.Exit(1) from e

    if output_format == OutputFormat.JSON:
        emit_json({"success": True, **comparison.to_dict()})
        return
    _render(comparison, show_sites=airline_sites)


def _build_pool(proxy_dir: str | None) -> ProxyPool:
    """Build the proxy pool from the environment, plus an optional override dir."""
    pool = load_pool()
    if proxy_dir:
        pool.merge(ProxyPool.from_dir(proxy_dir))
    return pool


def _render(comparison: RegionComparison, *, show_sites: bool) -> None:
    """Print the comparison as a Rich table with a spread summary."""
    title = f"{comparison.origin} → {comparison.destination} on {comparison.departure_date}"
    if comparison.return_date:
        title += f" · return {comparison.return_date}"

    table = Table(title=title, header_style="bold", title_style="bold cyan")
    table.add_column("Region")
    table.add_column("Proxy")
    table.add_column(f"Cheapest ({comparison.currency})", justify="right")
    table.add_column("Airline")
    if show_sites:
        table.add_column("Official site")
        table.add_column("Site price", justify="right")

    for quote in comparison.quotes:
        if quote.google_price is None:
            price = f"[dim]{quote.error or '—'}[/dim]"
            airline = "—"
        else:
            price = format_price(quote.google_price, comparison.currency)
            airline = quote.cheapest_airline or "—"
        row = [f"{quote.label} ({quote.region})", quote.proxy or "direct", price, airline]
        if show_sites:
            row.extend(_site_columns(quote))
        table.add_row(*row)

    console.print(table)
    _render_summary(comparison)


def _site_columns(quote: RegionQuote) -> list[str]:
    """Build the two ``--airline-sites`` columns (URL and best price) for a row."""
    check = quote.airline_site
    if not check:
        return ["—", "—"]
    site = check.site_url or f"[dim]{check.error or '—'}[/dim]"
    if check.signals:
        best = check.signals[0]
        site_price = format_price(best["amount"], best.get("currency"))
    else:
        site_price = "—"
    return [site, site_price]


def _render_summary(comparison: RegionComparison) -> None:
    """Print the cheapest/most-expensive headline and the spread."""
    if comparison.cheapest_region is None or comparison.spread_abs is None:
        console.print("\n[yellow]No comparable prices were returned.[/yellow]")
        return
    spread = format_price(comparison.spread_abs, comparison.currency)
    pct = f" ({comparison.spread_pct}%)" if comparison.spread_pct is not None else ""
    console.print(
        f"\nCheapest: [green]{comparison.cheapest_region}[/green]  ·  "
        f"Most expensive: [red]{comparison.most_expensive_region}[/red]  ·  "
        f"Spread: [bold]{spread}{pct}[/bold]"
    )
