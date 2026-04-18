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
import os
import sys
import io
from datetime import datetime

logger = logging.getLogger(__name__)

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

# ── Windows UTF-8 fix ─────────────────────────────────────────────────────────
# When launched via pythonw.exe (GUI subprocess) stdout is a pipe but Python
# may assign it cp1252 encoding.  Rich's legacy Windows renderer then crashes
# on box-drawing characters (─ in console.rule).  Force UTF-8 + disable the
# legacy renderer so Rich uses normal ANSI output regardless.
if sys.platform == "win32":
    try:
        if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        elif sys.stdout is not None and hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
    except Exception:
        pass

# Load .env before anything else so ANTHROPIC_API_KEY is available
load_dotenv()

console = Console(legacy_windows=False)


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    for lib in ("urllib3", "requests", "charset_normalizer", "selenium",
                "httpx", "httpcore", "anthropic", "hpack"):
        logging.getLogger(lib).setLevel(logging.WARNING)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--countries", "-c", multiple=True, default=["US"], show_default=True,
              help="Target country codes (repeatable: -c US -c GB)")
@click.option("--days", "-d", default=7, show_default=True, type=int,
              help="Lookback window in days for ad start dates.")
@click.option("--min-ads", default=5, show_default=True, type=int,
              help="Minimum active ads per product cluster.")
@click.option("--min-followers", default=10, show_default=True, type=int,
              help="Minimum page follower count.")
@click.option("--max-followers", default=400, show_default=True, type=int,
              help="Maximum page follower count. N8N-validated threshold: 400.")
@click.option("--max-total-ads", default=250, show_default=True, type=int,
              help="Max total active ad versions per page (0 = no cap). "
                   "N8N-validated threshold: 250 removes heavy media buyers.")
@click.option("--min-active-ratio", default=0.85, show_default=True, type=float,
              help="Minimum fraction of a page's ads that must be active (0–1). "
                   "N8N-validated threshold: 0.85. Use 0 to disable.")
@click.option("--niche", "-n", default=None,
              help="Product niche to focus on (e.g. 'pet products', 'home fitness gear'). "
                   "AI will generate targeted seed keywords instead of generic ones.")
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
@click.option("--require-shop-now/--no-require-shop-now", default=False, show_default=True,
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
@click.option("--facebook/--no-facebook", default=True, show_default=True,
              help="Run the Facebook Ads Library scan.")
@click.option("--tiktok/--no-tiktok", default=True, show_default=True,
              help="Also scan TikTok for the same keywords (50k views, last 30 days).")
@click.option("--tiktok-login", is_flag=True, default=False,
              help="Open a browser to log in to TikTok and save session cookies, then exit.")
@click.option("--until-winner", is_flag=True, default=False,
              help="Keep searching until at least one winner is found (see --keyword-cap).")
@click.option("--keyword-cap", default=500, show_default=True, type=int,
              help="Max total keywords when --until-winner is active.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Debug logging.")
@click.option("--discord-webhook", default=None, envvar="DISCORD_WEBHOOK_URL",
              help="Discord webhook URL for winner notifications (or set DISCORD_WEBHOOK_URL in .env).")
def main(
    countries, days, min_ads, min_followers, max_followers, max_total_ads,
    min_active_ratio,
    niche, keywords, max_keywords, keyword_depth, max_ads_per_keyword,
    video_only, require_shop_now, headless, output, no_csv,
    facebook, tiktok, tiktok_login, state_file, reset, verbose, until_winner,
    keyword_cap, discord_webhook,
):
    """MetaGatherer: Find winning ecommerce products in the Facebook Ads Library."""
    _setup_logging(verbose)

    # ── TikTok login flow (standalone, exits after saving cookies) ─────────────
    if tiktok_login:
        console.rule("[bold magenta]TikTok Login[/bold magenta]")
        console.print(
            "  A Chrome window will open. Log in to TikTok in that window.\n"
            "  Your session will be saved to [cyan]tiktok_cookies.json[/cyan] "
            "and reused on future scans.\n"
        )
        from fb_ads_scraper.tiktok import do_tiktok_login
        do_tiktok_login(headless=False)
        return

    from fb_ads_scraper.scraper import FBAdsScraper
    from fb_ads_scraper.output import (
        print_summary_banner, print_results_table, export_csv,
        print_tiktok_results, export_tiktok_csv,
    )
    from fb_ads_scraper.state import load_state, save_state, state_summary, DEFAULT_STATE_FILE
    import os

    console.rule("[bold cyan]MetaGatherer — FB Ads Library Scanner[/bold cyan]")
    if niche:
        console.print(f"  Niche: [bold magenta]{niche}[/bold magenta]  (AI will generate seed keywords)")
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

    if not headless and facebook:
        console.print(
            "[dim]A Chrome window will open and navigate the public Ads Library. "
            "Don't close it while scanning.[/dim]\n"
        )

    from fb_ads_scraper.scoring import WINNER_THRESHOLD, NEAR_MISS_THRESHOLD

    all_products = []
    tiktok_keywords: set = set(keywords) if keywords else set()

    # ── Facebook scan ──────────────────────────────────────────────────────────
    if facebook:
        scraper = FBAdsScraper(
            countries=list(countries),
            days=days,
            min_ads=min_ads,
            min_followers=min_followers,
            max_followers=max_followers,
            max_total_ads=max_total_ads,
            min_active_ratio=min_active_ratio,
            prefer_video=True,
            require_shop_now=require_shop_now,
            max_keyword_depth=keyword_depth,
            max_keywords=max_keywords,
            max_ads_per_keyword=max_ads_per_keyword,
            headless=headless,
            state_file=state_file,
            reset=reset,
            niche=niche or None,
        )
        _UNTIL_WINNER_CAP = max(keyword_cap, max_keywords)
        _EXTEND_BY        = max(50, _UNTIL_WINNER_CAP // 10)
        _extra_kws = list(keywords) if keywords else None

        all_products = scraper.run(extra_keywords=_extra_kws)

        if until_winner:
            from fb_ads_scraper.analysis import extract_new_keywords
            while True:
                _winners_so_far = [
                    w for w in all_products if getattr(w, "score", 0) >= WINNER_THRESHOLD
                ]
                if _winners_so_far:
                    break
                _searched = len(scraper._searched_keywords)
                if _searched >= _UNTIL_WINNER_CAP:
                    console.print(
                        f"[yellow]Searched {_searched} keywords "
                        f"({_UNTIL_WINNER_CAP} cap) — no winner found.[/yellow]"
                    )
                    break
                # Generate new keywords from all collected ad bodies
                _all_ads = [a for ads in scraper._page_ads.values() for a in ads]
                _new_kws = extract_new_keywords(
                    _all_ads, scraper._searched_keywords, max_new=_EXTEND_BY
                )
                if not _new_kws:
                    console.print("[yellow]No new keywords to explore — stopping.[/yellow]")
                    break
                scraper.max_keywords = min(_searched + _EXTEND_BY, _UNTIL_WINNER_CAP)
                console.print(
                    f"[cyan]No winners yet ({_searched} keywords searched). "
                    f"Extending to {scraper.max_keywords} with "
                    f"{len(_new_kws)} new keywords — continuing...[/cyan]"
                )
                all_products = scraper.run(extra_keywords=_new_kws)

        tiktok_keywords = scraper._searched_keywords

        if video_only:
            all_products = [w for w in all_products if w.is_video]

        winners = [w for w in all_products if getattr(w, "score", 0) >= WINNER_THRESHOLD]
        print_summary_banner(
            total_keywords=len(scraper._searched_keywords),
            total_ads=len(scraper._seen_keys),
            total_pages=len(scraper._page_ads),
            winner_count=len(winners),
        )
        print_results_table(all_products, days)

        exportable = [w for w in all_products if getattr(w, "score", 0) >= NEAR_MISS_THRESHOLD]
        if not no_csv and exportable:
            if output is None:
                output = f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            export_csv(exportable, output)
            # Record exported page IDs so future runs won't re-list them
            for w in exportable:
                scraper._seen_winner_ids.add(w.page_id)
            save_state(state_file, scraper._snapshot_state())

        # ── Save scan history to SQLite ────────────────────────────────────────
        try:
            from fb_ads_scraper.db import save_scan, save_winners
            _scan_id = save_scan(
                niche=niche or "",
                keywords_count=len(scraper._searched_keywords),
                ads_count=len(scraper._seen_keys),
                pages_count=len(scraper._page_ads),
                winner_count=len(winners),
                near_miss_count=max(0, len(exportable) - len(winners)),
            )
            if _scan_id > 0:
                save_winners(_scan_id, exportable, NEAR_MISS_THRESHOLD)
                logger.info(f"Scan history saved to DB (scan_id={_scan_id})")
        except Exception as _dbe:
            logger.debug(f"DB save failed: {_dbe}")

        # ── Discord winner notifications ────────────────────────────────────────
        _webhook = discord_webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")
        if _webhook and exportable:
            try:
                from fb_ads_scraper.discord_notify import notify_winner
                for _w in winners:
                    notify_winner(_webhook, _w, is_winner=True)
                _near = [w for w in exportable if getattr(w, "score", 0) < WINNER_THRESHOLD]
                for _w in _near[:3]:
                    notify_winner(_webhook, _w, is_winner=False)
                logger.info(
                    f"Discord: notified {len(winners)} winner(s) + {min(len(_near),3)} near-miss(es)"
                )
            except Exception as _de:
                logger.warning(f"Discord notify failed: {_de}")
    else:
        console.print("[dim]Facebook scan skipped (--no-facebook).[/dim]\n")
        # Use seed keywords for TikTok when Facebook is skipped
        from fb_ads_scraper.scraper import SEED_KEYWORDS, NICHE_SEED_MAP, _NICHE_ALIASES
        if niche:
            niche_lower = niche.lower()
            niche_seeds = next(
                (kws for k, kws in NICHE_SEED_MAP.items() if k in niche_lower), []
            )
            if not niche_seeds:
                words = niche_lower.split()
                alias = next((_NICHE_ALIASES[w] for w in words if w in _NICHE_ALIASES), None)
                niche_seeds = NICHE_SEED_MAP.get(alias, []) if alias else []
            tiktok_keywords = set(niche_seeds) | set(keywords)
        if not tiktok_keywords:
            tiktok_keywords = set(SEED_KEYWORDS[:10])

    # ── TikTok scan ────────────────────────────────────────────────────────────
    if tiktok:
        console.rule("[bold magenta]TikTok Scan[/bold magenta]")
        console.print(
            f"  Scanning TikTok for [bold]{len(tiktok_keywords)}[/bold] "
            f"keywords  •  ≥50k views  •  last 30 days\n"
        )
        from fb_ads_scraper.tiktok import run_tiktok_scan
        tiktok_results = run_tiktok_scan(
            keywords=tiktok_keywords,
            headless=headless,
        )
        print_tiktok_results(tiktok_results)
        if not no_csv and tiktok_results:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            tt_path = (output or f"results_{ts}").replace(".csv", "") + "_tiktok.csv"
            export_tiktok_csv(tiktok_results, tt_path)


if __name__ == "__main__":
    main()
