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

        # Strip None values — actor validates types strictly and None fields
        # will fail with "must be X type" even when the field is optional.
        run_input = {k: v for k, v in {
            "urls":                       [{"url": search_url}],
            "scrapeAdDetails":            True,
            "limitPerSource":             0,
            "count":                      limit,
            "scrapePageAds.period":       "",
            "scrapePageAds.activeStatus": "all",
            "scrapePageAds.sortBy":       "impressions_desc",
            "scrapePageAds.countryCode":  "ALL",
        }.items() if v is not None}

        try:
            run = self._client.actor(self.actor_id).call(run_input=run_input)
            items = list(
                self._client.dataset(run["defaultDatasetId"]).iterate_items()
            )
            logger.info(f"  Apify: {len(items)} raw items for '{keyword}'")
            # Log field coverage of first item to verify snapshot extraction
            if items:
                first = items[0]
                snap  = first.get("snapshot") or {}
                logger.info(
                    f"  Apify sample — top-level keys: {list(first.keys())[:20]}"
                )
                logger.info(
                    f"  Apify sample — snapshot keys: {list(snap.keys())[:20]}"
                )
                logger.info(
                    f"  Apify sample — collation_count={first.get('collation_count')} "
                    f"ad_archive_id={first.get('ad_archive_id')} "
                    f"has_body={'body' in snap} "
                    f"has_cards={'cards' in snap and bool(snap.get('cards'))} "
                    f"creation_time={snap.get('creation_time')}"
                )
            return items
        except Exception as e:
            logger.warning(f"Apify error for '{keyword}': {e}")
            return []


def apify_ad_to_standard(raw: dict, keyword: str) -> dict:
    """
    Map an Apify actor (XtaWFhbtfxyzqrFmd) output item to MetaGatherer's standard ad dict.

    Confirmed top-level fields from live runs:
      ad_archive_id, collation_count, collation_id, page_id, page_name, snapshot,
      is_active, reach_estimate, currency, spend, categories, contains_digital_created_media

    The `snapshot` sub-dict contains the actual creative data:
      body.text, title, cards[], link_url, page_profile_uri, creation_time,
      publisher_platform, videos[], images[]
    """
    snap: dict = raw.get("snapshot") or {}

    page_id   = str(raw.get("page_id") or snap.get("page_id") or "")
    page_name = raw.get("page_name") or snap.get("page_name") or ""

    # Page URL — prefer snapshot profile URI, fall back to constructed URL
    page_url = (
        snap.get("page_profile_uri")
        or snap.get("page_profile_url")
        or raw.get("page_profile_uri")
        or raw.get("page_url")
        or (f"https://www.facebook.com/{page_id}" if page_id else "")
    )

    # Follower / likes count — may be nested in snapshot or advertiser blob
    followers = 0
    try:
        adv  = raw.get("advertiser") or {}
        info = adv.get("ad_library_page_info", {}).get("page_info", {})
        followers = int(info.get("likes") or info.get("followers") or 0)
    except Exception:
        pass
    if not followers:
        followers = int(
            raw.get("page_likes") or raw.get("pageLikes")
            or snap.get("page_likes") or 0
        )

    # ── Ad body / creative text ───────────────────────────────────────────────
    # Primary source: snapshot.body.text (single creative) or snapshot.cards[]
    bodies: list[str] = []

    body_obj = snap.get("body")
    if isinstance(body_obj, dict):
        t = (body_obj.get("text") or "").strip()
        if t:
            bodies.append(t)
    elif isinstance(body_obj, str) and body_obj.strip():
        bodies.append(body_obj.strip())

    # Carousel cards may each have their own body
    for card in (snap.get("cards") or []):
        if isinstance(card, dict):
            card_body = card.get("body") or ""
            if isinstance(card_body, dict):
                card_body = card_body.get("text") or ""
            if card_body and card_body not in bodies:
                bodies.append(str(card_body).strip())

    # Fallback: top-level fields used by other actor flavours
    if not bodies:
        for field in ("ad_creative_bodies", "adCreativeBodies", "caption",
                      "description", "text"):
            val = raw.get(field)
            if isinstance(val, list):
                bodies = [str(v) for v in val if v]
                break
            elif isinstance(val, str) and val:
                bodies = [val]
                break

    # ── Start date → YYYY-MM-DD ───────────────────────────────────────────────
    start_date = ""
    # snapshot.creation_time is a Unix timestamp (int)
    creation_ts = snap.get("creation_time")
    if creation_ts:
        try:
            from datetime import datetime, timezone
            start_date = datetime.fromtimestamp(
                int(creation_ts), tz=timezone.utc
            ).strftime("%Y-%m-%d")
        except Exception:
            pass
    if not start_date:
        start_raw = (
            raw.get("ad_delivery_start_time")
            or raw.get("adDeliveryStartTime")
            or raw.get("startDate")
            or raw.get("start_date")
            or ""
        )
        start_date = str(start_raw)[:10] if start_raw else ""

    # ── CTA / snapshot URL ────────────────────────────────────────────────────
    cta_url = (
        snap.get("link_url")
        or (snap.get("link_destination") or {}).get("link")
        or raw.get("snapshot_url")
        or raw.get("ad_snapshot_url")
        or ""
    )

    # ── Publisher platforms ───────────────────────────────────────────────────
    platforms = (
        snap.get("publisher_platform")
        or raw.get("publisher_platforms")
        or raw.get("publisherPlatforms")
        or []
    )
    if isinstance(platforms, str):
        platforms = [platforms]

    # ── Media type ────────────────────────────────────────────────────────────
    has_video = bool(
        snap.get("videos")
        or raw.get("has_video")
        or str(raw.get("media_type", "")).upper() == "VIDEO"
    )

    # ── collation_count = number of adsets running this same creative ─────────
    # Under modern broad/ASC/DCT targeting a single creative is duplicated across
    # many adsets rather than creating many distinct ads.  This is the key signal.
    collation = int(raw.get("collation_count") or 1)

    ad_archive_id = str(raw.get("ad_archive_id") or raw.get("id") or "")

    return {
        "_key":                f"{page_id}_{ad_archive_id}",
        "page_id":             page_id,
        "page_name":           page_name,
        "page_url":            page_url,
        "page_followers":      followers,
        "ad_creative_bodies":  bodies,
        "_start_date":         start_date,
        "_cta_url":            cta_url,
        "_has_shop_now":       False,
        # collation_count is "N adsets use this creative" — the primary scaling
        # signal under broad/ASC/DCT targeting.  Treat it as ad_versions so
        # the scoring engine sees the real reach of this creative.
        "_ad_versions":        max(1, collation),
        "publisher_platforms": platforms,
        "media_type":          "VIDEO" if has_video else "IMAGE",
        "_keyword":            keyword,
        "ad_snapshot_url":     cta_url,
    }
