"""
Keyword extraction and product-similarity grouping.

Logic:
  - Extract candidate keywords from ad text (titles, bodies, captions).
  - Group ads from the same page by product similarity using shared keyword overlap.
  - A "product cluster" is a set of ads from one page that share enough keywords.
"""

import re
import string
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

# Common English stop-words to ignore when extracting product keywords
_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "as", "into", "through",
    "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "can", "need", "dare", "ought", "used",
    "this", "that", "these", "those", "i", "you", "he", "she", "we",
    "they", "it", "me", "him", "her", "us", "them", "my", "your", "his",
    "our", "their", "its", "get", "our", "your", "now", "shop", "buy",
    "off", "free", "new", "best", "sale", "today", "here", "click",
    "link", "bio", "use", "code", "save", "more", "all", "just", "like",
    "only", "also", "so", "if", "not", "no", "yes", "www", "http",
    "https", "com", "co", "uk", "us", "eu", "de", "fr", "limited",
    "offer", "deal", "discount", "shipping", "order", "add", "cart",
    "check", "out", "what", "how", "why", "when", "where", "who",
    "which", "than", "then", "time", "day", "days", "week", "weeks",
    "month", "months", "year", "years", "up", "over", "back", "per",
}

# These signal ecommerce intent; used as seed keyword boosters
ECOMMERCE_SIGNALS = {
    "buy now", "shop now", "order now", "get yours", "get it now",
    "add to cart", "free shipping", "limited time", "while supplies last",
    "exclusive deal", "flash sale", "50% off", "60% off", "70% off",
    "discount", "clearance", "promo", "checkout", "ships worldwide",
}

# CTA phrases that indicate "Shop Now" button presence in ad copy
SHOP_NOW_PHRASES = {
    "shop now", "shop here", "shop today", "click to shop",
    "order now", "buy now", "get it now", "get yours now",
    "purchase now", "grab yours", "link in bio", "click the link",
}


def _clean_text(text: str) -> str:
    """Lowercase, remove punctuation and URLs."""
    text = text.lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return text


def extract_keywords(ad: dict, min_len: int = 4, top_n: int = 10) -> list[str]:
    """
    Pull the most meaningful product-related words from an ad's text fields.
    Returns up to top_n keywords sorted by frequency.
    """
    blobs = []
    for field in ("ad_creative_bodies", "ad_creative_link_titles",
                  "ad_creative_link_descriptions", "ad_creative_link_captions"):
        val = ad.get(field)
        if isinstance(val, list):
            blobs.extend(val)
        elif isinstance(val, str):
            blobs.append(val)

    combined = " ".join(blobs)
    cleaned = _clean_text(combined)
    tokens = [
        w for w in cleaned.split()
        if len(w) >= min_len and w not in _STOP_WORDS and not w.isdigit()
    ]
    freq = Counter(tokens)
    return [w for w, _ in freq.most_common(top_n)]


def get_ad_text(ad: dict) -> str:
    """Concatenate all text fields of an ad into a single string."""
    parts = []
    for field in ("ad_creative_bodies", "ad_creative_link_titles",
                  "ad_creative_link_descriptions", "ad_creative_link_captions"):
        val = ad.get(field)
        if isinstance(val, list):
            parts.extend(str(v) for v in val if v)
        elif val:
            parts.append(str(val))
    return " ".join(parts)


def has_shop_now_cta(ad: dict) -> bool:
    """
    Heuristic: check whether the ad copy contains shop-now style phrasing.
    The FB Ads Library API does not expose the exact CTA button type, so we
    rely on text signals and captions (which often mirror the button label).
    """
    text = get_ad_text(ad).lower()
    captions = ad.get("ad_creative_link_captions") or []
    if isinstance(captions, str):
        captions = [captions]
    caption_text = " ".join(c.lower() for c in captions)
    combined = text + " " + caption_text
    return any(phrase in combined for phrase in SHOP_NOW_PHRASES)


def ad_started_within_days(ad: dict, days: int) -> bool:
    """Return True if ad_delivery_start_time is within the last `days` days."""
    raw = ad.get("ad_delivery_start_time") or ad.get("ad_creation_time")
    if not raw:
        return False
    try:
        # FB returns ISO-8601 with offset, e.g. "2024-03-01T12:00:00+0000"
        dt = datetime.fromisoformat(raw.replace("+0000", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return dt >= cutoff
    except ValueError:
        return False


def keyword_overlap(kw_set_a: set[str], kw_set_b: set[str], threshold: int = 3) -> bool:
    """Return True if two keyword sets share at least `threshold` words."""
    return len(kw_set_a & kw_set_b) >= threshold


def cluster_page_ads(ads: list[dict]) -> list[list[dict]]:
    """
    Given a list of ads from ONE page, group them into product clusters.
    Two ads belong to the same cluster if their keyword sets overlap by >= 3 words.
    Returns a list of clusters (each cluster is a list of ads).
    """
    if not ads:
        return []

    kw_sets = [set(extract_keywords(ad)) for ad in ads]
    n = len(ads)
    visited = [False] * n
    clusters = []

    for i in range(n):
        if visited[i]:
            continue
        cluster = [i]
        visited[i] = True
        for j in range(i + 1, n):
            if not visited[j] and keyword_overlap(kw_sets[i], kw_sets[j]):
                cluster.append(j)
                visited[j] = True
        clusters.append([ads[k] for k in cluster])

    return clusters


def extract_new_keywords(ads: list[dict], existing: set[str], max_new: int = 15) -> list[str]:
    """
    Mine `ads` for product keyword phrases not yet in `existing`.
    Returns up to `max_new` new candidate search terms.
    """
    freq: Counter = Counter()
    for ad in ads:
        for kw in extract_keywords(ad, top_n=20):
            if kw not in existing:
                freq[kw] += 1

    # Only return words that appeared in at least 2 different ads (reduces noise)
    candidates = [w for w, c in freq.most_common(max_new * 2) if c >= 2]
    return candidates[:max_new]
