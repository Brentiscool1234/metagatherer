"""
Core scraping orchestration.

BFS keyword expansion:
  Seed keywords → search ads → extract new keywords → repeat up to depth limit.

For each discovered page:
  - Fetch page follower count
  - Filter by follower range (10–2000 by default)
  - Group page ads into product clusters
  - Flag clusters with >= min_ads active ads started within `days` days
  - Check for Shop Now CTA
  - Run Shopify detection on the page website
"""

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from .api import FBApiClient, FBApiError
from .analysis import (
    cluster_page_ads,
    extract_new_keywords,
    has_shop_now_cta,
    ad_started_within_days,
    get_ad_text,
    ECOMMERCE_SIGNALS,
)
from .shopify import is_shopify_store, extract_url_from_ad

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


@dataclass
class WinningProduct:
    page_id: str
    page_name: str
    page_followers: int
    page_url: str
    page_category: str
    ad_count: int                  # ads in winning cluster (within lookback window)
    total_page_ads: int            # all active ads seen for this page
    has_shop_now: bool
    is_shopify: bool
    shopify_reason: str
    is_video: bool
    ad_start_dates: list[str]      # ISO dates of cluster ads
    sample_ad_title: str
    sample_ad_body: str
    sample_snapshot_url: str
    publisher_platforms: list[str]
    keywords_matched: list[str]    # search terms that found this page
    languages: list[str]
    impressions_range: str
    spend_range: str
    currency: str

    def to_dict(self) -> dict:
        return {
            "page_name": self.page_name,
            "page_id": self.page_id,
            "page_followers": self.page_followers,
            "page_url": self.page_url,
            "page_category": self.page_category,
            "winning_ad_count": self.ad_count,
            "total_page_ads_seen": self.total_page_ads,
            "has_shop_now_cta": self.has_shop_now,
            "is_shopify_store": self.is_shopify,
            "shopify_detection_reason": self.shopify_reason,
            "video_ads_present": self.is_video,
            "ad_start_dates": "; ".join(self.ad_start_dates),
            "earliest_ad_start": min(self.ad_start_dates) if self.ad_start_dates else "",
            "latest_ad_start": max(self.ad_start_dates) if self.ad_start_dates else "",
            "sample_ad_title": self.sample_ad_title,
            "sample_ad_body": self.sample_ad_body,
            "snapshot_url": self.sample_snapshot_url,
            "publisher_platforms": ", ".join(self.publisher_platforms),
            "keywords_matched": ", ".join(self.keywords_matched),
            "languages": ", ".join(self.languages),
            "impressions_range": self.impressions_range,
            "spend_range": self.spend_range,
            "currency": self.currency,
        }


class FBAdsScraper:
    def __init__(
        self,
        client: FBApiClient,
        countries: list[str] = None,
        days: int = 7,
        min_ads: int = 12,
        min_followers: int = 10,
        max_followers: int = 2000,
        prefer_video: bool = True,
        require_shop_now: bool = True,
        max_keyword_depth: int = 3,
        max_keywords: int = 30,
        api_pages_per_keyword: int = 3,
    ):
        self.client = client
        self.countries = countries or ["US"]
        self.days = days
        self.min_ads = min_ads
        self.min_followers = min_followers
        self.max_followers = max_followers
        self.prefer_video = prefer_video
        self.require_shop_now = require_shop_now
        self.max_keyword_depth = max_keyword_depth
        self.max_keywords = max_keywords
        self.api_pages_per_keyword = api_pages_per_keyword

        # Track all ads collected, keyed by page_id
        self._page_ads: dict[str, list[dict]] = defaultdict(list)
        # Track which keywords matched each page
        self._page_keywords: dict[str, set[str]] = defaultdict(set)
        # Seen ad IDs to avoid double-counting
        self._seen_ad_ids: set[str] = set()
        # Seen keywords
        self._searched_keywords: set[str] = set()

    def run(self, extra_keywords: list[str] = None) -> list[WinningProduct]:
        """
        Execute BFS keyword expansion, collect ads, and return winning products.
        """
        seeds = list(SEED_KEYWORDS)
        if extra_keywords:
            seeds = list(extra_keywords) + seeds

        queue: deque[tuple[str, int]] = deque((kw, 0) for kw in seeds)
        total_keywords_searched = 0

        logger.info(f"Starting BFS with {len(seeds)} seed keywords.")

        while queue and total_keywords_searched < self.max_keywords:
            keyword, depth = queue.popleft()
            if keyword in self._searched_keywords:
                continue
            self._searched_keywords.add(keyword)
            total_keywords_searched += 1

            logger.info(f"[depth={depth}] Searching: '{keyword}' ({total_keywords_searched}/{self.max_keywords})")

            # Fetch both video and all ads (video preferred but not exclusive)
            new_ads = self._fetch_ads(keyword)

            if not new_ads:
                continue

            # Index ads by page
            for ad in new_ads:
                ad_id = ad.get("id")
                if not ad_id or ad_id in self._seen_ad_ids:
                    continue
                self._seen_ad_ids.add(ad_id)
                page_id = ad.get("page_id", "unknown")
                self._page_ads[page_id].append(ad)
                self._page_keywords[page_id].add(keyword)

            # Expand keywords from this batch if not at depth limit
            if depth < self.max_keyword_depth:
                new_kws = extract_new_keywords(new_ads, self._searched_keywords, max_new=8)
                for kw in new_kws:
                    if kw not in self._searched_keywords:
                        queue.append((kw, depth + 1))
                        logger.debug(f"  → queued new keyword: '{kw}'")

        logger.info(
            f"Collection done. {len(self._page_ads)} unique pages, "
            f"{len(self._seen_ad_ids)} unique ads."
        )

        return self._evaluate_pages()

    def _fetch_ads(self, keyword: str) -> list[dict]:
        """Fetch ads for a keyword. Tries video first, then all if video count is low."""
        all_ads = []
        try:
            if self.prefer_video:
                video_ads = self.client.search_ads(
                    search_terms=keyword,
                    countries=self.countries,
                    active_status="ACTIVE",
                    media_type="VIDEO",
                    max_pages=self.api_pages_per_keyword,
                )
                all_ads.extend(video_ads)

            # Also fetch non-video to ensure we don't miss pages
            other_ads = self.client.search_ads(
                search_terms=keyword,
                countries=self.countries,
                active_status="ACTIVE",
                media_type=None,
                max_pages=self.api_pages_per_keyword,
            )
            # Merge, deduping by id
            existing_ids = {a.get("id") for a in all_ads}
            all_ads.extend(a for a in other_ads if a.get("id") not in existing_ids)

        except FBApiError as e:
            logger.error(f"FB API rejected request for '{keyword}': {e}")
            logger.error(
                "If you see 'Invalid OAuth' → your token expired. "
                "If you see 'identity' or 'verification' → visit "
                "https://www.facebook.com/ads/library/api/ and complete verification."
            )
        except Exception as e:
            logger.error(f"Error fetching ads for '{keyword}': {e}")

        return all_ads

    def _evaluate_pages(self) -> list[WinningProduct]:
        """
        For each page, check follower count and find winning product clusters.
        """
        winners: list[WinningProduct] = []
        total_pages = len(self._page_ads)

        for idx, (page_id, ads) in enumerate(self._page_ads.items(), 1):
            logger.info(f"Evaluating page {idx}/{total_pages}: {page_id} ({len(ads)} ads)")

            # Fetch page info
            page_info = self.client.get_page_info(page_id)
            fan_count = page_info.get("fan_count", 0) or 0

            # Follower filter
            if not (self.min_followers <= fan_count <= self.max_followers):
                logger.debug(
                    f"  Skipping {page_id}: {fan_count} followers "
                    f"(need {self.min_followers}–{self.max_followers})"
                )
                continue

            # Only look at ads within the lookback window
            recent_ads = [a for a in ads if ad_started_within_days(a, self.days)]
            if not recent_ads:
                logger.debug(f"  Skipping {page_id}: no ads within last {self.days} days")
                continue

            # Cluster similar ads
            clusters = cluster_page_ads(recent_ads)

            for cluster in clusters:
                if len(cluster) < self.min_ads:
                    continue

                # Require Shop Now CTA (at least on a majority of cluster ads)
                shop_now_count = sum(1 for a in cluster if has_shop_now_cta(a))
                if self.require_shop_now and shop_now_count == 0:
                    logger.debug(f"  Cluster skipped: no Shop Now CTA detected")
                    continue

                # Determine if any ad is video
                is_video = any(
                    (a.get("media_type") or "").upper() == "VIDEO"
                    for a in cluster
                )

                # Shopify check — use page website first, then ad URLs
                page_website = page_info.get("website") or ""
                shopify_url = page_website

                if not shopify_url:
                    for ad in cluster:
                        shopify_url = extract_url_from_ad(ad)
                        if shopify_url:
                            break

                is_shopify, shopify_reason = False, "no url available"
                if shopify_url:
                    is_shopify, shopify_reason = is_shopify_store(shopify_url)

                # Collect metadata
                sample_ad = cluster[0]
                titles = sample_ad.get("ad_creative_link_titles") or []
                bodies = sample_ad.get("ad_creative_bodies") or []
                sample_title = titles[0] if titles else ""
                sample_body = bodies[0] if bodies else ""

                start_dates = []
                for a in cluster:
                    raw = a.get("ad_delivery_start_time") or a.get("ad_creation_time") or ""
                    if raw:
                        start_dates.append(raw[:10])  # YYYY-MM-DD

                platforms: set[str] = set()
                langs: set[str] = set()
                for a in cluster:
                    pp = a.get("publisher_platforms") or []
                    if isinstance(pp, list):
                        platforms.update(pp)
                    ll = a.get("languages") or []
                    if isinstance(ll, list):
                        langs.update(ll)

                imp_range = "n/a"   # requires Meta Research API access
                spend_range = "n/a"  # requires Meta Research API access

                winner = WinningProduct(
                    page_id=page_id,
                    page_name=page_info.get("name") or sample_ad.get("page_name") or page_id,
                    page_followers=fan_count,
                    page_url=page_website or f"https://www.facebook.com/{page_id}",
                    page_category=page_info.get("category") or "",
                    ad_count=len(cluster),
                    total_page_ads=len(ads),
                    has_shop_now=shop_now_count > 0,
                    is_shopify=is_shopify,
                    shopify_reason=shopify_reason,
                    is_video=is_video,
                    ad_start_dates=sorted(set(start_dates)),
                    sample_ad_title=sample_title,
                    sample_ad_body=sample_body[:300],
                    sample_snapshot_url=sample_ad.get("ad_snapshot_url") or "",
                    publisher_platforms=sorted(platforms),
                    keywords_matched=sorted(self._page_keywords.get(page_id, set())),
                    languages=sorted(langs),
                    impressions_range=imp_range,
                    spend_range=spend_range,
                    currency=sample_ad.get("currency") or "",
                )
                winners.append(winner)
                logger.info(
                    f"  ✓ WINNER: {winner.page_name} | "
                    f"{winner.ad_count} ads | "
                    f"{fan_count} followers | "
                    f"Shopify={is_shopify}"
                )

        # Sort: most ads first, then video > non-video, then Shopify stores first
        winners.sort(key=lambda w: (-w.ad_count, not w.is_video, not w.is_shopify))
        logger.info(f"\nFound {len(winners)} winning product pages.")
        return winners
