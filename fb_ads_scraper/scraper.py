"""
Core scraping orchestration — browser-based (no API key required).

BFS keyword expansion:
  Seed keywords → scrape public Ads Library → extract new keywords → repeat.

For each discovered page:
  - Filter by follower range (10–2000 by default)
  - Group page ads into product clusters by keyword overlap
  - Flag clusters with >= min_ads active ads started within `days` days
  - Check for Shop Now CTA
  - Run Shopify detection on the page website
"""

import logging
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional

from .browser import (
    AdsLibraryBrowser,
    parse_follower_count,
    parse_date_text,
    run_search,
    create_browser,
    close_browser,
)
from .analysis import (
    cluster_page_ads,
    extract_new_keywords,
    has_shop_now_cta,
    get_ad_text,
    extract_keywords,
)
from .shopify import is_shopify_store

logger = logging.getLogger(__name__)

SEED_KEYWORDS = [
    "buy now",
    "shop now",
    "order now",
    "free shipping",
    "limited time offer",
    "get yours today",
    "flash sale",
    "ships worldwide",
    "add to cart",
    "exclusive deal",
]


def _browser_ad_to_standard(raw: dict, keyword: str) -> dict:
    """
    Convert a raw browser-scraped ad dict into the same schema that
    analysis.py expects (mirroring the old Graph API field names).
    """
    return {
        "id": raw.get("_key", ""),
        "page_id": raw.get("page_url", "").split("facebook.com/")[-1].split("?")[0],
        "page_name": raw.get("page_name", ""),
        "page_url": raw.get("page_url", ""),
        "ad_creative_bodies": [raw.get("ad_body", "")],
        "ad_creative_link_captions": [raw.get("cta_button", "")],
        "ad_creative_link_titles": [],
        "ad_creative_link_descriptions": [],
        "ad_snapshot_url": raw.get("snapshot_url", ""),
        "media_type": "VIDEO" if raw.get("has_video") else "IMAGE",
        "publisher_platforms": ["facebook"],
        "languages": [],
        # Browser-only fields
        "_follower_text": raw.get("follower_text", ""),
        "_follower_count": parse_follower_count(raw.get("follower_text", "")),
        "_has_shop_now": raw.get("has_shop_now", False),
        "_date_text": raw.get("date_text", ""),
        "_start_date": parse_date_text(raw.get("date_text", "") or ""),
        "_keyword": keyword,
    }


def _within_days(ad: dict, days: int) -> bool:
    """Return True if the ad's start date is within the last `days` days."""
    start = ad.get("_start_date")
    if not start:
        # If we couldn't parse a date, include it (assume active)
        return True
    try:
        dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return dt >= cutoff
    except ValueError:
        return True


class WinningProduct:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def to_dict(self) -> dict:
        return {
            "page_name": self.page_name,
            "page_id": self.page_id,
            "page_followers": self.page_followers,
            "page_url": self.page_url,
            "winning_ad_count": self.ad_count,
            "total_page_ads_seen": self.total_page_ads,
            "has_shop_now_cta": self.has_shop_now,
            "is_shopify_store": self.is_shopify,
            "shopify_detection_reason": self.shopify_reason,
            "video_ads_present": self.is_video,
            "ad_start_dates": "; ".join(self.ad_start_dates),
            "earliest_ad_start": min(self.ad_start_dates) if self.ad_start_dates else "",
            "latest_ad_start": max(self.ad_start_dates) if self.ad_start_dates else "",
            "sample_ad_body": self.sample_ad_body,
            "snapshot_url": self.sample_snapshot_url,
            "publisher_platforms": ", ".join(self.publisher_platforms),
            "keywords_matched": ", ".join(self.keywords_matched),
        }


class FBAdsScraper:
    def __init__(
        self,
        countries: list[str] = None,
        days: int = 7,
        min_ads: int = 12,
        min_followers: int = 10,
        max_followers: int = 2000,
        prefer_video: bool = True,
        require_shop_now: bool = True,
        max_keyword_depth: int = 3,
        max_keywords: int = 30,
        max_ads_per_keyword: int = 120,
        headless: bool = False,
    ):
        self.countries = countries or ["US"]
        self.days = days
        self.min_ads = min_ads
        self.min_followers = min_followers
        self.max_followers = max_followers
        self.prefer_video = prefer_video
        self.require_shop_now = require_shop_now
        self.max_keyword_depth = max_keyword_depth
        self.max_keywords = max_keywords
        self.max_ads_per_keyword = max_ads_per_keyword
        self.headless = headless

        # page_id → list of standardized ad dicts
        self._page_ads: dict[str, list[dict]] = defaultdict(list)
        # page_id → set of matched keywords
        self._page_keywords: dict[str, set[str]] = defaultdict(set)
        # seen ad _keys to avoid double-counting
        self._seen_keys: set[str] = set()
        # keywords already searched
        self._searched_keywords: set[str] = set()

        self._browser: Optional[AdsLibraryBrowser] = None

    def run(self, extra_keywords: list[str] = None) -> list["WinningProduct"]:
        seeds = list(SEED_KEYWORDS)
        if extra_keywords:
            seeds = list(extra_keywords) + seeds

        logger.info(f"Starting browser...")
        self._browser = create_browser(self.countries, headless=self.headless)

        try:
            queue: deque[tuple[str, int]] = deque((kw, 0) for kw in seeds)
            total = 0

            while queue and total < self.max_keywords:
                keyword, depth = queue.popleft()
                if keyword in self._searched_keywords:
                    continue
                self._searched_keywords.add(keyword)
                total += 1

                logger.info(f"[depth={depth}] Searching: '{keyword}' ({total}/{self.max_keywords})")

                raw_ads = run_search(self._browser, keyword, max_ads=self.max_ads_per_keyword)
                new_ads = []

                for raw in raw_ads:
                    key = raw.get("_key", "")
                    if not key or key in self._seen_keys:
                        continue
                    self._seen_keys.add(key)
                    ad = _browser_ad_to_standard(raw, keyword)
                    page_id = ad.get("page_id", "unknown")
                    if page_id and page_id != "unknown":
                        self._page_ads[page_id].append(ad)
                        self._page_keywords[page_id].add(keyword)
                        new_ads.append(ad)

                # Keyword expansion
                if depth < self.max_keyword_depth and new_ads:
                    new_kws = extract_new_keywords(new_ads, self._searched_keywords, max_new=6)
                    for kw in new_kws:
                        if kw not in self._searched_keywords:
                            queue.append((kw, depth + 1))
                            logger.debug(f"  → queued: '{kw}'")

            logger.info(
                f"Collection done. {len(self._page_ads)} pages, "
                f"{len(self._seen_keys)} unique ads."
            )
        except KeyboardInterrupt:
            logger.warning("Interrupted — evaluating partial results...")
        finally:
            if self._browser:
                close_browser(self._browser)

        return self._evaluate_pages()

    def _evaluate_pages(self) -> list["WinningProduct"]:
        winners = []
        total = len(self._page_ads)

        for idx, (page_id, ads) in enumerate(self._page_ads.items(), 1):
            logger.info(f"Evaluating page {idx}/{total}: {page_id} ({len(ads)} ads)")

            # Use the follower count from the ads (scraped from the page card)
            follower_counts = [a.get("_follower_count", 0) for a in ads if a.get("_follower_count", 0) > 0]
            fan_count = int(sum(follower_counts) / len(follower_counts)) if follower_counts else 0

            if fan_count > 0 and not (self.min_followers <= fan_count <= self.max_followers):
                logger.debug(f"  Skip {page_id}: {fan_count} followers (need {self.min_followers}–{self.max_followers})")
                continue

            # Recency filter
            recent_ads = [a for a in ads if _within_days(a, self.days)]
            if not recent_ads:
                logger.debug(f"  Skip {page_id}: no ads within {self.days}d window")
                continue

            # Cluster by product similarity
            clusters = cluster_page_ads(recent_ads)

            for cluster in clusters:
                if len(cluster) < self.min_ads:
                    continue

                # Shop Now requirement
                shop_now_count = sum(1 for a in cluster if a.get("_has_shop_now") or has_shop_now_cta(a))
                if self.require_shop_now and shop_now_count == 0:
                    logger.debug(f"  Cluster skipped: no Shop Now CTA")
                    continue

                is_video = any((a.get("media_type") or "").upper() == "VIDEO" for a in cluster)
                if self.prefer_video and not is_video:
                    # Don't hard-exclude non-video, just note it
                    pass

                # Shopify check — use the page URL from the ads
                page_url = next((a.get("page_url", "") for a in cluster if a.get("page_url")), "")
                is_shopify, shopify_reason = False, "no url"
                if page_url:
                    is_shopify, shopify_reason = is_shopify_store(page_url)

                # Collect dates
                start_dates = []
                for a in cluster:
                    d = a.get("_start_date")
                    if d:
                        start_dates.append(d)

                platforms: set[str] = set()
                for a in cluster:
                    pp = a.get("publisher_platforms") or []
                    if isinstance(pp, list):
                        platforms.update(pp)

                sample = cluster[0]
                bodies = sample.get("ad_creative_bodies") or []
                sample_body = bodies[0][:300] if bodies else ""

                page_name = sample.get("page_name", page_id)

                winner = WinningProduct(
                    page_id=page_id,
                    page_name=page_name,
                    page_followers=fan_count,
                    page_url=page_url,
                    ad_count=len(cluster),
                    total_page_ads=len(ads),
                    has_shop_now=shop_now_count > 0,
                    is_shopify=is_shopify,
                    shopify_reason=shopify_reason,
                    is_video=is_video,
                    ad_start_dates=sorted(set(start_dates)),
                    sample_ad_body=sample_body,
                    sample_snapshot_url=sample.get("ad_snapshot_url", ""),
                    publisher_platforms=sorted(platforms),
                    keywords_matched=sorted(self._page_keywords.get(page_id, set())),
                )
                winners.append(winner)
                logger.info(
                    f"  ✓ WINNER: {page_name} | {len(cluster)} ads | "
                    f"{fan_count} followers | Shopify={is_shopify}"
                )

        winners.sort(key=lambda w: (-w.ad_count, not w.is_video, not w.is_shopify))
        logger.info(f"\nFound {len(winners)} winning product pages.")
        return winners
