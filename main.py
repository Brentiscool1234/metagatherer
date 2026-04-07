#!/usr/bin/env python3
"""
MetaGatherer — FB Ads Library winning-product scraper
======================================================
Uses Selenium to automate the PUBLIC Facebook Ads Library website.
No API key. No Meta approval. No Facebook login. Zero ban risk.

Setup:
    pip install -r requirements.txt
    python main.py

Examples:
    python main.py
    python main.py --countries GB --days 3 --min-ads 15
    python main.py -k "posture corrector" -k "knee brace"
    python main.py --headless --output winners.csv
"""

import logging
import sys
from datetime import datetime

import click
from rich.console import Console
from rich.logging import RichHandler

console = Console()


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    for lib in ("urllib3", "requests", "charset_normalizer", "selenium"):
        logging.getLogger(lib).setLevel(logging.WARNING)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--countries", "-c", multiple=True, default=["US"], show_default=True,
              help="Target country codes (repeatable: -c US -c GB)")
@click.option("--days", "-d", default=7, show_default=True, type=int,
              help="Lookback window in days for ad start dates.")
@click.option("--min-ads", default=12, show_default=True, type=int,
              help="Minimum active ads per product cluster.")
@click.option("--min-followers", default=10, show_default=True, type=int,
              help="Minimum page follower count.")
@click.option("--max-followers", default=2000, show_default=True, type=int,
              help="Maximum page follower count.")
@click.option("--keywords", "-k", multiple=True,
              help="Extra seed keywords (repeatable: -k 'posture corrector')")
@click.option("--max-keywords", default=30, show_default=True, type=int,
              help="Max total keywords to search including BFS-discovered ones.")
@click.option("--keyword-depth", default=3, show_default=True, type=int,
              help="BFS depth for keyword expansion (0 = seed keywords only).")
@click.option("--max-ads-per-keyword", default=120, show_default=True, type=int,
              help="Max ads to collect per keyword search.")
@click.option("--video-only/--no-video-only", default=False, show_default=True,
              help="Only include pages that have at least one video ad.")
@click.option("--require-shop-now/--no-require-shop-now", default=True, show_default=True,
              help="Require Shop Now / Buy Now CTA detected in ad copy.")
@click.option("--headless/--no-headless", default=False, show_default=True,
              help="Run Chrome headlessly (no visible window). Default: visible.")
@click.option("--output", "-o", default=None,
              help="CSV output path. Auto-named with timestamp if omitted.")
@click.option("--no-csv", is_flag=True, default=False, help="Skip CSV export.")
@click.option("--state-file", default="scraper_state.json", show_default=True,
              help="Path to the persistent state file for resuming interrupted runs.")
@click.option("--reset", is_flag=True, default=False,
              help="Ignore saved state and start a fresh scan from scratch.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Debug logging.")
def main(
    countries, days, min_ads, min_followers, max_followers,
    keywords, max_keywords, keyword_depth, max_ads_per_keyword,
    video_only, require_shop_now, headless, output, no_csv,
    state_file, reset, verbose,
):
    """MetaGatherer: Find winning ecommerce products in the Facebook Ads Library."""
    _setup_logging(verbose)

    from fb_ads_scraper.scraper import FBAdsScraper
    from fb_ads_scraper.output import print_summary_banner, print_results_table, export_csv
    from fb_ads_scraper.state import load_state, state_summary, DEFAULT_STATE_FILE
    import os

    console.rule("[bold cyan]MetaGatherer — FB Ads Library Scanner[/bold cyan]")
    console.print(
        f"  Countries: [bold]{', '.join(countries)}[/bold]  |  "
        f"Lookback: [bold]{days}d[/bold]  |  "
        f"Min ads: [bold]{min_ads}[/bold]  |  "
        f"Followers: [bold]{min_followers}–{max_followers}[/bold]"
    )
    console.print(
        f"  Require Shop Now: [bold]{require_shop_now}[/bold]  |  "
        f"Video only: [bold]{video_only}[/bold]  |  "
        f"Max keywords: [bold]{max_keywords}[/bold] (depth {keyword_depth})  |  "
        f"Headless: [bold]{headless}[/bold]"
    )
    console.rule()

    # Show resume / reset status
    if reset:
        console.print("[yellow]--reset flag set — ignoring any saved state, starting fresh.[/yellow]\n")
    elif os.path.exists(state_file):
        saved = load_state(state_file)
        if saved:
            console.print(
                f"[green]Resuming saved state[/green] ({state_file}): "
                f"[bold]{state_summary(saved)}[/bold]\n"
                f"[dim]Use --reset to start over.[/dim]\n"
            )
        else:
            console.print(f"[dim]No usable state in {state_file} — starting fresh.[/dim]\n")
    else:
        console.print(f"[dim]No saved state found — starting fresh.[/dim]\n")

    if not headless:
        console.print(
            "[dim]A Chrome window will open and navigate the public Ads Library. "
            "Don't close it while scanning.[/dim]\n"
        )

    scraper = FBAdsScraper(
        countries=list(countries),
        days=days,
        min_ads=min_ads,
        min_followers=min_followers,
        max_followers=max_followers,
        prefer_video=True,
        require_shop_now=require_shop_now,
        max_keyword_depth=keyword_depth,
        max_keywords=max_keywords,
        max_ads_per_keyword=max_ads_per_keyword,
        headless=headless,
        state_file=state_file,
        reset=reset,
    )

    winners = scraper.run(extra_keywords=list(keywords) if keywords else None)

    if video_only:
        winners = [w for w in winners if w.is_video]

    print_summary_banner(
        total_keywords=len(scraper._searched_keywords),
        total_ads=len(scraper._seen_keys),
        total_pages=len(scraper._page_ads),
        winner_count=len(winners),
    )
    print_results_table(winners, days)

    if not no_csv and winners:
        if output is None:
            output = f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        export_csv(winners, output)


if __name__ == "__main__":
    main()
