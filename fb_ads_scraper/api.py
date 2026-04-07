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

# Core fields available to any standard developer token.
# Restricted fields (impressions, spend, bylines, estimated_audience_size)
# require Meta Research API approval and are excluded here.
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
])

PAGE_FIELDS = "fan_count,name,website,category,link"


class FBApiError(Exception):
    """Raised when the FB API returns a non-retryable error."""
    def __init__(self, code, error_type, message):
        self.code = code
        self.error_type = error_type
        super().__init__(f"FB API {code} ({error_type}): {message}")


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
        """Make a GET request with exponential backoff on rate limits / 5xx errors.

        400 errors are NOT retried — they indicate a bad request and FB's
        error message is extracted and raised immediately so it's visible.
        """
        params = dict(params or {})
        # Don't re-add token when following a self-contained pagination next-URL
        if "access_token" not in url:
            params["access_token"] = self.access_token

        delay = 2
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(url, params=params, timeout=30)

                if not resp.ok:
                    # Always extract FB's actual error body
                    fb_code, fb_type, fb_msg = "?", "?", resp.text[:200]
                    try:
                        err_body = resp.json()
                        fb_err = err_body.get("error", {})
                        fb_code = fb_err.get("code", "?")
                        fb_type = fb_err.get("type", "?")
                        fb_msg = fb_err.get("message", resp.text[:200])
                        fb_sub = fb_err.get("error_subcode", "")
                        if fb_sub:
                            fb_msg += f" (subcode {fb_sub})"
                    except Exception:
                        pass

                    is_rate_limit = (
                        resp.status_code == 429
                        or "User request limit reached" in resp.text
                        or fb_code in (4, 17, 32, 613)
                    )
                    is_transient = resp.status_code in (500, 502, 503, 504)

                    if (is_rate_limit or is_transient) and attempt < self.max_retries:
                        logger.warning(
                            f"Transient/rate-limit error ({resp.status_code}). "
                            f"Retrying in {delay}s... | {fb_code} {fb_type}: {fb_msg}"
                        )
                        time.sleep(delay)
                        delay *= 2
                        continue

                    # Non-retryable — raise with FB's message clearly visible
                    raise FBApiError(fb_code, fb_type, fb_msg)

                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    code = err.get("code")
                    # Transient FB errors (temporary service issues)
                    if code in (1, 2, 341) and attempt < self.max_retries:
                        logger.warning(
                            f"Transient FB error ({code}): {err.get('message')}. "
                            f"Retrying in {delay}s..."
                        )
                        time.sleep(delay)
                        delay *= 2
                        continue
                    raise FBApiError(code, err.get("type", "?"), err.get("message", ""))

                return data

            except FBApiError:
                raise  # never swallow these
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise
                logger.warning(f"Network error: {e}. Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2

    def search_ads(
        self,
        search_terms: str,
        countries: list[str],
        active_status: str = "ACTIVE",
        media_type: Optional[str] = None,
        limit: int = 100,
        max_pages: int = 5,
    ) -> list[dict]:
        """
        Search the Ads Library and return ads (paginated).

        Args:
            search_terms: Keywords to search for.
            countries: List of 2-letter country codes, e.g. ["US"].
            active_status: "ACTIVE", "INACTIVE", or "ALL".
            media_type: Optional filter — "VIDEO", "IMAGE", "MEME", "CAROUSEL".
            limit: Results per page (max 500 per FB docs, but 100 is safer).
            max_pages: Cap on how many API pages to fetch per keyword.
        """
        # ad_reached_countries must be a JSON array string: '["US"]'
        params = {
            "search_terms": search_terms,
            "ad_reached_countries": json.dumps(list(countries)),
            "ad_active_status": active_status,
            "fields": ADS_FIELDS,
            "limit": limit,
        }
        if media_type:
            params["media_type"] = media_type

        ads = []
        url = f"{BASE_URL}/ads_archive"
        page_num = 0

        while url and page_num < max_pages:
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
        """Return page info with in-memory caching."""
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
