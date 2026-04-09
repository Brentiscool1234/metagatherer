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

# Comprehensive stop-words — anything too generic to be a useful product keyword
_STOP_WORDS = {
    # Articles / conjunctions / prepositions
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "as", "into", "through",
    "between", "during", "before", "after", "above", "below", "since",
    # Common verbs
    "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "can", "need", "get", "got", "getting",
    "make", "made", "makes", "making", "take", "takes", "taken",
    "come", "comes", "came", "coming", "go", "goes", "went", "going",
    "know", "knew", "known", "think", "thought", "look", "see", "feel",
    "want", "want", "wanted", "try", "tried", "find", "keep", "let",
    "put", "set", "run", "help", "show", "move", "play", "turn", "start",
    # Pronouns
    "this", "that", "these", "those", "i", "you", "he", "she", "we",
    "they", "it", "me", "him", "her", "us", "them", "my", "your", "his",
    "our", "their", "its", "what", "which", "who", "whom", "whose",
    # Generic adjectives / adverbs (useless as search terms)
    "good", "great", "best", "better", "bad", "new", "old", "big", "small",
    "large", "little", "high", "low", "long", "short", "right", "left",
    "real", "sure", "true", "false", "own", "same", "different", "other",
    "every", "each", "both", "few", "more", "most", "much", "many",
    "only", "just", "even", "also", "still", "well", "back", "way",
    "here", "there", "then", "now", "very", "too", "quite", "really",
    "never", "always", "often", "already", "again", "once", "ever",
    "however", "though", "although", "because", "while", "when", "where",
    "how", "why", "than", "so", "if", "not", "no", "yes", "any", "all",
    "first", "last", "next", "second", "third", "early", "late",
    # Ecommerce / marketing noise — useless as search terms
    "buy", "shop", "order", "off", "free", "sale", "today", "click",
    "link", "bio", "use", "code", "save", "check", "out", "offer",
    "deal", "discount", "shipping", "cart", "add", "limited", "time",
    "exclusive", "now", "only", "special", "promo", "percent", "price",
    "cost", "paid", "pay", "money", "cash", "value", "worth", "cheap",
    "fast", "quick", "easy", "simple", "perfect", "amazing", "awesome",
    "incredible", "love", "loved", "loving", "like", "liked", "enjoy",
    "works", "work", "working", "worked", "made", "days", "weeks",
    "months", "years", "day", "week", "month", "year", "per", "over",
    "people", "person", "men", "women", "man", "woman", "kids", "family",
    "life", "world", "home", "house", "place", "things", "thing",
    "learn", "style", "yourself", "reviews", "review",
    "sponsored", "ad", "ads", "advertisement", "advertiser",
    "delivery", "deliver", "delivered", "upgrade", "upgraded",
    "luxury", "luxurious", "premium", "quality", "brand", "brands",
    "savings", "save", "saved", "products", "product", "items", "item",
    "store", "stores", "online", "website", "site",
    "choose", "chosen", "choice", "living", "lifestyle",
    "meals", "meal", "food", "drink", "level", "levels",
    "results", "result", "experience", "experiences",
    "amazing", "incredible", "fantastic", "wonderful", "powerful",
    "everything", "nothing", "something", "anything",
    "needs", "need", "needed", "wants", "wanted",
    "later", "elevate", "elevated", "transform", "transformation",
    # Web / tech noise
    "www", "http", "https", "com", "co", "uk", "eu", "org", "net",
    "facebook", "instagram", "tiktok", "youtube", "twitter",
    "i", "a",
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
    Two ads belong to the same cluster if their keyword sets share at least 1 word.
    For small dropshipping pages (which usually promote one product) this
    keeps the whole page's ads together rather than splitting them into
    tiny groups that each fall below the min_ads threshold.
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
            if not visited[j] and keyword_overlap(kw_sets[i], kw_sets[j], threshold=1):
                cluster.append(j)
                visited[j] = True
        clusters.append([ads[k] for k in cluster])

    return clusters


def extract_new_keywords(ads: list[dict], existing: set[str], max_new: int = 15) -> list[str]:
    """
    Mine ads for product keyword PHRASES (bigrams + trigrams) not yet searched.

    Single words like "luxury" or "delivery" are useless for finding dropshipping
    products. 2-3 word phrases like "dog anxiety vest" or "knee compression sleeve"
    are actual searchable product names — that's what we want to expand into.

    Rules:
    - Both/all words in a phrase must pass the stop-word filter
    - Phrase must appear in at least 2 different ads (filters brand-specific copy)
    - Trigrams ranked higher than bigrams (more specific)
    """
    # Build a clean word list per ad, used for phrase generation
    def _words(ad: dict) -> list[str]:
        text = get_ad_text(ad)
        cleaned = _clean_text(text)
        return [
            w for w in cleaned.split()
            if len(w) >= 3 and w not in _STOP_WORDS and not w.isdigit()
        ]

    # Count how many distinct ads each phrase appears in
    phrase_ad_count: Counter = Counter()
    seen_in_ad: dict[str, set] = {}  # phrase → set of ad indices

    for ad_idx, ad in enumerate(ads):
        words = _words(ad)
        seen_phrases: set[str] = set()

        # Bigrams
        for i in range(len(words) - 1):
            phrase = f"{words[i]} {words[i + 1]}"
            if phrase not in existing:
                seen_phrases.add(phrase)

        # Trigrams
        for i in range(len(words) - 2):
            phrase = f"{words[i]} {words[i + 1]} {words[i + 2]}"
            if phrase not in existing:
                seen_phrases.add(phrase)

        for phrase in seen_phrases:
            phrase_ad_count[phrase] += 1

    # Keep phrases seen in 2+ ads, prefer trigrams (more specific)
    candidates = [
        phrase for phrase, cnt in phrase_ad_count.most_common(max_new * 4)
        if cnt >= 2
    ]
    # Trigrams first, then bigrams, both ranked by frequency
    trigrams = [p for p in candidates if len(p.split()) == 3]
    bigrams  = [p for p in candidates if len(p.split()) == 2]
    ranked = trigrams + bigrams

    return ranked[:max_new]
