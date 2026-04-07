"""
Output layer: Rich terminal dashboard and CSV export.
"""

import csv
import os
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box
from rich.columns import Columns
from rich.align import Align

from .scraper import WinningProduct

console = Console()


def _bool_icon(val: bool) -> str:
    return "[green]✓[/green]" if val else "[red]✗[/red]"


def print_summary_banner(total_keywords: int, total_ads: int, total_pages: int, winner_count: int):
    """Print a summary stats panel before the results table."""
    stats = (
        f"[bold]Keywords searched:[/bold] {total_keywords}   "
        f"[bold]Total ads collected:[/bold] {total_ads}   "
        f"[bold]Pages evaluated:[/bold] {total_pages}   "
        f"[bold cyan]Winning products found:[/bold cyan] {winner_count}"
    )
    console.print(Panel(Align.center(stats), title="[bold cyan]MetaGatherer — Scan Complete[/bold cyan]", border_style="cyan"))


def print_results_table(winners: list[WinningProduct], days: int):
    """Render a Rich table of winning products in the terminal."""
    if not winners:
        console.print("\n[yellow]No winning products found. Try relaxing the filters or adding more keywords.[/yellow]\n")
        return

    table = Table(
        title=f"[bold cyan]Winning Products (active ads within last {days} days)[/bold cyan]",
        box=box.ROUNDED,
        show_lines=True,
        highlight=True,
        expand=True,
    )

    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Page", style="bold", min_width=18)
    table.add_column("Followers", justify="right", width=10)
    table.add_column("Ads\n(window)", justify="center", width=8)
    table.add_column("Video", justify="center", width=6)
    table.add_column("Shop\nNow", justify="center", width=6)
    table.add_column("Shopify", justify="center", width=8)
    table.add_column("Platforms", min_width=12)
    table.add_column("Impressions", justify="right", width=14)
    table.add_column("Ad Title / Body (sample)", min_width=30)
    table.add_column("Snapshot URL", min_width=20, overflow="fold")

    for i, w in enumerate(winners, 1):
        # Followers coloring
        fol_color = "green" if w.page_followers <= 500 else "yellow"
        fol_str = f"[{fol_color}]{w.page_followers:,}[/{fol_color}]"

        # Ad count coloring
        ad_color = "bright_green" if w.ad_count >= 20 else ("green" if w.ad_count >= 12 else "yellow")
        ad_str = f"[{ad_color}]{w.ad_count}[/{ad_color}]"

        preview = w.sample_ad_title or w.sample_ad_body
        if len(preview) > 80:
            preview = preview[:77] + "..."

        table.add_row(
            str(i),
            f"[link={w.page_url}]{w.page_name}[/link]",
            fol_str,
            ad_str,
            _bool_icon(w.is_video),
            _bool_icon(w.has_shop_now),
            _bool_icon(w.is_shopify),
            ", ".join(w.publisher_platforms) or "—",
            w.impressions_range,
            preview or "—",
            w.sample_snapshot_url or "—",
        )

    console.print()
    console.print(table)
    console.print()

    # Detail cards for top 3
    if winners:
        console.print("[bold cyan]── Top Results (detail) ──[/bold cyan]")
        for w in winners[:3]:
            _print_detail_card(w)


def _print_detail_card(w: WinningProduct):
    lines = [
        f"[bold]{w.page_name}[/bold]  •  {w.page_followers:,} followers  •  {w.page_category}",
        f"Page URL: [cyan]{w.page_url}[/cyan]",
        f"Active ads in window: [bold green]{w.ad_count}[/bold green]  (total seen: {w.total_page_ads})",
        f"Platforms: {', '.join(w.publisher_platforms) or '—'}  |  Languages: {', '.join(w.languages) or '—'}",
        f"Video: {_bool_icon(w.is_video)}  Shop Now CTA: {_bool_icon(w.has_shop_now)}  Shopify: {_bool_icon(w.is_shopify)}  ({w.shopify_reason})",
        f"Ad dates: {', '.join(w.ad_start_dates[:5])}{'...' if len(w.ad_start_dates) > 5 else ''}",
        f"Keywords matched: [italic]{', '.join(w.keywords_matched[:8])}[/italic]",
        f"Impressions: {w.impressions_range}  |  Spend: {w.spend_range} {w.currency}",
        "",
        f"[dim]Sample title:[/dim]  {w.sample_ad_title or '—'}",
        f"[dim]Sample body:[/dim]   {w.sample_ad_body or '—'}",
        f"[dim]Snapshot:[/dim]      [cyan]{w.sample_snapshot_url or '—'}[/cyan]",
    ]
    body = "\n".join(lines)
    console.print(
        Panel(body, title=f"[bold]#{w.page_id}[/bold]", border_style="green", expand=False)
    )
    console.print()


def export_csv(winners: list[WinningProduct], path: str):
    """Write results to a CSV file."""
    if not winners:
        console.print("[yellow]No results to export.[/yellow]")
        return

    rows = [w.to_dict() for w in winners]
    fieldnames = list(rows[0].keys())

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    console.print(f"\n[bold green]✓ Results exported to:[/bold green] [cyan]{os.path.abspath(path)}[/cyan]")
    console.print(f"  {len(winners)} winning products, {len(fieldnames)} columns.\n")
