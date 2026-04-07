"""
Core scraping orchestration — Selenium browser-based, no API key required.

BFS keyword expansion:
  Seed keywords → scrape public Ads Library → extract new keywords → repeat.

Filtering:
  - Page follower count: 10–2000 (scraped from ad cards)
  - Product clustering: group ads from same page by keyword overlap
  - Winning threshold: >= min_ads ads in cluster within `days`-day window
  - Shop Now CTA detection
  - Shopify store verification
"""

import logging
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional

from .browser import AdsLibraryBrowser, parse_follower_count, parse_date_text
from .analysis import cluster_page_ads, extract_new_keywords, has_shop_now_cta, extract_keywords
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


def _to_standard_ad(raw: dict, keyword: str) -> dict:
    """Convert a browser-scraped ad dict to the schema analysis.py expects."""
    page_url = raw.get("page_url", "")
    page_id = page_url.split("facebook.com/")[-1].split("?")[0].strip("/") if page_url else "unknown"
    return {
        "id": raw.get("_key", ""),
        "page_id": page_id,
        "page_name": raw.get("page_name", ""),
        "page_url": page_url,
        "ad_creative_bodies": [raw.get("ad_body", "")],
        "ad_creative_link_captions": [raw.get("cta_button", "")],
        "ad_creative_link_titles": [],
        "ad_creative_link_descriptions": [],
        "ad_snapshot_url": raw.get("snapshot_url", ""),
        "media_type": "VIDEO" if raw.get("has_video") else "IMAGE",
        "publisher_platforms": ["facebook"],
        "languages": [],
        "_follower_count": parse_follower_count(raw.get("follower_text", "")),
        "_has_shop_now": raw.get("has_shop_now", False),
        "_start_date": parse_date_text(raw.get("date_text", "")),
        "_keyword": keyword,
    }


def _within_days(ad: dict, days: int) -> bool:
    start = ad.get("_start_date")
    if not start:
        return True  # no date = assume active/recent
    try:
        dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
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

        self._page_ads: dict[str, list[dict]] = defaultdict(list)
        self._page_keywords: dict[str, set[str]] = defaultdict(set)
        self._seen_keys: set[str] = set()
        self._searched_keywords: set[str] = set()

    def run(self, extra_keywords: list[str] = None) -> list[WinningProduct]:
        seeds = list(SEED_KEYWORDS)
        if extra_keywords:
            seeds = list(extra_keywords) + seeds

        browser = AdsLibraryBrowser(countries=self.countries, headless=self.headless)
        browser.start()

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

                raw_ads = browser.search_keyword(keyword, max_ads=self.max_ads_per_keyword)
                new_ads = []

                for raw in raw_ads:
                    key = raw.get("_key", "")
                    if not key or key in self._seen_keys:
                        continue
                    self._seen_keys.add(key)
                    ad = _to_standard_ad(raw, keyword)
                    page_id = ad.get("page_id", "")
                    if page_id and page_id != "unknown":
                        self._page_ads[page_id].append(ad)
                        self._page_keywords[page_id].add(keyword)
                        new_ads.append(ad)

                # BFS keyword expansion
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
            browser.stop()

        return self._evaluate_pages()

    def _evaluate_pages(self) -> list[WinningProduct]:
        winners = []
        total = len(self._page_ads)

        for idx, (page_id, ads) in enumerate(self._page_ads.items(), 1):
            logger.info(f"Evaluating {idx}/{total}: {page_id} ({len(ads)} ads)")

            counts = [a["_follower_count"] for a in ads if a.get("_follower_count", 0) > 0]
            fan_count = int(sum(counts) / len(counts)) if counts else 0

            if fan_count > 0 and not (self.min_followers <= fan_count <= self.max_followers):
                logger.debug(f"  Skip: {fan_count} followers out of range")
                continue

            recent = [a for a in ads if _within_days(a, self.days)]
            if not recent:
                logger.debug(f"  Skip: no ads within {self.days}d")
                continue

            for cluster in cluster_page_ads(recent):
                if len(cluster) < self.min_ads:
                    continue

                shop_now = sum(1 for a in cluster if a.get("_has_shop_now") or has_shop_now_cta(a))
                if self.require_shop_now and shop_now == 0:
                    continue

                is_video = any((a.get("media_type") or "").upper() == "VIDEO" for a in cluster)

                page_url = next((a["page_url"] for a in cluster if a.get("page_url")), "")
                is_shopify, shopify_reason = (False, "no url")
                if page_url:
                    is_shopify, shopify_reason = is_shopify_store(page_url)

                start_dates = sorted({a["_start_date"] for a in cluster if a.get("_start_date")})
                platforms = sorted({p for a in cluster for p in (a.get("publisher_platforms") or [])})
                sample = cluster[0]
                bodies = sample.get("ad_creative_bodies") or []
                page_name = sample.get("page_name", page_id)

                w = WinningProduct(
                    page_id=page_id,
                    page_name=page_name,
                    page_followers=fan_count,
                    page_url=page_url,
                    ad_count=len(cluster),
                    total_page_ads=len(ads),
                    has_shop_now=shop_now > 0,
                    is_shopify=is_shopify,
                    shopify_reason=shopify_reason,
                    is_video=is_video,
                    ad_start_dates=start_dates,
                    sample_ad_body=(bodies[0][:300] if bodies else ""),
                    sample_snapshot_url=sample.get("ad_snapshot_url", ""),
                    publisher_platforms=platforms,
                    keywords_matched=sorted(self._page_keywords.get(page_id, set())),
                )
                winners.append(w)
                logger.info(
                    f"  ✓ WINNER: {page_name} | {len(cluster)} ads | "
                    f"{fan_count} followers | Shopify={is_shopify}"
                )

        winners.sort(key=lambda w: (-w.ad_count, not w.is_video, not w.is_shopify))
        logger.info(f"Found {len(winners)} winning products.")
        return winners
