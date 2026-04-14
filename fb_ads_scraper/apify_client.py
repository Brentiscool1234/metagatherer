"""
Apify-based Facebook Ads Library scraper.

Uses any Apify actor that accepts a keyword search query and returns
Facebook ad data. Runs via the synchronous endpoint — blocks per keyword
then returns structured results without needing a local browser.

Required env vars:
    APIFY_API_KEY   — your Apify token (get at console.apify.com → Settings → API)
    APIFY_ACTOR_ID  — actor to run (e.g. "apidojo/facebook-ads-library-scraper")
                      defaults to "apidojo/facebook-ads-library-scraper"

Actor input schema sent by this client:
    searchQuery, country, activeStatus, adType, maxItems, period

If your actor uses different field names, set APIFY_INPUT_TEMPLATE in .env
as a JSON string to override the default input payload (use {keyword},
{country}, {limit} as placeholders).
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_APIFY_BASE    = "https://api.apify.com/v2"
_DEFAULT_ACTOR = "apidojo/facebook-ads-library-scraper"
_RUN_TIMEOUT   = 120    # seconds to wait for a single actor run


class ApifyAdsClient:
    def __init__(self, api_key: str, actor_id: str = ""):
        self.api_key  = api_key
        # Apify uses "~" as separator in URL paths instead of "/"
        raw = (actor_id or os.getenv("APIFY_ACTOR_ID", "") or _DEFAULT_ACTOR).strip()
        self.actor_id = raw.replace("/", "~")

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
        period  = f"last{min(days, 30)}d"

        # Allow full input override via env var
        template = os.getenv("APIFY_INPUT_TEMPLATE", "")
        if template:
            try:
                payload = json.loads(
                    template
                    .replace("{keyword}", keyword)
                    .replace("{country}", country)
                    .replace("{limit}", str(limit))
                )
            except json.JSONDecodeError:
                logger.warning("APIFY_INPUT_TEMPLATE is not valid JSON — using default")
                payload = self._default_payload(keyword, country, limit, period)
        else:
            payload = self._default_payload(keyword, country, limit, period)

        url = (
            f"{_APIFY_BASE}/acts/{self.actor_id}"
            f"/run-sync-get-dataset-items"
            f"?token={self.api_key}"
            f"&timeout={_RUN_TIMEOUT}"
            f"&memory=512"
        )
        try:
            resp = httpx.post(
                url, json=payload,
                timeout=_RUN_TIMEOUT + 15,
            )
            if resp.status_code == 400:
                logger.warning(
                    f"Apify 400 for '{keyword}' — check APIFY_ACTOR_ID and input schema. "
                    f"Response: {resp.text[:300]}"
                )
                return []
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                # Some actors wrap in {"items": [...]}
                data = data.get("items", data.get("data", []))
            logger.debug(f"  Apify: {len(data)} results for '{keyword}'")
            return data if isinstance(data, list) else []
        except httpx.TimeoutException:
            logger.warning(f"Apify timeout for '{keyword}' — skipping (browser will handle next run)")
            return []
        except Exception as e:
            logger.warning(f"Apify error for '{keyword}': {e}")
            return []

    @staticmethod
    def _default_payload(keyword, country, limit, period):
        return {
            "searchQuery":  keyword,
            "country":      country,
            "activeStatus": "ACTIVE",
            "adType":       "ALL",
            "maxItems":     limit,
            "period":       period,
        }

    @staticmethod
    def verify() -> bool:
        """Quick check that the API key is non-empty (no network call)."""
        return bool(os.getenv("APIFY_API_KEY", "").strip())


def apify_ad_to_standard(raw: dict, keyword: str) -> dict:
    """
    Map an Apify actor output item to MetaGatherer's standard ad dict.
    Handles the most common field name variants across popular actors.
    """
    page_id   = str(raw.get("page_id")   or raw.get("pageId")   or "")
    page_name =     raw.get("page_name") or raw.get("pageName") or ""
    page_url  = (
        raw.get("page_profile_uri")
        or raw.get("page_profile_url")
        or raw.get("pageUrl")
        or raw.get("page_url")
        or ""
    )

    # Follower / likes count — may be nested several levels deep
    followers = 0
    try:
        adv = raw.get("advertiser") or {}
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

    # Start date — normalise to YYYY-MM-DD
    start_raw = (
        raw.get("ad_delivery_start_time")
        or raw.get("adDeliveryStartTime")
        or raw.get("startDate")
        or raw.get("start_date")
        or ""
    )
    start_date = str(start_raw)[:10] if start_raw else ""

    # CTA / store URL
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
        or (raw.get("ad_creative_link_captions") is not None
            and str(raw.get("media_type", "")).upper() == "VIDEO")
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
