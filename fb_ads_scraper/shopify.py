"""
Shopify store detection.

Checks a URL to determine whether it resolves to a Shopify-powered store using:
  1. Domain contains "myshopify.com"
  2. Response headers contain Shopify-specific values
  3. Response HTML contains Shopify CDN references or meta tags
"""

import logging
import re
from urllib.parse import urlparse, parse_qs, unquote

import requests

logger = logging.getLogger(__name__)

_SHOPIFY_HEADER_KEYS = {
    "x-shopify-stage",
    "x-storefront-renderer-rendered",
    "x-sorting-hat-podid",
    "x-shopid",
    "x-shardid",
}

_SHOPIFY_HTML_PATTERNS = [
    re.compile(r"cdn\.shopify\.com", re.I),
    re.compile(r"Shopify\.theme", re.I),
    re.compile(r'"shop_id":', re.I),
    re.compile(r"myshopify\.com", re.I),
    re.compile(r"shopify-section", re.I),
]

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
})


def decode_facebook_redirect(url: str) -> str:
    """Unwrap Facebook's l.php?u= redirect to get the real destination URL."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.netloc in ("l.facebook.com", "lm.facebook.com") or "l.php" in parsed.path:
        qs = parse_qs(parsed.query)
        if "u" in qs:
            return unquote(qs["u"][0])
    return url


def _normalize_url(url: str) -> str:
    """Ensure URL has a scheme."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def is_shopify_store(url: str, timeout: int = 8) -> tuple[bool, str]:
    """
    Return (is_shopify: bool, reason: str).

    Fast path: myshopify.com domain → True immediately.
    Otherwise: HTTP HEAD then GET (up to 2 KB of body) to check headers/HTML.
    """
    url = _normalize_url(url)
    if not url:
        return False, "no url"

    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # Fast path
    if "myshopify.com" in host:
        return True, "myshopify.com domain"

    # Try HEAD first (fast, no body)
    try:
        head = _session.head(url, timeout=timeout, allow_redirects=True)
        headers_lower = {k.lower(): v for k, v in head.headers.items()}

        for key in _SHOPIFY_HEADER_KEYS:
            if key in headers_lower:
                return True, f"header: {key}"

        # Check final redirect URL
        if head.url and "myshopify.com" in head.url:
            return True, "redirect to myshopify.com"

        # GET only a small portion of the body to check HTML signals
        resp = _session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        # Read first 20 KB — enough to find <head> Shopify signals
        chunk = b""
        for block in resp.iter_content(chunk_size=4096):
            chunk += block
            if len(chunk) >= 20480:
                break
        resp.close()

        html = chunk.decode("utf-8", errors="ignore")
        for pattern in _SHOPIFY_HTML_PATTERNS:
            if pattern.search(html):
                return True, f"html: {pattern.pattern}"

        return False, "no shopify signals found"

    except requests.RequestException as e:
        logger.debug(f"Shopify check failed for {url}: {e}")
        return False, f"request error: {e}"


def extract_url_from_ad(ad: dict) -> str:
    """
    Try to pull a destination URL from an ad dict.
    Checks captions and snapshot URL hostname, falls back to page website.
    """
    # ad_creative_link_captions sometimes contains the raw URL
    captions = ad.get("ad_creative_link_captions") or []
    if isinstance(captions, str):
        captions = [captions]
    for cap in captions:
        if cap and ("http" in cap or "." in cap):
            url = _normalize_url(cap)
            if urlparse(url).netloc:
                return url

    # Bylines field sometimes has URL-like info
    bylines = ad.get("bylines") or []
    if isinstance(bylines, str):
        bylines = [bylines]
    for b in bylines:
        if b and "." in b:
            url = _normalize_url(b)
            if urlparse(url).netloc:
                return url

    return ""
