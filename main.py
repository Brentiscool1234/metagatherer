#!/usr/bin/env python3
"""
MetaGatherer — FB Ads Library winning-product scraper
======================================================

Usage:
    python main.py [OPTIONS]

Examples:
    # Basic scan (US, last 7 days, min 12 ads):
    python main.py

    # Target UK, last 3 days, min 15 ads, video only, export CSV:
    python main.py --countries GB --days 3 --min-ads 15 --video-only --output results.csv

    # Add custom seed keywords:
    python main.py --keywords "posture corrector" "back pain" "knee brace"

    # Relax Shopify requirement to cast a wider net:
    python main.py --no-require-shop-now --max-followers 5000
"""

import logging
import os
import sys
from datetime import datetime

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

load_dotenv()

console = Console()


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    # Suppress noisy libs
    for lib in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(lib).setLevel(logging.WARNING)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--token",
    envvar="FB_ACCESS_TOKEN",
    required=True,
    help="Facebook Graph API access token. Can also be set via FB_ACCESS_TOKEN env var.",
)
@click.option(
    "--countries",
    "-c",
    multiple=True,
    default=["US"],
    show_default=True,
    help="Target countries (2-letter ISO codes). Can be repeated: -c US -c GB -c CA",
)
@click.option(
    "--days",
    "-d",
    default=7,
    show_default=True,
    type=int,
    help="Lookback window in days. Ads must have started within this window.",
)
@click.option(
    "--min-ads",
    default=12,
    show_default=True,
    type=int,
    help="Minimum number of active ads a page must have for the same product.",
)
@click.option(
    "--min-followers",
    default=10,
    show_default=True,
    type=int,
    help="Minimum page follower count.",
)
@click.option(
    "--max-followers",
    default=2000,
    show_default=True,
    type=int,
    help="Maximum page follower count.",
)
@click.option(
    "--keywords",
    "-k",
    multiple=True,
    help="Additional seed keywords to prepend to the built-in list. Can be repeated.",
)
@click.option(
    "--max-keywords",
    default=30,
    show_default=True,
    type=int,
    help="Max total keywords to search (including BFS-discovered ones).",
)
@click.option(
    "--keyword-depth",
    default=3,
    show_default=True,
    type=int,
    help="BFS expansion depth. 0 = only seed keywords, 3 = three levels of discovery.",
)
@click.option(
    "--video-only/--no-video-only",
    default=False,
    show_default=True,
    help="If set, only consider pages that have at least one video ad.",
)
@click.option(
    "--require-shop-now/--no-require-shop-now",
    default=True,
    show_default=True,
    help="Require detected Shop Now CTA in ad copy.",
)
@click.option(
    "--output",
    "-o",
    default=None,
    help=(
        "Path to output CSV file. "
        "Defaults to results_<timestamp>.csv in the current directory."
    ),
)
@click.option(
    "--no-csv",
    is_flag=True,
    default=False,
    help="Skip CSV export (terminal output only).",
)
@click.option(
    "--api-pages",
    default=3,
    show_default=True,
    type=int,
    help="Number of FB API result pages to fetch per keyword (each page = up to 500 ads).",
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Enable debug logging.")
def main(
    token,
    countries,
    days,
    min_ads,
    min_followers,
    max_followers,
    keywords,
    max_keywords,
    keyword_depth,
    video_only,
    require_shop_now,
    output,
    no_csv,
    api_pages,
    verbose,
):
    """MetaGatherer: Find winning ecommerce products in the Facebook Ads Library."""
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    # Lazy imports after logging is set up
    from fb_ads_scraper.api import FBApiClient
    from fb_ads_scraper.scraper import FBAdsScraper
    from fb_ads_scraper.output import (
        print_summary_banner,
        print_results_table,
        export_csv,
        console as out_console,
    )

    console.rule("[bold cyan]MetaGatherer — FB Ads Library Scanner[/bold cyan]")
    console.print(
        f"  Countries: [bold]{', '.join(countries)}[/bold]  |  "
        f"Lookback: [bold]{days}d[/bold]  |  "
        f"Min ads: [bold]{min_ads}[/bold]  |  "
        f"Followers: [bold]{min_followers}–{max_followers}[/bold]"
    )
    console.print(
        f"  Video only: [bold]{video_only}[/bold]  |  "
        f"Require Shop Now: [bold]{require_shop_now}[/bold]  |  "
        f"Max keywords: [bold]{max_keywords}[/bold] (depth {keyword_depth})"
    )
    console.rule()

    # Build client and verify token
    client = FBApiClient(access_token=token)
    console.print("Verifying access token...", end=" ")
    if not client.verify_token():
        console.print("[bold red]FAILED[/bold red]")
        console.print(
            "[red]Your Facebook access token is invalid or expired.\n"
            "See README.md for how to generate a valid token.[/red]"
        )
        sys.exit(1)
    console.print("[bold green]OK[/bold green]")

    # Build and run scraper
    scraper = FBAdsScraper(
        client=client,
        countries=list(countries),
        days=days,
        min_ads=min_ads,
        min_followers=min_followers,
        max_followers=max_followers,
        prefer_video=True,  # always prefer video; --video-only filters output
        require_shop_now=require_shop_now,
        max_keyword_depth=keyword_depth,
        max_keywords=max_keywords,
        api_pages_per_keyword=api_pages,
    )

    try:
        winners = scraper.run(extra_keywords=list(keywords) if keywords else None)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Showing partial results...[/yellow]")
        # Evaluate whatever was collected so far
        winners = scraper._evaluate_pages()

    # Apply video-only filter if requested
    if video_only:
        before = len(winners)
        winners = [w for w in winners if w.is_video]
        log.info(f"Video-only filter: {before} → {len(winners)} results")

    # Print summary banner
    print_summary_banner(
        total_keywords=len(scraper._searched_keywords),
        total_ads=len(scraper._seen_ad_ids),
        total_pages=len(scraper._page_ads),
        winner_count=len(winners),
    )

    # Print terminal table
    print_results_table(winners, days)

    # Export CSV
    if not no_csv and winners:
        if output is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output = f"results_{ts}.csv"
        export_csv(winners, output)


if __name__ == "__main__":
    main()
