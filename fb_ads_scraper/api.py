"""
Facebook Ads Library API client with rate limiting, caching, and pagination.
Docs: https://developers.facebook.com/docs/marketing-api/reference/ads-archive
"""

import json
import time
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FB_API_VERSION = "v19.0"
BASE_URL = f"https://graph.facebook.com/{FB_API_VERSION}"

# Fields to request from ads_archive.
# NOTE: impressions, spend, bylines require Meta Research API access
# (restricted to approved researchers). Removed to avoid 400 errors for
# standard developer tokens.
ADS_FIELDS = ",".join([
    "id",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "ad_snapshot_url",
    "page_id",
    "page_name",
    "publisher_platforms",
    "media_type",
    "languages",
    "currency",
])

PAGE_FIELDS = "fan_count,name,website,category,link"


class RateLimitError(Exception):
    pass


class FBApiClient:
    def __init__(self, access_token: str, max_retries: int = 4):
        self.access_token = access_token
        self.max_retries = max_retries
        self._page_cache: dict = {}
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "MetaGatherer/1.0"})

    def _request(self, url: str, params: dict = None) -> dict:
        """Make a GET request with exponential backoff on rate limit or transient errors."""
        params = dict(params or {})  # never mutate caller's dict
        # Don't add token again if it's already embedded in a pagination next-URL
        if "access_token" not in url:
            params["access_token"] = self.access_token
        delay = 2
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(url, params=params, timeout=30)

                # Try to parse FB's error body before raising on status
                if not resp.ok:
                    try:
                        err_body = resp.json()
                        fb_err = err_body.get("error", {})
                        fb_msg = f"FB {fb_err.get('code','?')} ({fb_err.get('type','?')}): {fb_err.get('message','')}"
                    except Exception:
                        fb_msg = resp.text[:300]

                    is_rate_limit = resp.status_code == 429 or "User request limit reached" in resp.text
                    is_transient = resp.status_code in (500, 502, 503, 504)

                    if (is_rate_limit or is_transient) and attempt < self.max_retries:
                        logger.warning(f"Transient error ({resp.status_code}). Retrying in {delay}s... [{fb_msg}]")
                        time.sleep(delay)
                        delay *= 2
                        continue

                    resp.raise_for_status()

                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    # Transient FB errors worth retrying
                    if err.get("code") in (1, 2, 4, 17, 341) and attempt < self.max_retries:
                        logger.warning(
                            f"FB API error (code {err.get('code')}): {err.get('message')}. "
                            f"Retrying in {delay}s..."
                        )
                        time.sleep(delay)
                        delay *= 2
                        continue
                    raise ValueError(f"FB API error {err.get('code')} ({err.get('type')}): {err.get('message')}")
                return data
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise
                logger.warning(f"Request failed: {e}. Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2

    def search_ads(
        self,
        search_terms: str,
        countries: list[str],
        active_status: str = "ACTIVE",
        media_type: Optional[str] = None,
        limit: int = 500,
        max_pages: int = 5,
    ) -> list[dict]:
        """
        Search the Ads Library and return all ads (paginated).

        Args:
            search_terms: Keywords to search for.
            countries: List of 2-letter country codes, e.g. ["US"].
            active_status: "ACTIVE", "INACTIVE", or "ALL".
            media_type: Optional filter — "VIDEO", "IMAGE", "NONE", "MEME", "CAROUSEL".
            limit: Results per page (max 500).
            max_pages: Cap on how many API pages to fetch per keyword.
        """
        # ad_reached_countries must be a JSON-encoded array string, e.g. '["US"]'
        params = {
            "search_terms": search_terms,
            "ad_reached_countries": json.dumps(list(countries)),
            "ad_active_status": active_status,
            "ad_type": "ALL",
            "fields": ADS_FIELDS,
            "limit": limit,
        }
        if media_type:
            params["media_type"] = media_type

        ads = []
        url = f"{BASE_URL}/ads_archive"
        page_num = 0

        while url and page_num < max_pages:
            # For page 0 pass full params; for subsequent pages the next-URL
            # is self-contained (cursor + token already embedded), so pass {}.
            data = self._request(url, params if page_num == 0 else {})
            ads.extend(data.get("data", []))
            page_num += 1

            next_url = data.get("paging", {}).get("next")
            if next_url:
                url = next_url
            else:
                break

        logger.info(f"Fetched {len(ads)} ads for '{search_terms}'")
        return ads

    def get_page_info(self, page_id: str) -> dict:
        """Return page info (fan_count, website, etc.) with in-memory caching."""
        if page_id in self._page_cache:
            return self._page_cache[page_id]

        try:
            data = self._request(
                f"{BASE_URL}/{page_id}",
                {"fields": PAGE_FIELDS},
            )
        except Exception as e:
            logger.warning(f"Could not fetch page {page_id}: {e}")
            data = {}

        self._page_cache[page_id] = data
        return data

    def verify_token(self) -> bool:
        """Quick check that the token is valid."""
        try:
            self._request(f"{BASE_URL}/me", {"fields": "id,name"})
            return True
        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            return False
