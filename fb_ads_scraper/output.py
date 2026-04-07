"""
Output layer: Rich terminal dashboard and CSV export.
"""

import csv
import os
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.align import Align
from rich import box

from .scraper import WinningProduct

console = Console()


def _bool_icon(val: bool) -> str:
    return "[green]✓[/green]" if val else "[red]✗[/red]"


def print_summary_banner(total_keywords: int, total_ads: int, total_pages: int, winner_count: int):
    stats = (
        f"[bold]Keywords searched:[/bold] {total_keywords}   "
        f"[bold]Total ads collected:[/bold] {total_ads}   "
        f"[bold]Pages evaluated:[/bold] {total_pages}   "
        f"[bold cyan]Winning products found:[/bold cyan] {winner_count}"
    )
    console.print(Panel(
        Align.center(stats),
        title="[bold cyan]MetaGatherer — Scan Complete[/bold cyan]",
        border_style="cyan",
    ))


def print_results_table(winners: list[WinningProduct], days: int):
    if not winners:
        console.print("\n[yellow]No winning products found. Try relaxing the filters or adding more keywords.[/yellow]\n")
        return

    table = Table(
        title=f"[bold cyan]Winning Products[/bold cyan]",
        box=box.ROUNDED,
        show_lines=True,
        highlight=True,
        expand=True,
    )

    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Page", style="bold", min_width=18)
    table.add_column("Followers", justify="right", width=12)
    table.add_column("Ads", justify="center", width=6)
    table.add_column("Video", justify="center", width=6)
    table.add_column("Shop Now", justify="center", width=9)
    table.add_column("Shopify", justify="center", width=8)
    table.add_column("Keywords", min_width=20)
    table.add_column("Ad Body (sample)", min_width=35)

    for i, w in enumerate(winners, 1):
        fol = w.page_followers
        if fol == 0:
            fol_str = "[dim]unknown[/dim]"
        elif fol <= 500:
            fol_str = f"[green]{fol:,}[/green]"
        else:
            fol_str = f"[yellow]{fol:,}[/yellow]"

        ad_color = "bright_green" if w.ad_count >= 20 else ("green" if w.ad_count >= 12 else "yellow")
        ad_str = f"[{ad_color}]{w.ad_count}[/{ad_color}]"

        preview = (w.sample_ad_body or "").strip()
        if len(preview) > 90:
            preview = preview[:87] + "..."

        kws = ", ".join(w.keywords_matched[:4]) if w.keywords_matched else "—"

        table.add_row(
            str(i),
            f"[link={w.page_url}]{w.page_name}[/link]",
            fol_str,
            ad_str,
            _bool_icon(w.is_video),
            _bool_icon(w.has_shop_now),
            _bool_icon(w.is_shopify),
            kws,
            preview or "—",
        )

    console.print()
    console.print(table)
    console.print()

    if winners:
        console.print("[bold cyan]── Top Results (detail) ──[/bold cyan]")
        for w in winners[:5]:
            _print_detail_card(w)


def _print_detail_card(w: WinningProduct):
    fol_display = f"{w.page_followers:,}" if w.page_followers > 0 else "unknown"
    lines = [
        f"[bold]{w.page_name}[/bold]  •  {fol_display} followers",
        f"Page URL:     [cyan]{w.page_url or '—'}[/cyan]",
        f"Active ads:   [bold green]{w.ad_count}[/bold green]  (total seen on page: {w.total_page_ads})",
        f"Platforms:    {', '.join(w.publisher_platforms) or '—'}",
        f"Video: {_bool_icon(w.is_video)}   Shop Now CTA: {_bool_icon(w.has_shop_now)}   "
        f"Shopify: {_bool_icon(w.is_shopify)}  ({w.shopify_reason})",
        f"Ad dates:     {', '.join(w.ad_start_dates[:5])}{'...' if len(w.ad_start_dates) > 5 else ''}",
        f"Keywords:     [italic]{', '.join(w.keywords_matched[:8]) or '—'}[/italic]",
        "",
        f"[dim]Sample body:[/dim]  {w.sample_ad_body or '—'}",
        f"[dim]Snapshot:[/dim]     [cyan]{w.sample_snapshot_url or '—'}[/cyan]",
    ]
    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]#{i if (i := getattr(w, '_rank', '?')) else '?'} {w.page_name}[/bold]",
        border_style="green",
        expand=False,
    ))
    console.print()


def export_csv(winners: list[WinningProduct], path: str):
    if not winners:
        console.print("[yellow]No results to export.[/yellow]")
        return

    rows = [w.to_dict() for w in winners]
    fieldnames = list(rows[0].keys())

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    console.print(f"\n[bold green]✓ Exported to:[/bold green] [cyan]{os.path.abspath(path)}[/cyan]")
    console.print(f"  {len(winners)} winning products, {len(fieldnames)} columns.\n")
