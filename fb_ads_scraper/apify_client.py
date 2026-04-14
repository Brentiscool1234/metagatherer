"""
Apify-based Facebook Ads Library scraper.

Uses actor XtaWFhbtfxyzqrFmd via the official apify-client Python package.
The actor accepts Facebook Ads Library search URLs and returns structured ad data.

Required:
    pip install apify-client
    APIFY_API_KEY in .env  (console.apify.com → Settings → Integrations → API token)
"""

import logging
import os
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

_ACTOR_ID = "XtaWFhbtfxyzqrFmd"
_ADS_LIB_BASE = "https://www.facebook.com/ads/library/"


def _build_search_url(keyword: str, country: str) -> str:
    """Build a Facebook Ads Library keyword search URL for the Apify actor."""
    params = {
        "active_status": "all",
        "ad_type": "all",
        "country": country.upper(),
        "q": keyword,
        "search_type": "keyword_unordered",
        "media_type": "all",
    }
    return _ADS_LIB_BASE + "?" + urlencode(params)


class ApifyAdsClient:
    def __init__(self, api_key: str, actor_id: str = ""):
        self.api_key  = api_key
        self.actor_id = (actor_id or os.getenv("APIFY_ACTOR_ID", "") or _ACTOR_ID).strip()
        # Lazy-load the apify_client package
        try:
            from apify_client import ApifyClient as _AC
            self._client = _AC(api_key)
        except ImportError:
            raise ImportError(
                "apify-client is not installed. Run: pip install apify-client"
            )

    def search_keyword(
        self,
        keyword: str,
        countries: list[str],
        limit: int = 120,
        days: int = 7,
    ) -> list[dict]:
        """
        Run the Apify actor for one keyword, return raw ad dicts.
        Falls back to [] on any error so the caller can use the browser instead.
        """
        country = (countries[0] if countries else "US").upper()
        search_url = _build_search_url(keyword, country)

        run_input = {
            "urls": [{"url": search_url}],
            "scrapeAdDetails": True,
            "limitPerSource": 0,
            "count": limit,
            "scrapePageAds.period": "",
            "scrapePageAds.activeStatus": "all",
            "scrapePageAds.sortBy": "impressions_desc",
            "scrapePageAds.countryCode": "ALL",
            "runTag": None,
            "proxy": None,
        }

        try:
            run = self._client.actor(self.actor_id).call(run_input=run_input)
            items = list(
                self._client.dataset(run["defaultDatasetId"]).iterate_items()
            )
            logger.debug(f"  Apify: {len(items)} results for '{keyword}'")
            return items
        except Exception as e:
            logger.warning(f"Apify error for '{keyword}': {e}")
            return []


def apify_ad_to_standard(raw: dict, keyword: str) -> dict:
    """
    Map an Apify actor output item to MetaGatherer's standard ad dict.
    """
    page_id   = str(raw.get("page_id")   or raw.get("pageId")   or "")
    page_name =     raw.get("page_name") or raw.get("pageName") or ""
    page_url  = (
        raw.get("page_profile_uri")
        or raw.get("page_profile_url")
        or raw.get("pageUrl")
        or raw.get("page_url")
        or (f"https://www.facebook.com/{page_id}" if page_id else "")
    )

    # Follower / likes count — may be nested
    followers = 0
    try:
        adv  = raw.get("advertiser") or {}
        info = adv.get("ad_library_page_info", {}).get("page_info", {})
        followers = int(info.get("likes") or info.get("followers") or 0)
    except Exception:
        pass
    if not followers:
        followers = int(raw.get("page_likes") or raw.get("pageLikes") or 0)

    # Ad body / creative text
    bodies: list[str] = []
    for field in ("ad_creative_bodies", "adCreativeBodies", "body",
                  "caption", "description", "text"):
        val = raw.get(field)
        if isinstance(val, list):
            bodies = [str(v) for v in val if v]
            break
        elif isinstance(val, str) and val:
            bodies = [val]
            break

    # Start date → YYYY-MM-DD
    start_raw = (
        raw.get("ad_delivery_start_time")
        or raw.get("adDeliveryStartTime")
        or raw.get("startDate")
        or raw.get("start_date")
        or ""
    )
    start_date = str(start_raw)[:10] if start_raw else ""

    # CTA / snapshot URL
    cta_url = (
        raw.get("snapshot_url")
        or raw.get("snapshotUrl")
        or raw.get("ad_snapshot_url")
        or ""
    )

    # Publisher platforms
    platforms = raw.get("publisher_platforms") or raw.get("publisherPlatforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]

    has_video = bool(
        raw.get("has_video")
        or str(raw.get("media_type", "")).upper() == "VIDEO"
    )

    return {
        "_key":                f"{page_id}_{raw.get('id', raw.get('ad_id', ''))}",
        "page_id":             page_id,
        "page_name":           page_name,
        "page_url":            page_url,
        "page_followers":      followers,
        "ad_creative_bodies":  bodies,
        "_start_date":         start_date,
        "_cta_url":            cta_url,
        "_has_shop_now":       False,
        "_ad_versions":        1,
        "publisher_platforms": platforms,
        "media_type":          "VIDEO" if has_video else "IMAGE",
        "_keyword":            keyword,
        "ad_snapshot_url":     cta_url,
    }
