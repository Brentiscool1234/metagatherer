"""
Rich terminal dashboard and CSV export.
"""

import csv
import os

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.align import Align
from rich.columns import Columns
from rich.text import Text
from rich import box

from .scraper import WinningProduct
from .scoring import CRITERIA, WINNER_THRESHOLD, NEAR_MISS_THRESHOLD

console = Console()

_SCORE_BAR_WIDTH = 20


def _score_bar(score: float) -> str:
    filled = round((score / 10.0) * _SCORE_BAR_WIDTH)
    filled = max(0, min(filled, _SCORE_BAR_WIDTH))
    empty = _SCORE_BAR_WIDTH - filled
    if score >= WINNER_THRESHOLD:
        color = "bright_green"
    elif score >= NEAR_MISS_THRESHOLD:
        color = "yellow"
    else:
        color = "red"
    bar = "█" * filled + "░" * empty
    return f"[{color}]{bar} {score:.1f}/10[/{color}]"


def _bool_icon(val: bool) -> str:
    return "[green]✓[/green]" if val else "[dim]✗[/dim]"


def _breakdown_line(breakdown: dict) -> str:
    parts = []
    for key, (label, max_pts) in CRITERIA.items():
        earned = breakdown.get(key, 0.0)
        if earned >= max_pts:
            color = "green"
        elif earned > 0:
            color = "yellow"
        else:
            color = "dim"
        parts.append(f"[{color}]{label}: {earned:.1f}/{max_pts:.1f}[/{color}]")
    return "  ".join(parts)


def print_summary_banner(
    total_keywords: int, total_ads: int, total_pages: int, winner_count: int
):
    stats = (
        f"[bold]Keywords:[/bold] {total_keywords}   "
        f"[bold]Ads collected:[/bold] {total_ads}   "
        f"[bold]Pages evaluated:[/bold] {total_pages}   "
        f"[bold cyan]Winners (≥{WINNER_THRESHOLD}):[/bold cyan] {winner_count}"
    )
    console.print(Panel(
        Align.center(stats),
        title="[bold cyan]MetaGatherer — Scan Complete[/bold cyan]",
        border_style="cyan",
    ))


def print_results_table(products: list[WinningProduct], days: int):
    """Full dashboard: winners table + near-misses + detail cards for top 5."""
    if not products:
        console.print("\n[yellow]No products found. Try --reset and a more specific niche.[/yellow]\n")
        return

    winners = [w for w in products if getattr(w, "score", 0) >= WINNER_THRESHOLD]
    near_misses = [
        w for w in products
        if NEAR_MISS_THRESHOLD <= getattr(w, "score", 0) < WINNER_THRESHOLD
    ]

    # ── Winners table ────────────────────────────────────────────
    if winners:
        console.print()
        console.rule(f"[bold bright_green]  WINNERS  ({len(winners)} products, score ≥ {WINNER_THRESHOLD})  [/bold bright_green]")
        _render_table(winners, show_rank=True)

        console.print("[bold cyan]── Score Breakdown (Top 5) ──[/bold cyan]")
        for i, w in enumerate(winners[:5], 1):
            _print_detail_card(w, rank=i)
    else:
        console.print("\n[yellow]No products scored ≥ 7.0. See near-misses below.[/yellow]")

    # ── Near-misses table ────────────────────────────────────────
    if near_misses:
        console.print()
        console.rule(f"[bold yellow]  NEAR MISSES  ({len(near_misses)} products, score {NEAR_MISS_THRESHOLD}–{WINNER_THRESHOLD - 0.1:.1f})  [/bold yellow]")
        _render_table(near_misses, show_rank=False)

    console.print()


def _ads_library_url(page_id: str, country: str = "US") -> str:
    """Build a direct link to all active ads for this page in the Ads Library."""
    from urllib.parse import urlencode
    if page_id and page_id.isdigit():
        params = {
            "active_status": "active", "ad_type": "all",
            "country": country, "search_type": "page",
            "view_all_page_id": page_id,
        }
    else:
        params = {
            "active_status": "active", "ad_type": "all",
            "country": country, "q": page_id, "search_type": "page",
        }
    return "https://www.facebook.com/ads/library/?" + urlencode(params)


def _render_table(products: list[WinningProduct], show_rank: bool):
    table = Table(box=box.ROUNDED, show_lines=True, expand=True)

    if show_rank:
        table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Page / Store", style="bold", min_width=18)
    table.add_column("Score", min_width=26)
    table.add_column("Ads", justify="center", width=5)
    table.add_column("Followers", justify="right", width=11)
    table.add_column("Vid", justify="center", width=4)
    table.add_column("Shopify", justify="center", width=8)
    table.add_column("Store URL", min_width=22, overflow="fold")
    table.add_column("Ad Library", min_width=12, overflow="fold")

    for i, w in enumerate(products, 1):
        score = getattr(w, "score", 0.0)
        bar = _score_bar(score)

        fol = w.page_followers
        fol_str = (
            "[green]" + f"{fol:,}" + "[/green]" if 10 <= fol <= 500
            else "[yellow]" + f"{fol:,}" + "[/yellow]" if fol > 0
            else "[dim]?[/dim]"
        )

        ad_color = "bright_green" if w.ad_count >= 20 else "green" if w.ad_count >= 12 else "yellow"
        ad_str = f"[{ad_color}]{w.ad_count}[/{ad_color}]"

        shopify_icon = (
            "[bright_green]✓✓[/bright_green]" if getattr(w, "shopify_confirmed_via_browser", False)
            else "[green]✓[/green]" if w.is_shopify
            else "[dim]✗[/dim]"
        )

        store = getattr(w, "store_url", "") or w.page_url or "—"
        if len(store) > 40:
            store = store[:37] + "..."

        lib_url = _ads_library_url(w.page_id)
        lib_link = f"[cyan][link={lib_url}]View ads ↗[/link][/cyan]"

        page_display = f"[link={w.page_url}]{w.page_name}[/link]" if w.page_url else w.page_name

        row = [page_display, bar, ad_str, fol_str,
               _bool_icon(w.is_video),
               shopify_icon, store, lib_link]
        if show_rank:
            row = [str(i)] + row
        table.add_row(*row)

    console.print(table)


def _print_detail_card(w: WinningProduct, rank: int):
    score = getattr(w, "score", 0.0)
    breakdown = getattr(w, "score_breakdown", {})
    fol_str = f"{w.page_followers:,}" if w.page_followers > 0 else "unknown"
    store = getattr(w, "store_url", "") or w.page_url or "—"

    shopify_str = "✓ (browser-verified)" if getattr(w, "shopify_confirmed_via_browser", False) \
        else "✓ (HTTP headers)" if w.is_shopify \
        else f"✗  ({w.shopify_reason})"

    lib_url = _ads_library_url(w.page_id)
    snapshot = getattr(w, "sample_snapshot_url", "") or ""

    # Sourcing summary line
    sourcing = getattr(w, "sourcing_data", {}) or {}
    if sourcing.get("aliexpress_found"):
        ali_min = sourcing.get("aliexpress_min_price", "?")
        ali_max = sourcing.get("aliexpress_max_price", "?")
        margin = sourcing.get("margin_pct")
        be_roas = sourcing.get("break_even_roas")
        store_p = sourcing.get("shopify_product_price")
        confidence = sourcing.get("aliexpress_match_confidence", 0) or 0
        conf_str = (
            "[bright_green]high[/bright_green]" if confidence >= 0.75
            else "[yellow]medium[/yellow]" if confidence >= 0.5
            else "[dim]low[/dim]"
        )
        viable_color = "bright_green" if sourcing.get("margin_viable") else "red"
        margin_str = (
            f"[{viable_color}]{margin}% margin[/{viable_color}]"
            f" | break-even ROAS: {be_roas}x"
            if margin is not None else "margin: n/a"
        )
        ali_url = sourcing.get("aliexpress_product_url", "")
        shop_url = sourcing.get("shopify_product_url", "")
        ali_link = (f" [cyan][link={ali_url}]↗ AliExpress listing[/link][/cyan]"
                    if ali_url else "")
        shop_link = (f"  [cyan][link={shop_url}]↗ product page[/link][/cyan]"
                     if shop_url else "")
        sourcing_line = (
            f"[bold]Sourcing:[/bold]  AliExpress ${ali_min}–${ali_max} "
            f"({sourcing.get('aliexpress_suppliers', '?')} suppliers, "
            f"confidence: {conf_str}){ali_link}"
            f"\n           Store price: ${store_p or '?'}{shop_link}  →  {margin_str}"
        )
    elif sourcing.get("aliexpress_search_term"):
        sourcing_line = (
            f"[bold]Sourcing:[/bold]  [dim]AliExpress: not found "
            f"(searched: {sourcing.get('aliexpress_search_term', '?')})[/dim]"
        )
    else:
        sourcing_line = ""

    lines = [
        f"[bold]{_score_bar(score)}[/bold]",
        f"[dim]{'─' * 50}[/dim]",
        f"[bold]Page:[/bold]      {w.page_name}  •  {fol_str} followers",
        f"[bold]Page URL:[/bold]  [cyan][link={w.page_url}]{w.page_url or '—'}[/link][/cyan]",
        f"[bold]Store URL:[/bold] [cyan][link={store}]{store}[/link][/cyan]",
        f"[bold]All ads ↗:[/bold] [cyan][link={lib_url}]{lib_url}[/link][/cyan]",
        (f"[bold]Ad snap ↗:[/bold] [cyan][link={snapshot}]{snapshot[:80]}[/link][/cyan]"
         if snapshot else ""),
        f"[bold]Ads:[/bold]       {w.ad_count} active  (total on page: {w.total_page_ads})",
        f"[bold]Shopify:[/bold]   {shopify_str}",
        sourcing_line,
        f"[bold]Platforms:[/bold] {', '.join(w.publisher_platforms) or '—'}",
        f"[bold]Ad dates:[/bold]  {', '.join(w.ad_start_dates[:5]) or '—'}{'...' if len(w.ad_start_dates) > 5 else ''}",
        f"[bold]Keywords:[/bold]  [italic]{', '.join(w.keywords_matched[:6]) or '—'}[/italic]",
        "",
        f"[bold]Score breakdown:[/bold]",
        f"  {_breakdown_line(breakdown)}",
        "",
        f"[dim]Sample ad:[/dim] {(w.sample_ad_body or '—')[:200]}",
    ]
    lines = [l for l in lines if l]  # drop empty optional lines
    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]#{rank} — {w.page_name}[/bold]",
        border_style="bright_green" if score >= WINNER_THRESHOLD else "yellow",
        expand=False,
    ))
    console.print()


def print_tiktok_results(results):
    """Print TikTok results table."""
    if not results:
        console.print("\n[yellow]No TikTok videos found matching the criteria.[/yellow]\n")
        return

    console.print()
    console.rule(
        f"[bold magenta]  TIKTOK RESULTS  "
        f"({len(results)} videos ≥ 50k views, last 30 days)  [/bold magenta]"
    )

    table = Table(box=box.ROUNDED, show_lines=True, expand=True)
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Views", justify="right", min_width=8)
    table.add_column("@Creator", min_width=14)
    table.add_column("Shopify", justify="center", width=8)
    table.add_column("Store URL", min_width=22, overflow="fold")
    table.add_column("Video Link", min_width=20, overflow="fold")
    table.add_column("Caption", min_width=20, overflow="fold")

    for i, r in enumerate(results, 1):
        views_str = f"[bright_green]{r.views_text}[/bright_green]"
        shopify_icon = (
            "[bright_green]✓[/bright_green]" if r.is_shopify else "[dim]✗[/dim]"
        )
        bio = r.bio_url or "—"
        if len(bio) > 35:
            bio = bio[:32] + "..."

        vid_link = f"[cyan][link={r.video_url}]{r.video_url.split('/')[-1]}…[/link][/cyan]"
        caption = (r.caption or "—")[:60]

        table.add_row(
            str(i), views_str,
            f"@{r.username}" if r.username else "—",
            shopify_icon, bio, vid_link, caption,
        )

    console.print(table)
    console.print()


def export_tiktok_csv(results, path: str):
    """Export TikTok results to CSV."""
    if not results:
        return
    import csv as _csv
    fieldnames = [
        "views", "views_text", "username", "keyword", "upload_date",
        "video_url", "bio_url", "is_shopify", "shopify_reason", "caption",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow({k: getattr(r, k, "") for k in fieldnames})
    console.print(f"[bold green]✓ TikTok results:[/bold green] [cyan]{path}[/cyan]\n")


def export_csv(products: list[WinningProduct], path: str):
    if not products:
        console.print("[yellow]No results to export.[/yellow]")
        return

    rows = []
    for w in products:
        d = w.to_dict()
        breakdown = getattr(w, "score_breakdown", {})
        for key, (label, _) in CRITERIA.items():
            d[f"score_{key}"] = breakdown.get(key, 0.0)
        rows.append(d)

    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    winners_count = sum(1 for w in products if getattr(w, "score", 0) >= WINNER_THRESHOLD)
    console.print(f"\n[bold green]✓ Exported to:[/bold green] [cyan]{os.path.abspath(path)}[/cyan]")
    console.print(f"  {winners_count} winners + {len(products) - winners_count} near-misses, {len(fieldnames)} columns.\n")
