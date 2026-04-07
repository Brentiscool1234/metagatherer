"""
Core scraping orchestration — Selenium browser-based, no API key required.

Two-phase approach:
  Phase 1 — Keyword sweep: search 30 keywords, collect ads, track pages
  Phase 2 — Page verification: visit each promising page's own Ads Library
             view to count ALL their active ads (fixes the 12+ threshold)

AI keyword expansion uses Claude (ANTHROPIC_API_KEY in .env) to suggest
specific product search terms instead of generic frequency-mined words.
"""

import logging
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional

from .browser import AdsLibraryBrowser, parse_follower_count, parse_date_text
from .analysis import cluster_page_ads, extract_new_keywords, has_shop_now_cta
from .ai_keywords import expand_keywords_with_ai, keywords_for_niche
from .shopify import is_shopify_store
from .state import save_state, load_state, state_summary, DEFAULT_STATE_FILE

logger = logging.getLogger(__name__)

# Pages/ads from these platforms are supplier marketplaces, not dropshipping stores
_PLATFORM_BLOCKLIST = {
    "alibaba", "aliexpress", "temu", "amazon", "1688", "dhgate", "shein",
    "wish.com", "banggood",
}

def _is_blocked(raw: dict) -> bool:
    """Return True if this ad is from a supplier marketplace we want to skip."""
    check = " ".join([
        (raw.get("page_name") or "").lower(),
        (raw.get("page_url") or "").lower(),
        (raw.get("ad_body") or "").lower(),
    ])
    return any(term in check for term in _PLATFORM_BLOCKLIST)

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

# Pages seen this many times in Phase 1 get a full verification visit
PAGE_VISIT_THRESHOLD = 2


def _to_standard_ad(raw: dict, keyword: str) -> dict:
    page_url = raw.get("page_url", "")
    page_id = (
        page_url.split("facebook.com/")[-1].split("?")[0].strip("/")
        if page_url else "unknown"
    )
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
        # "N ads use this creative and text" — actual running ad count per card
        "_ad_versions": max(1, int(raw.get("ad_versions", 1) or 1)),
    }


def _within_days(ad: dict, days: int) -> bool:
    start = ad.get("_start_date")
    if not start:
        return True
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
        use_ai: bool = True,
        state_file: str = DEFAULT_STATE_FILE,
        reset: bool = False,
        niche: str = None,
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
        self.use_ai = use_ai
        self.state_file = state_file
        self.niche = niche

        self._page_ads: dict[str, list[dict]] = defaultdict(list)
        self._page_keywords: dict[str, set[str]] = defaultdict(set)
        self._page_followers: dict[str, int] = {}
        self._seen_keys: set[str] = set()
        self._searched_keywords: set[str] = set()

        # Load previous run's state unless reset was requested
        self._resume_queue: list[tuple[str, int]] = []
        if not reset:
            saved = load_state(state_file)
            if saved:
                self._page_ads = saved["page_ads"]
                self._page_keywords = saved["page_keywords"]
                self._page_followers = saved["page_followers"]
                self._seen_keys = saved["seen_keys"]
                self._searched_keywords = saved["searched_keywords"]
                self._resume_queue = saved["queue"]
                logger.info(
                    f"Resuming saved state — {state_summary(saved)}"
                )

    def _snapshot_state(self, queue: deque) -> dict:
        return {
            "searched_keywords": self._searched_keywords,
            "queue": list(queue),
            "page_ads": self._page_ads,
            "page_keywords": self._page_keywords,
            "page_followers": self._page_followers,
            "seen_keys": self._seen_keys,
        }

    def run(self, extra_keywords: list[str] = None) -> list[WinningProduct]:
        # If a niche was given, let AI generate targeted seed keywords for it.
        # These replace the generic seeds (buy now / shop now / etc.) so the
        # BFS starts inside the niche right away.
        if self.niche and self.use_ai:
            niche_kws = keywords_for_niche(self.niche, count=10)
            if niche_kws:
                logger.info(f"Niche '{self.niche}' → AI seeds: {niche_kws}")
                seeds = niche_kws
            else:
                logger.warning(
                    f"AI couldn't generate keywords for niche '{self.niche}' "
                    f"— falling back to generic seeds."
                )
                seeds = list(SEED_KEYWORDS)
        else:
            seeds = list(SEED_KEYWORDS)

        if extra_keywords:
            seeds = list(extra_keywords) + seeds

        browser = AdsLibraryBrowser(countries=self.countries, headless=self.headless)
        browser.start()

        try:
            # ── Phase 1: Keyword sweep ────────────────────────────────────
            logger.info("Phase 1: Keyword sweep")

            # Resume from saved queue if available, otherwise start from seeds.
            # Any extra_keywords not yet searched are prepended.
            if self._resume_queue:
                queue: deque[tuple[str, int]] = deque(self._resume_queue)
                # Add any extra_keywords not already searched
                for kw in reversed(list(extra_keywords or [])):
                    if kw not in self._searched_keywords:
                        queue.appendleft((kw, 0))
                logger.info(f"  Resuming with {len(queue)} keyword(s) in queue.")
            else:
                queue = deque((kw, 0) for kw in seeds)

            total = len(self._searched_keywords)
            all_bodies_for_ai: list[str] = []
            all_page_names_for_ai: list[str] = []

            while queue and total < self.max_keywords:
                keyword, depth = queue.popleft()
                if keyword in self._searched_keywords:
                    continue
                self._searched_keywords.add(keyword)
                total += 1

                logger.info(f"[{depth}] '{keyword}' ({total}/{self.max_keywords})")
                raw_ads = browser.search_keyword(keyword, max_ads=self.max_ads_per_keyword)
                new_ads = []

                for raw in raw_ads:
                    key = raw.get("_key", "")
                    if not key or key in self._seen_keys:
                        continue
                    if _is_blocked(raw):
                        self._seen_keys.add(key)  # mark seen so we don't re-process
                        continue
                    self._seen_keys.add(key)
                    ad = _to_standard_ad(raw, keyword)
                    page_id = ad.get("page_id", "")
                    if page_id and page_id != "unknown":
                        self._page_ads[page_id].append(ad)
                        self._page_keywords[page_id].add(keyword)
                        new_ads.append(ad)
                        body = raw.get("ad_body", "")
                        if body:
                            all_bodies_for_ai.append(body)
                        pname = raw.get("page_name", "")
                        if pname:
                            all_page_names_for_ai.append(pname)

                if depth < self.max_keyword_depth and new_ads:
                    # Try AI expansion first, fall back to frequency-based
                    if self.use_ai and all_bodies_for_ai:
                        ai_kws = expand_keywords_with_ai(
                            all_bodies_for_ai[-60:],  # recent bodies
                            self._searched_keywords,
                            max_new=8,
                            page_names=all_page_names_for_ai[-40:],
                        )
                        for kw in ai_kws:
                            if kw not in self._searched_keywords:
                                queue.append((kw, depth + 1))
                        if ai_kws:
                            save_state(self.state_file, self._snapshot_state(queue))
                            continue  # skip frequency-based if AI gave us something

                    # Frequency-based fallback
                    freq_kws = extract_new_keywords(new_ads, self._searched_keywords, max_new=6)
                    for kw in freq_kws:
                        if kw not in self._searched_keywords:
                            queue.append((kw, depth + 1))

                # Save after every keyword so interruptions are resumable
                save_state(self.state_file, self._snapshot_state(queue))

            logger.info(
                f"Phase 1 done: {len(self._page_ads)} pages, "
                f"{len(self._seen_keys)} ads collected."
            )

            # ── Phase 2: Page verification ────────────────────────────────
            # Pages worth a direct visit: total ad versions >= threshold
            # ("3 ads use this creative" counts as 3, not 1)
            def _total_versions(ads):
                return sum(a.get("_ad_versions", 1) for a in ads)

            promising = {
                pid: ads for pid, ads in self._page_ads.items()
                if _total_versions(ads) >= PAGE_VISIT_THRESHOLD
            }
            logger.info(
                f"Phase 2: Verifying {len(promising)} pages "
                f"(seen {PAGE_VISIT_THRESHOLD}+ times) for full ad counts..."
            )

            for pid, existing_ads in promising.items():
                # Skip supplier marketplace pages entirely
                if any(term in pid.lower() for term in _PLATFORM_BLOCKLIST):
                    continue
                fan_count = self._page_followers.get(pid, self._avg_followers(existing_ads))
                # Skip pages obviously outside follower range
                if fan_count > 0 and not (self.min_followers <= fan_count <= self.max_followers):
                    continue

                page_follower_count, page_ads = browser.get_page_ads(pid, max_ads=200)
                if page_follower_count > 0:
                    self._page_followers[pid] = page_follower_count
                if not page_ads:
                    continue

                added = 0
                for raw in page_ads:
                    key = raw.get("_key", "")
                    if not key or key in self._seen_keys:
                        continue
                    if _is_blocked(raw):
                        self._seen_keys.add(key)
                        continue
                    self._seen_keys.add(key)
                    ad = _to_standard_ad(raw, "page_visit")
                    self._page_ads[pid].append(ad)
                    added += 1

                total_now = len(self._page_ads[pid])
                logger.info(
                    f"  {pid}: {total_now} total ads "
                    f"({added} new from page visit)"
                )

        except KeyboardInterrupt:
            logger.warning("Interrupted — evaluating partial results...")
        finally:
            browser.stop()

        return self._evaluate_pages()

    def _avg_followers(self, ads: list[dict]) -> int:
        counts = [a["_follower_count"] for a in ads if a.get("_follower_count", 0) > 0]
        return int(sum(counts) / len(counts)) if counts else 0

    def _evaluate_pages(self) -> list[WinningProduct]:
        winners = []
        total = len(self._page_ads)

        for idx, (page_id, ads) in enumerate(self._page_ads.items(), 1):
            fan_count = self._page_followers.get(page_id, self._avg_followers(ads))

            if fan_count > 0 and not (self.min_followers <= fan_count <= self.max_followers):
                continue

            recent = [a for a in ads if _within_days(a, self.days)]
            if not recent:
                continue

            for cluster in cluster_page_ads(recent):
                # Sum "N ads use this creative" across all cards in the cluster
                total_versions = sum(a.get("_ad_versions", 1) for a in cluster)
                if total_versions < self.min_ads:
                    continue

                shop_now = sum(
                    1 for a in cluster
                    if a.get("_has_shop_now") or has_shop_now_cta(a)
                )
                if self.require_shop_now and shop_now == 0:
                    continue

                is_video = any(
                    (a.get("media_type") or "").upper() == "VIDEO"
                    for a in cluster
                )

                page_url = next(
                    (a["page_url"] for a in cluster if a.get("page_url")), ""
                )
                is_shopify, shopify_reason = False, "no url"
                if page_url:
                    is_shopify, shopify_reason = is_shopify_store(page_url)

                start_dates = sorted(
                    {a["_start_date"] for a in cluster if a.get("_start_date")}
                )
                platforms = sorted(
                    {p for a in cluster for p in (a.get("publisher_platforms") or [])}
                )
                sample = cluster[0]
                bodies = sample.get("ad_creative_bodies") or []
                page_name = sample.get("page_name", page_id)

                w = WinningProduct(
                    page_id=page_id,
                    page_name=page_name,
                    page_followers=fan_count,
                    page_url=page_url,
                    ad_count=total_versions,  # sum of "N ads use this creative"
                    total_page_ads=len(ads),
                    has_shop_now=shop_now > 0,
                    is_shopify=is_shopify,
                    shopify_reason=shopify_reason,
                    is_video=is_video,
                    ad_start_dates=start_dates,
                    sample_ad_body=(bodies[0][:300] if bodies else ""),
                    sample_snapshot_url=sample.get("ad_snapshot_url", ""),
                    publisher_platforms=platforms,
                    keywords_matched=sorted(
                        self._page_keywords.get(page_id, set())
                    ),
                )
                winners.append(w)
                logger.info(
                    f"  ✓ WINNER: {page_name} | {len(cluster)} ads | "
                    f"{fan_count} followers | Shopify={is_shopify}"
                )

        winners.sort(key=lambda w: (-w.ad_count, not w.is_video, not w.is_shopify))
        logger.info(f"Found {len(winners)} winning products.")
        return winners
