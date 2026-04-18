"""
Core scraping orchestration — Selenium browser-based, no API key required.

Two-phase approach:
  Phase 1 — Keyword sweep: search 30 keywords, collect ads, track pages
  Phase 2 — Page verification: visit each promising page's own Ads Library
             view to count ALL their active ads (fixes the 12+ threshold)

AI keyword expansion uses Claude (ANTHROPIC_API_KEY in .env) to suggest
specific product search terms instead of generic frequency-mined words.
"""

import json
import logging
import os
import time as _time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional

from .browser import AdsLibraryBrowser, parse_follower_count, parse_date_text
from .analysis import cluster_page_ads, extract_new_keywords, has_shop_now_cta
from .ai_keywords import expand_keywords_with_ai, keywords_for_niche, is_dropshipping_page
from .shopify import is_shopify_store
from .state import save_state, load_state, state_summary, DEFAULT_STATE_FILE

logger = logging.getLogger(__name__)

# ── Control-file helpers (GUI ↔ scraper IPC) ──────────────────────────────────
_CTRL_FILE   = "mg_control.json"   # GUI writes cmd → scraper reads
_INJECT_FILE = "mg_inject.json"    # Expert writes keywords → scraper reads


def _read_control() -> str:
    """Read the current control command ('pause'/'resume'/'stop') or ''."""
    try:
        if os.path.exists(_CTRL_FILE):
            data = json.loads(open(_CTRL_FILE).read())
            return data.get("cmd", "")
    except Exception:
        pass
    return ""


def _clear_control():
    try:
        if os.path.exists(_CTRL_FILE):
            os.remove(_CTRL_FILE)
    except Exception:
        pass


def _read_and_clear_inject() -> list[str]:
    """Return any AI-injected keywords and remove the file."""
    try:
        if os.path.exists(_INJECT_FILE):
            data = json.loads(open(_INJECT_FILE).read())
            os.remove(_INJECT_FILE)
            return [k for k in data if isinstance(k, str)]
    except Exception:
        pass
    return []


# ── Pages/ads from these platforms are supplier marketplaces, not dropshipping stores
_PLATFORM_BLOCKLIST = {
    "alibaba", "aliexpress", "temu", "amazon", "1688", "dhgate", "shein",
    "wish.com", "banggood",
}

# Page names that indicate Facebook's login wall was scraped instead of a real page
_JUNK_PAGE_NAMES = {
    "log in", "login", "sign in", "sign up", "facebook", "create account",
    "log into facebook", "connect with facebook",
}

# Well-known large brands that will never be dropshipping stores
_KNOWN_BIG_BRANDS = {
    "intuit", "quickbooks", "turbotax", "volvo", "chatgpt", "openai",
    "red lobster", "lay's", "lays", "planet fitness", "fidelity",
    "bank of america", "ally bank", "ally financial", "carecredit",
    "king arthur", "nectar sleep", "lovevery", "tracy anderson",
    "pocket fm", "adobe", "chewy", "taco bell", "tacobell",
    "mcdonald", "burger king", "wendy's", "subway", "starbucks",
    "walmart", "target", "costco", "nike", "adidas", "apple",
    "samsung", "google", "microsoft", "netflix", "spotify",
    "doordash", "uber eats", "grubhub", "instacart",
    "state farm", "geico", "progressive", "allstate",
    "chase", "wells fargo", "citibank", "capital one",
    "h&r block", "creditkarma", "credit karma",
    "factor", "factor meals", "hims", "hers", "noom",
    "weight watchers", "nutrisystem", "bowflex",
    "casper", "purple mattress", "saatva",
    "indeed", "linkedin", "ziprecruiter",
    "chewy", "petco", "petsmart",
    # Retail / food chains
    "lowe's", "lowes", "home depot", "homedepot", "best buy", "bestbuy",
    "kroger", "safeway", "whole foods", "wholefoods", "trader joe",
    "kohl's", "kohls", "nordstrom", "macy's", "macys", "gap", "old navy",
    "old navy", "h&m", "zara", "forever 21", "forever21",
    "domino's", "dominoes", "pizza hut", "pizzahut", "kfc", "chick-fil-a",
    "chipotle", "panera", "panda express", "panera bread",
    "dunkin", "krispy kreme", "popeyes",
    # Media / services
    "hulu", "disney", "disney+", "hbo", "peacock", "amazon prime",
    "new york times", "nytimes", "washington post", "wapo",
    "nothing bundt cakes", "nothingbundtcakes",
    "booker prize", "booker prizes",
    "anaconda", "anaconda distribution",  # software company
    "hello nancy", "hellonancy",
    # Insurance / finance
    "aetna", "cigna", "humana", "unitedhealth", "anthem",
    "vanguard", "schwab", "fidelity", "td ameritrade",
    # Automotive
    "toyota", "honda", "ford", "chevrolet", "chevy", "bmw", "mercedes",
    "hyundai", "kia", "nissan", "tesla", "dodge", "chrysler",
}

# Maps user-supplied niche words → canonical NICHE_SEED_MAP / NICHE_TERMS key
_NICHE_ALIASES: dict[str, str] = {
    # pet
    "dog": "pet", "dogs": "pet", "cat": "pet", "cats": "pet",
    "pets": "pet", "puppy": "pet", "puppies": "pet",
    "kitten": "pet", "kittens": "pet", "animal": "pet", "animals": "pet",
    "canine": "pet", "feline": "pet",
    # fitness
    "gym": "fitness", "workout": "fitness", "sport": "fitness",
    "sports": "fitness", "exercise": "fitness", "weightloss": "fitness",
    "weight loss": "fitness", "yoga": "fitness", "pilates": "fitness",
    # kitchen
    "cooking": "kitchen", "baking": "kitchen", "chef": "kitchen",
    "cook": "kitchen", "recipe": "kitchen",
    # beauty
    "skincare": "beauty", "makeup": "beauty", "cosmetic": "beauty",
    "cosmetics": "beauty", "haircare": "beauty", "nails": "beauty",
    # home
    "house": "home", "bedroom": "home", "decor": "home",
    "interior": "home", "furniture": "home", "cleaning": "home",
    # tech
    "gadget": "tech", "gadgets": "tech", "electronics": "tech",
    "phone": "tech", "computer": "tech",
    # jewelry
    "accessories": "jewelry", "fashion": "jewelry", "jewellery": "jewelry",
    # baby
    "kids": "baby", "infant": "baby", "toddler": "baby",
    "children": "baby", "child": "baby", "newborn": "baby",
    # outdoor
    "camping": "outdoor", "hiking": "outdoor", "garden": "outdoor",
    "gardening": "outdoor", "hunting": "outdoor", "fishing": "outdoor",
    # car
    "vehicle": "car", "auto": "car", "truck": "car", "automotive": "car",
}


# Maximum product price — ads mentioning prices above this are filtered out
MAX_PRODUCT_PRICE = 250

# Food-related signals in ad copy — human food products are not dropshippable
_FOOD_SIGNALS = {
    "calories", "nutrition facts", "per serving", "serving size",
    "ingredients:", "tablespoon", "teaspoon", "bake at", "preheat oven",
    "hellofresh", "hungryroot", "freshly", "everyplate", "blue apron",
    "sunbasket", "meal kit", "meal plan", "food delivery",
    "order food", "dinner delivery", "lunch delivery",
    "snack subscription", "grocery delivery",
    "restaurant", "dine in", "takeout", "take-out", "catering",
    "coffee subscription", "wine subscription", "beer subscription",
}

# Chemical / hazardous product signals — not suitable for dropshipping
_CHEMICAL_SIGNALS = {
    "bleach", "ammonia", "chlorine", "hydrochloric", "sulfuric acid",
    "pesticide", "herbicide", "insecticide", "fungicide", "rodenticide",
    "weed killer", "bug killer", "rat poison", "disinfectant concentrate",
    "industrial cleaner", "solvent", "chemical formula",
}

import re as _re
_PRICE_RE = _re.compile(r'\$\s*([\d,]+(?:\.\d{1,2})?)')


def _min_price_in_text(text: str) -> float:
    """Return the largest dollar amount found in text, or 0 if none found.

    We want the product price, not a shipping/discount figure. Ad copy like
    "Save $5 — get yours for $49.99" would yield min=$5 (shipping cost) which
    would incorrectly pass the MAX_PRODUCT_PRICE filter. Taking the max picks
    the actual product price instead.
    """
    prices = []
    for m in _PRICE_RE.finditer(text):
        try:
            val = float(m.group(1).replace(",", ""))
            if 1 < val < 50_000:   # ignore $0 and absurd numbers
                prices.append(val)
        except ValueError:
            pass
    return max(prices) if prices else 0.0


def _is_blocked(raw: dict) -> bool:
    """Return True if this ad should be skipped (marketplace, food, chemicals)."""
    # Support both raw browser dicts (ad_body: str) and standardized dicts
    # (ad_creative_bodies: list). Without this, Phase 1 food/chemical checks
    # silently never fire because standardized dicts don't have "ad_body".
    ad_body = raw.get("ad_body") or ""
    if not ad_body:
        bodies = raw.get("ad_creative_bodies") or []
        if isinstance(bodies, list):
            ad_body = " ".join(b for b in bodies if b)
    check = " ".join([
        (raw.get("page_name") or "").lower(),
        (raw.get("page_url") or "").lower(),
        ad_body.lower(),
    ])
    if any(term in check for term in _PLATFORM_BLOCKLIST):
        return True
    body = ad_body.lower()
    if any(sig in body for sig in _FOOD_SIGNALS):
        return True
    if any(sig in body for sig in _CHEMICAL_SIGNALS):
        return True
    return False


def _is_junk_page(page_name: str, page_id: str) -> bool:
    """Return True if this page is a login-wall artifact or known big brand."""
    name = (page_name or "").lower().strip()
    pid = (page_id or "").lower().replace(".", " ").strip()
    if name in _JUNK_PAGE_NAMES:
        return True
    # Check both page name and page ID slug against brand set
    combined = name + " " + pid
    if any(brand in combined for brand in _KNOWN_BIG_BRANDS):
        return True
    # Numeric-only page IDs with generic names are often login artifacts
    if name in ("log in", "") and page_id.isdigit():
        return True
    return False

SEED_KEYWORDS = [
    "buy now",
    "shop now",
    "order now",
    "get yours",
    "50% off",
    "as seen on",
    "ships worldwide",
    "add to cart",
    "while supplies last",
    "grab yours",
]

# Hardcoded niche → seed keywords used when AI is unavailable
NICHE_SEED_MAP: dict[str, list[str]] = {
    "pet": [
        "cat water fountain", "dog anxiety vest", "automatic pet feeder",
        "dog harness no pull", "cat litter mat", "pet hair remover",
        "dog calming treats", "retractable dog leash", "pet nail grinder",
        "cat scratcher lounge",
    ],
    "fitness": [
        "resistance bands set", "ab roller wheel", "posture corrector",
        "knee compression sleeve", "jump rope speed", "pull up bar",
        "massage gun deep tissue", "yoga mat thick", "ankle weights",
        "foam roller muscle",
    ],
    "kitchen": [
        "vegetable chopper", "garlic press rocker", "mandoline slicer",
        "avocado slicer", "egg poacher pan", "spiralizer vegetable",
        "knife sharpener electric", "salad spinner", "rice cooker mini",
        "air fryer rack",
    ],
    "beauty": [
        "led face mask therapy", "hair growth serum", "jade roller gua sha",
        "lash serum growth", "vitamin c serum face", "derma roller face",
        "blackhead remover vacuum", "eyebrow stamp kit", "lip plumper device",
        "scalp massager shampoo",
    ],
    "home": [
        "led strip lights", "galaxy projector star", "weighted blanket",
        "humidifier ultrasonic", "oil diffuser essential", "shower head filter",
        "door draft stopper", "cable management box", "shower caddy tension",
        "blackout curtains thermal",
    ],
    "tech": [
        "wireless charger pad", "phone holder car mount", "ring light selfie",
        "bluetooth tracker wallet", "laptop stand adjustable", "desk organizer",
        "cable organizer clips", "screen cleaner kit", "keyboard wrist rest",
        "webcam cover privacy",
    ],
    "jewelry": [
        "layered necklace set", "minimalist ring gold", "huggie hoop earrings",
        "initial necklace pendant", "birthstone bracelet", "anklet set gold",
        "crystal hair claw", "statement earrings", "pearl necklace set",
        "charm bracelet women",
    ],
    "baby": [
        "baby monitor camera", "diaper bag backpack", "baby carrier wrap",
        "teething toys silicone", "white noise machine baby", "baby nail file",
        "nursing pillow breastfeeding", "baby food maker", "stroller organizer",
        "bath thermometer baby",
    ],
    "outdoor": [
        "portable solar charger", "camping hammock lightweight", "hiking backpack",
        "water filter straw", "led headlamp rechargeable", "folding camp chair",
        "paracord bracelet survival", "insulated water bottle", "bug repellent wristband",
        "emergency blanket mylar",
    ],
    "car": [
        "car phone mount magnetic", "dash cam front rear", "seat gap organizer",
        "trunk organizer collapsible", "car air freshener vent", "steering wheel cover",
        "car vacuum cordless", "windshield sun shade", "blind spot mirror",
        "tire pressure gauge digital",
    ],
}

# Pages seen this many times in Phase 1 get a full verification visit.
# Raising this reduces Phase 2 visits (each visit costs ~15s browser time).
# A page must have accumulated 3+ ad-version units to be worth visiting.
PAGE_VISIT_THRESHOLD = 3

# Maps niche key → related terms for fast keyword-based relevance filtering
NICHE_TERMS: dict[str, set[str]] = {
    "pet":      {"dog","cat","pet","pup","puppy","kitten","feline","canine","fur","paw","leash","collar","treat","feeder","litter","aquarium","fish","bird","hamster","rabbit"},
    "fitness":  {"fitness","workout","exercise","gym","muscle","weight","yoga","protein","resistance","band","stretch","posture","knee","back","pain","relief","brace","sleeve","foam","roller","massage"},
    "kitchen":  {"kitchen","cook","chef","slice","chop","dice","peel","gadget","knife","cutting","board","pan","pot","air fryer","blender","juicer","coffee","grater","strainer","utensil"},
    "beauty":   {"skin","face","hair","beauty","glow","serum","mask","lash","brow","nail","lip","eye","acne","wrinkle","moistur","collagen","vitamin c","retinol","scalp","dandruff"},
    "home":     {"home","room","bedroom","bathroom","kitchen","decor","light","lamp","curtain","pillow","blanket","organiz","storage","shelf","humidif","diffuser","air purif","projector","candle"},
    "tech":     {"tech","phone","laptop","computer","cable","charge","wireless","bluetooth","usb","screen","keyboard","mouse","webcam","stand","holder","earbuds","headphone","speaker","ring light"},
    "jewelry":  {"jewelry","necklace","bracelet","ring","earring","pendant","charm","gold","silver","crystal","pearl","diamond","bead","anklet","choker","locket"},
    "baby":     {"baby","infant","toddler","newborn","diaper","stroller","crib","nursery","teething","pacifier","breastfeed","bottle","monitor","carrier","swaddle"},
    "outdoor":  {"outdoor","camping","hiking","trail","backpack","tent","survival","waterproof","solar","headlamp","fire","fishing","hunting","kayak","bike","cycle"},
    "car":      {"car","vehicle","auto","truck","suv","dashboard","seat","trunk","tyre","tire","windshield","mirror","park","drive","road","motor"},
}


def _extract_page_id(page_url: str) -> str:
    """
    Extract a stable page identifier from a Facebook page URL.

    Handles both formats:
      https://www.facebook.com/somebrand         → "somebrand"
      https://www.facebook.com/profile.php?id=123456789 → "123456789"  (numeric)
    """
    if not page_url:
        return "unknown"
    # profile.php?id=NUMERIC — very common for newer/smaller pages
    import re as _re2
    m = _re2.search(r"profile\.php\?.*?id=(\d+)", page_url)
    if m:
        return m.group(1)
    slug = page_url.split("facebook.com/")[-1].split("?")[0].strip("/")
    # Avoid accidental capture of non-page paths
    if not slug or "/" in slug or slug.lower() in ("", "ads", "pages", "groups"):
        return "unknown"
    return slug


def _to_standard_ad(raw: dict, keyword: str) -> dict:
    page_url = raw.get("page_url", "")
    page_id  = _extract_page_id(page_url)
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
        "_cta_url": raw.get("cta_url", ""),
        # Browser scrapes live library — all returned ads are active
        "_is_active": True,
    }


def _api_search_keyword(client, keyword: str, countries: list, limit: int = 200) -> list:
    """
    Call the FB Ads Library API for a single keyword.
    Returns a list of raw API ad dicts (already structured — no Selenium needed).
    """
    try:
        return client.search_ads(
            search_terms=keyword,
            countries=countries,
            active_status="ACTIVE",
            limit=min(limit, 200),
            max_pages=3,
        )
    except Exception as e:
        logger.warning(f"  API search failed for '{keyword}': {e}")
        return []


def _api_ad_to_standard(api_ad: dict, keyword: str) -> dict:
    """
    Convert an FB API ad dict to the same internal format that _to_standard_ad
    produces from Selenium-scraped data, so the rest of the pipeline is unchanged.
    """
    page_id = str(api_ad.get("page_id", "") or "unknown")
    page_name = api_ad.get("page_name", "")

    # Infer CTA URL from captions (API often puts store URL here)
    captions = api_ad.get("ad_creative_link_captions") or []
    if isinstance(captions, str):
        captions = [captions]
    cta_url = next((c for c in captions if c and c.startswith("http")), "")

    # Infer start date from delivery start time
    raw_start = (
        api_ad.get("ad_delivery_start_time") or
        api_ad.get("ad_creation_time") or ""
    )
    start_date = None
    if raw_start:
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(raw_start.replace("+0000", "+00:00"))
            start_date = dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    bodies = api_ad.get("ad_creative_bodies") or []
    if isinstance(bodies, str):
        bodies = [bodies]

    is_video = (api_ad.get("media_type") or "").upper() == "VIDEO"
    has_shop_now = any(
        phrase in " ".join(bodies).lower()
        for phrase in ("shop now", "buy now", "order now", "get yours")
    )

    platforms = api_ad.get("publisher_platforms") or ["facebook"]

    return {
        "id": api_ad.get("id", ""),
        "page_id": page_id,
        "page_name": page_name,
        "page_url": f"https://www.facebook.com/{page_id}",
        "ad_creative_bodies": bodies,
        "ad_creative_link_captions": captions,
        "ad_creative_link_titles": api_ad.get("ad_creative_link_titles") or [],
        "ad_creative_link_descriptions": api_ad.get("ad_creative_link_descriptions") or [],
        "ad_snapshot_url": api_ad.get("ad_snapshot_url", ""),
        "media_type": "VIDEO" if is_video else "IMAGE",
        "publisher_platforms": platforms,
        "languages": api_ad.get("languages") or [],
        "_follower_count": 0,       # fetched in Phase 2
        "_has_shop_now": has_shop_now,
        "_start_date": start_date,
        "_keyword": keyword,
        "_ad_versions": 1,          # API gives 1 record per creative
        "_cta_url": cta_url,
        "_key": api_ad.get("id", ""),
        "_is_active": True,         # API only returns active ads
    }


def _within_days(ad: dict, days: int) -> bool:
    # Apify sets _is_active explicitly. A currently-running ad is always
    # "recent" regardless of when it started — long-running ads are the
    # BEST signals. Only apply the date window to inactive/unconfirmed ads.
    if ad.get("_is_active") is True:
        return True
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
        sourcing = getattr(self, "sourcing_data", {}) or {}
        return {
            "score": getattr(self, "score", 0.0),
            "page_name": self.page_name,
            "page_id": self.page_id,
            "page_followers": self.page_followers,
            "page_url": self.page_url,
            "store_url": getattr(self, "store_url", ""),
            "winning_ad_count": self.ad_count,
            "total_page_ads_seen": self.total_page_ads,
            "has_shop_now_cta": self.has_shop_now,
            "is_shopify_store": self.is_shopify,
            "shopify_via_browser": getattr(self, "shopify_confirmed_via_browser", False),
            "shopify_detection_reason": self.shopify_reason,
            "video_ads_present": self.is_video,
            "ad_start_dates": "; ".join(self.ad_start_dates),
            "earliest_ad_start": min(self.ad_start_dates) if self.ad_start_dates else "",
            "latest_ad_start": max(self.ad_start_dates) if self.ad_start_dates else "",
            "sample_ad_body": self.sample_ad_body,
            "snapshot_url": self.sample_snapshot_url,
            "ads_library_url": (
                # Numeric page IDs → direct page view via view_all_page_id (most reliable)
                f"https://www.facebook.com/ads/library/?active_status=all"
                f"&ad_type=all&country=US&search_type=page"
                f"&view_all_page_id={self.page_id}"
                if self.page_id.isdigit() else
                # Username slug → Ads Library search by page name
                f"https://www.facebook.com/ads/library/?active_status=all"
                f"&ad_type=all&country=US&search_type=page"
                f"&q={self.page_name or self.page_id}"
            ),
            "publisher_platforms": ", ".join(self.publisher_platforms),
            "keywords_matched": ", ".join(self.keywords_matched),
            # ── AliExpress sourcing ──────────────────────────────────────────
            "shopify_product_title": sourcing.get("shopify_product_title", ""),
            "shopify_product_url": sourcing.get("shopify_product_url", ""),
            "store_price_usd": sourcing.get("shopify_product_price", ""),
            "aliexpress_found": sourcing.get("aliexpress_found", ""),
            "aliexpress_product_url": sourcing.get("aliexpress_product_url", ""),
            "aliexpress_match_confidence": sourcing.get("aliexpress_match_confidence", ""),
            "aliexpress_price_min": sourcing.get("aliexpress_min_price", ""),
            "aliexpress_price_max": sourcing.get("aliexpress_max_price", ""),
            "aliexpress_supplier_count": sourcing.get("aliexpress_suppliers", ""),
            "aliexpress_search_term": sourcing.get("aliexpress_search_term", ""),
            "margin_pct": sourcing.get("margin_pct", ""),
            "gross_profit_usd": sourcing.get("gross_profit", ""),
            "break_even_roas": sourcing.get("break_even_roas", ""),
            "margin_viable": sourcing.get("margin_viable", ""),
            # ── Market signals ───────────────────────────────────────────────
            "saturation_count": getattr(self, "saturation_count", 0),
            "page_age_days": getattr(self, "page_age_days", ""),
            "recent_ad_count": getattr(self, "recent_ad_count", 0),
            "freshness_rate": getattr(self, "freshness_rate", ""),
            "market_stage": getattr(self, "market_stage", ""),
            # ── Opportunity & risk ───────────────────────────────────────────
            "opportunity_tier": getattr(self, "opportunity_tier", ""),
            "opportunity_label": getattr(self, "opportunity_label", ""),
            "risk_flags": "; ".join(
                f"{f.code}({f.severity})" for f in getattr(self, "risk_flags", [])
            ),
            # ── Market intel ─────────────────────────────────────────────────
            "market_structure": (getattr(self, "market_intel", None) or {}).get("structure", ""),
            "entry_timing": (getattr(self, "market_intel", None) or {}).get("entry_timing", ""),
            "new_advertisers": (getattr(self, "market_intel", None) or {}).get("new_advertisers", ""),
            "established_advertisers": (getattr(self, "market_intel", None) or {}).get("established_advertisers", ""),
            "competitor_count": len(getattr(self, "competitors", []) or []),
            "top_competitors": "; ".join(
                c["page_name"] for c in (getattr(self, "competitors", []) or [])[:5]
            ),
        }


class FBAdsScraper:
    def __init__(
        self,
        countries: list[str] = None,
        days: int = 7,
        min_ads: int = 5,
        min_followers: int = 10,
        max_followers: int = 400,   # N8N validated: pages with 400+ likes are legacy brands
        max_total_ads: int = 250,   # N8N validated: 250 cap removes heavy media buyers
        min_active_ratio: float = 0.85,  # N8N validated: ≥85% of ads must still be active
        prefer_video: bool = True,
        require_shop_now: bool = False,
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
        self.max_total_ads = max_total_ads  # 0 = no cap
        self.min_active_ratio = min_active_ratio  # 0.0 = disabled
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
        self._seen_winner_ids: set[str] = set()
        self._visited_page_ids: set[str] = set()   # pages already Phase-2-crawled
        # Saturation tracking: keyword → number of distinct pages seen for that keyword
        self._keyword_page_density: dict[str, int] = {}

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
                self._seen_winner_ids = saved.get("seen_winner_ids", set())
                self._visited_page_ids = saved.get("visited_page_ids", set())
                self._keyword_page_density = saved.get("keyword_page_density", {})
                logger.info(
                    f"Resuming saved state — {state_summary(saved)}"
                )

    def _snapshot_state(self, queue=None) -> dict:
        return {
            "searched_keywords": self._searched_keywords,
            "queue": list(queue) if queue is not None else [],
            "page_ads": self._page_ads,
            "page_keywords": self._page_keywords,
            "page_followers": self._page_followers,
            "seen_keys": self._seen_keys,
            "seen_winner_ids": self._seen_winner_ids,
            "visited_page_ids": self._visited_page_ids,
            "keyword_page_density": self._keyword_page_density,
        }

    def run(self, extra_keywords: list[str] = None, discord_webhook: str = "",
            stop_on_winner: bool = False) -> list[WinningProduct]:
        # Generic seeds that reliably return results from FB Ads Library.
        # Always included as a safety net so Phase 1 collects something even
        # if niche-specific terms return 0 ads.
        _GENERIC_SAFETY_SEEDS = ["buy now", "shop now", "order now", "get yours"]

        if self.niche:
            if self.use_ai:
                niche_kws = keywords_for_niche(self.niche, count=10)
            else:
                niche_kws = []

            if niche_kws:
                logger.info(f"Niche '{self.niche}' → AI seeds: {niche_kws}")
                niche_seeds = niche_kws
            else:
                # Try hardcoded niche map
                niche_lower = self.niche.lower()
                niche_seeds = next(
                    (kws for key, kws in NICHE_SEED_MAP.items() if key in niche_lower),
                    None,
                )
                if niche_seeds:
                    logger.info(f"Niche '{self.niche}' → built-in seed keywords")
                else:
                    # Try alias map: "dogs"→"pet", "gym"→"fitness", etc.
                    words = niche_lower.split()
                    alias_key = next(
                        (_NICHE_ALIASES[w] for w in words if w in _NICHE_ALIASES),
                        None,
                    )
                    if alias_key:
                        niche_seeds = NICHE_SEED_MAP.get(alias_key, [])
                        if niche_seeds:
                            logger.info(
                                f"Niche '{self.niche}' → alias '{alias_key}' "
                                f"seed keywords"
                            )
                    if not niche_seeds:
                        logger.warning(
                            f"No built-in seeds for niche '{self.niche}' — "
                            f"using generic seeds. Set ANTHROPIC_API_KEY for AI seeds."
                        )
                        niche_seeds = []

            # Niche seeds first, then generic safety seeds (deduped)
            seen = set(niche_seeds)
            seeds = list(niche_seeds) + [s for s in _GENERIC_SAFETY_SEEDS if s not in seen]
        else:
            seeds = list(SEED_KEYWORDS)

        if extra_keywords:
            seeds = list(extra_keywords) + [s for s in seeds if s not in extra_keywords]

        # ── Fast-path detection (priority: Apify > FB API > browser) ────────────
        # Apify: set APIFY_API_KEY (and optionally APIFY_ACTOR_ID) in .env
        # FB API: set FB_ACCESS_TOKEN in .env
        # Both skip the local browser for Phase 1; Phase 2 always uses browser.
        _apify_client = None
        _api_client   = None

        _apify_key = os.getenv("APIFY_API_KEY", "").strip()
        if _apify_key:
            try:
                from .apify_client import ApifyAdsClient
                _apify_client = ApifyAdsClient(_apify_key)
                logger.info(
                    f"Apify mode active — Phase 1 via actor '{_apify_client.actor_id}'"
                )
            except Exception as e:
                logger.warning(f"Apify init failed: {e} — falling back")
                _apify_client = None

        if not _apify_client:
            _fb_token = os.getenv("FB_ACCESS_TOKEN", "").strip()
            if _fb_token:
                try:
                    from .api import FBApiClient
                    _api_client = FBApiClient(_fb_token)
                    if _api_client.verify_token():
                        logger.info("FB API token valid — Phase 1 will use API (fast mode)")
                    else:
                        logger.warning("FB_ACCESS_TOKEN invalid — falling back to browser")
                        _api_client = None
                except Exception as e:
                    logger.warning(f"FB API init failed: {e} — falling back to browser")
                    _api_client = None

        # Load Shopify disk cache early so quick winner checks during Phase 1
        # can use cached results without waiting for the full HTTP pre-check.
        _SHOPIFY_CACHE_FILE = "shopify_url_cache.json"
        _disk_cache: dict[str, tuple[bool, str]] = {}
        try:
            import json as _json
            if os.path.exists(_SHOPIFY_CACHE_FILE):
                _raw = _json.load(open(_SHOPIFY_CACHE_FILE))
                _disk_cache = {k: tuple(v) for k, v in _raw.items()}
        except Exception:
            pass

        browser = AdsLibraryBrowser(countries=self.countries, headless=self.headless)
        browser.start()

        try:
            # ── Phase 1: Keyword sweep ────────────────────────────────────
            logger.info(
                "Phase 1: Keyword sweep"
                + (" [Apify]" if _apify_client else " [FB API fast-mode]" if _api_client else " [browser mode]")
            )

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
                all_seeds = list(seeds) + list(extra_keywords or [])
                queue = deque(
                    (kw, 0) for kw in all_seeds
                    if kw not in self._searched_keywords
                )

            total = len(self._searched_keywords)
            all_bodies_for_ai: list[str] = []
            all_page_names_for_ai: list[str] = []

            # ── Apify lookahead: keep N actor runs in-flight while the main
            # loop processes the previous results.  Turns 30 serial 40s calls
            # (20 min) into ~40s + 29×processing_time (~5 min) — 4-5× speedup.
            _ap_pool = None
            _ap_futures: dict = {}
            _APIFY_LOOKAHEAD = 6

            if _apify_client:
                from concurrent.futures import ThreadPoolExecutor as _TPE
                _ap_pool = _TPE(max_workers=_APIFY_LOOKAHEAD)

                def _apify_prefetch_next():
                    """Keep up to LOOKAHEAD futures in-flight — submit only as slots open up."""
                    for _pkw, _ in list(queue):
                        # Stop if pool is already full
                        if len(_ap_futures) >= _APIFY_LOOKAHEAD:
                            break
                        # Don't submit beyond the keyword cap
                        if total + len(_ap_futures) >= self.max_keywords:
                            break
                        if _pkw not in self._searched_keywords and _pkw not in _ap_futures:
                            _ap_futures[_pkw] = _ap_pool.submit(
                                _apify_client.search_keyword, _pkw,
                                countries=self.countries,
                                limit=self.max_ads_per_keyword,
                                days=self.days,
                            )

                _apify_prefetch_next()  # kick off first batch before the loop starts

            # AI expansion counter — only call Claude every N keywords to reduce
            # redundant API calls (all_bodies_for_ai accumulates across keywords
            # so calling every 3rd keyword uses practically the same data).
            _AI_EXPAND_EVERY = 3
            _ai_expand_counter = 0

            _CHECK_WINNER_EVERY = 10  # quick scoring pass every N keywords
            _winner_check_counter = 0
            _found_winner_early = False

            while queue and total < self.max_keywords:
                keyword, depth = queue.popleft()
                if keyword in self._searched_keywords:
                    continue
                self._searched_keywords.add(keyword)
                total += 1

                logger.info(f"[{depth}] '{keyword}' ({total}/{self.max_keywords})")

                if _apify_client:
                    from .apify_client import apify_ad_to_standard as _apify_to_std
                    # Kick off the NEXT batch before blocking on this one —
                    # while we wait for this result, the next N are already running.
                    _apify_prefetch_next()
                    if keyword in _ap_futures:
                        raw_ads = _ap_futures.pop(keyword).result()
                    else:
                        # BFS-expanded keyword added after initial prefetch
                        raw_ads = _apify_client.search_keyword(
                            keyword,
                            countries=self.countries,
                            limit=self.max_ads_per_keyword,
                            days=self.days,
                        )
                elif _api_client:
                    raw_ads = _api_search_keyword(
                        _api_client, keyword,
                        countries=self.countries,
                        limit=min(self.max_ads_per_keyword, 200),
                    )
                else:
                    raw_ads = browser.search_keyword(keyword, max_ads=self.max_ads_per_keyword)

                new_ads = []
                # Track distinct pages seen for this keyword (saturation)
                _pages_this_keyword: set[str] = set()

                for raw in raw_ads:
                    # For Apify: convert first so _key is built from actor-specific fields,
                    # then use the converted ad's key. For other sources check raw directly.
                    if _apify_client:
                        ad = _apify_to_std(raw, keyword)
                        key = ad.get("_key", "")
                    else:
                        key = raw.get("_key", "") or raw.get("id", "")

                    if not key or key in self._seen_keys:
                        continue
                    # Run blocklist on the standardized ad dict — Apify raws use
                    # different field names (snapshot.body.text vs page_name), so
                    # calling _is_blocked(raw) silently misses everything in Apify mode.
                    # For Apify we already have `ad`; for other sources convert first.
                    if not _apify_client:
                        if _api_client:
                            ad = _api_ad_to_standard(raw, keyword)
                        else:
                            ad = _to_standard_ad(raw, keyword)
                    if _is_blocked(ad):
                        self._seen_keys.add(key)
                        continue
                    self._seen_keys.add(key)
                    page_id = ad.get("page_id", "")
                    if page_id and page_id != "unknown":
                        self._page_ads[page_id].append(ad)
                        self._page_keywords[page_id].add(keyword)
                        _pages_this_keyword.add(page_id)
                        new_ads.append(ad)
                        bodies = ad.get("ad_creative_bodies") or []
                        body = bodies[0] if bodies else ""
                        if body:
                            all_bodies_for_ai.append(body)
                        pname = ad.get("page_name", "")
                        if pname:
                            all_page_names_for_ai.append(pname)
                        # Propagate follower count from Apify data so Phase 2
                        # pre-check and _evaluate_pages have a real value even
                        # before a browser visit.  Always take the highest seen —
                        # different ads from the same page can report different counts.
                        if _apify_client:
                            pf = ad.get("page_followers", 0) or 0
                            if pf > self._page_followers.get(page_id, 0):
                                self._page_followers[page_id] = pf

                # Update saturation density for this keyword
                self._keyword_page_density[keyword] = len(_pages_this_keyword)

                # ── Hot-check: immediately visit pages that look like winners ─────────
                # If a page already has >= 15 recent ads after this keyword, do an
                # early Phase-2 browser visit so we don't wait until all keywords finish.
                # Capped at 2 hot-checks per keyword to limit speed impact.
                if browser and not _api_client and not _apify_client:
                    _HOT_THRESHOLD = 15
                    _hot_checked = 0
                    for _hot_pid in list(_pages_this_keyword):
                        if _hot_checked >= 2:
                            break
                        if _hot_pid in self._visited_page_ids and _hot_pid not in self._seen_winner_ids:
                            continue
                        _hot_ads = self._page_ads.get(_hot_pid, [])
                        _hot_recent = [a for a in _hot_ads if _within_days(a, self.days)]
                        if len(_hot_recent) < _HOT_THRESHOLD:
                            continue
                        logger.info(
                            f"  🔥 Hot-check: page {_hot_pid} has {len(_hot_recent)} recent ads "
                            f"— visiting now for full count"
                        )
                        try:
                            _fan, _page_ads_now = browser.get_page_ads(
                                _hot_pid, max_ads=200,
                                known_followers=self._page_followers.get(_hot_pid, 0))
                            if _fan > 0:
                                self._page_followers[_hot_pid] = _fan
                            for _raw in (_page_ads_now or []):
                                _k = _raw.get("_key", "")
                                if not _k or _k in self._seen_keys:
                                    continue
                                if _is_blocked(_raw):
                                    self._seen_keys.add(_k)
                                    continue
                                self._seen_keys.add(_k)
                                _ad = _to_standard_ad(_raw, "hot_check")
                                self._page_ads[_hot_pid].append(_ad)
                            self._visited_page_ids.add(_hot_pid)
                        except Exception as _hce:
                            logger.debug(f"  Hot-check failed for {_hot_pid}: {_hce}")
                        _hot_checked += 1

                _ai_expand_counter += 1
                if depth < self.max_keyword_depth and new_ads:
                    expanded = False
                    # Only call Claude every N keywords — all_bodies_for_ai accumulates
                    # so the N-th call sees nearly identical data as the (N-1)-th.
                    # Reduces AI API calls by ~3× with no meaningful quality loss.
                    if (self.use_ai and all_bodies_for_ai
                            and _ai_expand_counter >= _AI_EXPAND_EVERY):
                        _ai_expand_counter = 0
                        # AI expansion: asks Claude for product-specific phrases
                        ai_kws = expand_keywords_with_ai(
                            all_bodies_for_ai[-60:],
                            self._searched_keywords,
                            max_new=8,
                            page_names=all_page_names_for_ai[-40:],
                        )
                        for kw in ai_kws:
                            if kw not in self._searched_keywords:
                                queue.append((kw, depth + 1))
                        if ai_kws:
                            save_state(self.state_file, self._snapshot_state(queue))
                            expanded = True
                            continue

                    if not expanded:
                        # Phrase-based fallback: bigrams/trigrams from ad copy
                        # (much better than single words — finds "dog anxiety vest"
                        # instead of "anxiety" or "vest" separately)
                        phrase_kws = extract_new_keywords(
                            new_ads, self._searched_keywords, max_new=6
                        )
                        for kw in phrase_kws:
                            if kw not in self._searched_keywords:
                                queue.append((kw, depth + 1))
                        if phrase_kws:
                            logger.debug(
                                f"  Phrase expansion: {phrase_kws}"
                            )

                # ── Periodic winner check (until-winner mode) ─────────────
                if stop_on_winner:
                    _winner_check_counter += 1
                    if _winner_check_counter >= _CHECK_WINNER_EVERY:
                        _winner_check_counter = 0
                        from .scoring import WINNER_THRESHOLD as _WT
                        _quick = self._evaluate_pages(browser=None, shopify_cache=_disk_cache)
                        _quick_winners = [r for r in _quick if getattr(r, "score", 0) >= _WT]
                        if _quick_winners:
                            logger.info(
                                f"  Winner found after {total} keywords "
                                f"({_quick_winners[0].page_name}) — stopping keyword sweep early."
                            )
                            _found_winner_early = True
                            break

                # Save after every keyword so interruptions are resumable
                save_state(self.state_file, self._snapshot_state(queue))

                # ── Check GUI control signals ─────────────────────────────
                # Check for AI-injected keywords (written by the expert module)
                injected = _read_and_clear_inject()
                for kw in injected:
                    if kw not in self._searched_keywords:
                        queue.appendleft((kw, depth + 1))
                        logger.info(f"  AI expert injected keyword: '{kw}'")

                ctrl = _read_control()
                if ctrl == "pause":
                    _clear_control()
                    logger.info("⏸  Scan paused — saving state. Press Continue to resume.")
                    save_state(self.state_file, self._snapshot_state(queue))
                    _stopped_while_paused = False
                    while True:
                        _time.sleep(1)
                        cmd2 = _read_control()
                        if cmd2 == "resume":
                            _clear_control()
                            logger.info("▶  Scan resumed.")
                            break
                        if cmd2 == "stop":
                            _clear_control()
                            logger.info("⏹  Stopped while paused — exporting results.")
                            _stopped_while_paused = True
                            break
                    if _stopped_while_paused:
                        break  # exit Phase 1 outer loop
                    # else: resume — fall through to next keyword

                if ctrl == "stop":
                    _clear_control()
                    logger.info("⏹  Stop requested — finishing current state and exporting results.")
                    save_state(self.state_file, self._snapshot_state(queue))
                    break

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
                # Check for stop signal between page visits too
                if _read_control() == "stop":
                    _clear_control()
                    logger.info("⏹  Stop received during Phase 2 — exporting partial results.")
                    break

                # Skip supplier marketplace pages entirely
                if any(term in pid.lower() for term in _PLATFORM_BLOCKLIST):
                    continue

                # Skip pages already crawled in a previous run, UNLESS they were
                # winners or near-misses (worth rechecking for new/grown ad counts)
                if pid in self._visited_page_ids and pid not in self._seen_winner_ids:
                    logger.debug(f"  Phase 2 skip {pid}: already crawled, no prior win/near-miss")
                    continue

                # When Apify sourced this page's data, the actor already returned
                # the follower count and full collation_count — browser verification
                # adds nothing and just wastes 15-25s per page.
                if _apify_client and self._page_followers.get(pid, 0) > 0:
                    logger.debug(
                        f"  Phase 2 skip {pid}: Apify data complete "
                        f"({self._page_followers[pid]} followers confirmed)"
                    )
                    self._visited_page_ids.add(pid)
                    continue

                fan_count = self._page_followers.get(pid, self._avg_followers(existing_ads))
                # Skip pages obviously outside follower range
                if fan_count > 0 and not (self.min_followers <= fan_count <= self.max_followers):
                    continue

                page_follower_count, page_ads = browser.get_page_ads(
                    pid, max_ads=200,
                    known_followers=self._page_followers.get(pid, 0))
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

                self._visited_page_ids.add(pid)
                total_now = len(self._page_ads[pid])
                logger.info(
                    f"  {pid}: {total_now} total ads "
                    f"({added} new from page visit)"
                )

        except KeyboardInterrupt:
            logger.warning("Interrupted — evaluating partial results...")

        # Pre-check Shopify only for pages that have >12 active ads within the
        # lookback window — no point hitting HTTP for pages that will be filtered out.
        # _disk_cache and _SHOPIFY_CACHE_FILE were already loaded before Phase 1.
        _SHOPIFY_AD_THRESHOLD = 12
        shopify_cache: dict[str, tuple[bool, str]] = {}
        try:
            from .shopify import batch_check_shopify, decode_facebook_redirect as _dfr
            import json as _json

            # Reload disk cache in case new entries were written during Phase 1 quick checks
            try:
                if os.path.exists(_SHOPIFY_CACHE_FILE):
                    _raw = _json.load(open(_SHOPIFY_CACHE_FILE))
                    _disk_cache = {k: tuple(v) for k, v in _raw.items()}
                    logger.debug(f"Shopify disk cache: {len(_disk_cache)} entries loaded")
            except Exception:
                pass

            all_real_urls: set[str] = set()
            for pid, _ads in self._page_ads.items():
                # Count distinct recent creatives for this page.
                # _ad_versions == collation_count in Apify mode (can be 300+ for 1 creative),
                # so sum() would trigger for every store with even one viral ad. Use len().
                recent_count = sum(
                    1 for a in _ads if _within_days(a, self.days)
                )
                if recent_count <= _SHOPIFY_AD_THRESHOLD:
                    continue  # too few recent creatives — skip Shopify check entirely
                for _ad in _ads:
                    cta = _ad.get("_cta_url", "")
                    if cta:
                        real = _dfr(cta)
                        if real and real.startswith("http"):
                            all_real_urls.add(real)

            # Apply disk cache — skip URLs we've already checked
            shopify_cache.update(_disk_cache)
            uncached_urls = {u for u in all_real_urls if u not in shopify_cache}

            if uncached_urls:
                logger.info(
                    f"Pre-checking {len(uncached_urls)} store URLs for Shopify "
                    f"({len(all_real_urls) - len(uncached_urls)} served from cache, "
                    f"pages with >{_SHOPIFY_AD_THRESHOLD} active ads in last "
                    f"{self.days}d, multithreaded HTTP)..."
                )
                new_results = batch_check_shopify(uncached_urls)
                shopify_cache.update(new_results)
                confirmed = sum(1 for u in all_real_urls if shopify_cache.get(u, (False,))[0])
                logger.info(f"  Shopify confirmed: {confirmed}/{len(all_real_urls)}")

                # Persist newly checked results to disk
                try:
                    _to_save = {k: list(v) for k, v in shopify_cache.items()}
                    with open(_SHOPIFY_CACHE_FILE, "w") as _sf:
                        _json.dump(_to_save, _sf)
                    logger.debug(f"Shopify cache saved: {len(_to_save)} entries")
                except Exception:
                    pass
            elif all_real_urls:
                logger.info(
                    f"Shopify: all {len(all_real_urls)} URLs served from disk cache"
                )
        except Exception as e:
            logger.debug(f"Shopify pre-check failed: {e}")

        # Evaluate with browser still alive so it can visit store URLs
        try:
            results = self._evaluate_pages(browser, shopify_cache=shopify_cache)
        finally:
            if _ap_pool is not None:
                _ap_pool.shutdown(wait=False)
            browser.stop()

        return results

    def _avg_followers(self, ads: list[dict]) -> int:
        # Check both _follower_count (browser/API ads) and page_followers (Apify ads)
        counts = [
            a.get("_follower_count") or a.get("page_followers") or 0
            for a in ads
        ]
        counts = [c for c in counts if c > 0]
        return int(sum(counts) / len(counts)) if counts else 0

    def _evaluate_pages(
        self, browser=None, shopify_cache: dict = None
    ) -> list[WinningProduct]:
        winners = []
        shopify_cache = shopify_cache or {}
        stats = {"total": 0, "blocked": 0, "junk": 0, "niche_miss": 0,
                 "follower_range": 0, "no_recent": 0, "low_ads": 0,
                 "high_price": 0, "no_cta": 0, "passed": 0}

        for page_id, ads in self._page_ads.items():
            stats["total"] += 1
            # Filter blocklisted platforms — catches entries loaded from old state files
            if any(term in page_id.lower() for term in _PLATFORM_BLOCKLIST):
                stats["blocked"] += 1; continue

            # Skip pages already exported as winners in a previous run
            if page_id in self._seen_winner_ids:
                logger.debug(f"  SKIP {page_id}: already listed as winner in a previous run")
                continue

            # Find the best page_name: skip junk names like "Log in" from login wall
            page_name = ""
            for _ad in ads:
                _n = (_ad.get("page_name") or "").strip()
                if _n and _n.lower() not in _JUNK_PAGE_NAMES:
                    page_name = _n
                    break

            if any(term in page_name.lower() for term in _PLATFORM_BLOCKLIST):
                stats["blocked"] += 1; continue
            # Filter login-wall artifacts and known big brands
            if _is_junk_page(page_name, page_id):
                logger.debug(f"  SKIP {page_id}: junk/big-brand ({page_name!r})")
                stats["junk"] += 1; continue

            # Niche relevance filter — fast keyword check, no API calls needed
            if self.niche:
                niche_lower = self.niche.lower()
                niche_key = next((k for k in NICHE_TERMS if k in niche_lower), None)
                if niche_key:
                    terms = NICHE_TERMS[niche_key]
                    all_text = (page_name + " " + " ".join(
                        b for a in ads[:30]
                        for b in (a.get("ad_creative_bodies") or [])
                    )).lower()
                    if not any(t in all_text for t in terms):
                        logger.debug(f"  SKIP {page_id}: not in niche '{self.niche}'")
                        stats["niche_miss"] += 1; continue

            fan_count = self._page_followers.get(page_id, self._avg_followers(ads))
            if fan_count > 0 and not (self.min_followers <= fan_count <= self.max_followers):
                logger.debug(f"  SKIP {page_id}: {fan_count} followers outside range")
                stats["follower_range"] += 1; continue

            # Upper ad count cap — pages with too many total active ads are
            # large commercial brands or media buyers, not dropshipping stores.
            # (Validated workflow threshold: 250)
            if self.max_total_ads > 0:
                # Use count of distinct creatives, not sum of collation_count/ad_versions.
                # In Apify mode _ad_versions == collation_count (can be 300+ for one creative),
                # so sum() would incorrectly filter small stores with a single viral creative.
                page_total_creatives = len(ads)
                if page_total_creatives > self.max_total_ads:
                    logger.debug(
                        f"  SKIP {page_id}: {page_total_creatives} distinct creatives "
                        f"> max {self.max_total_ads} (likely large brand)"
                    )
                    stats["too_many_ads"] = stats.get("too_many_ads", 0) + 1
                    continue

            # Active ratio filter — skip pages where most ads have already stopped.
            # N8N validated threshold: ≥85% of the page's ads must be is_active=True.
            # For browser/API mode every scraped ad is live so ratio is always 1.0;
            # for Apify (active_status=all) some ads may have is_active=False.
            if self.min_active_ratio > 0:
                flagged_inactive = [a for a in ads if a.get("_is_active") is False]
                if flagged_inactive:
                    active_count = len(ads) - len(flagged_inactive)
                    ratio = active_count / len(ads)
                    if ratio < self.min_active_ratio:
                        logger.debug(
                            f"  SKIP {page_id}: active_ratio={ratio:.0%} "
                            f"< {self.min_active_ratio:.0%} "
                            f"({active_count}/{len(ads)} active)"
                        )
                        stats["low_active_ratio"] = stats.get("low_active_ratio", 0) + 1
                        continue

            recent = [a for a in ads if _within_days(a, self.days)]
            if not recent:
                logger.debug(f"  SKIP {page_id}: no ads within last {self.days} days")
                stats["no_recent"] += 1
                continue

            for cluster in cluster_page_ads(recent):
                total_versions = sum(a.get("_ad_versions", 1) for a in cluster)
                if total_versions < self.min_ads:
                    logger.debug(f"  SKIP {page_id}: {total_versions} ad versions (need {self.min_ads})")
                    stats["low_ads"] += 1; continue

                # Price filter — skip clusters where the product costs > $250
                all_text = " ".join(
                    b for a in cluster
                    for b in (a.get("ad_creative_bodies") or [])
                )
                min_price = _min_price_in_text(all_text)
                if min_price > MAX_PRODUCT_PRICE:
                    logger.debug(
                        f"  SKIP {page_id}: price ${min_price:.0f} > ${MAX_PRODUCT_PRICE}"
                    )
                    stats["high_price"] += 1; continue

                shop_now = sum(
                    1 for a in cluster
                    if a.get("_has_shop_now") or has_shop_now_cta(a)
                )
                if self.require_shop_now and shop_now == 0:
                    logger.debug(f"  SKIP {page_id}: no shop-now CTA")
                    stats["no_cta"] += 1; continue

                stats["passed"] += 1

                is_video = any(
                    (a.get("media_type") or "").upper() == "VIDEO"
                    for a in cluster
                )

                page_url = next(
                    (a["page_url"] for a in cluster if a.get("page_url")), ""
                )

                # Get the real store URL from CTA buttons (unwrap l.php if needed)
                from .shopify import decode_facebook_redirect
                # Collect ALL cta_urls in this cluster — use the first valid one
                cta_url = next(
                    (a.get("_cta_url", "") for a in cluster if a.get("_cta_url")), ""
                )
                store_url = decode_facebook_redirect(cta_url) if cta_url else ""

                # Step 1: look up pre-checked cache (multithreaded HTTP done earlier)
                is_shopify, shopify_reason = False, "no url"
                if store_url and store_url in shopify_cache:
                    is_shopify, shopify_reason = shopify_cache[store_url]
                elif store_url:
                    # Not in cache (shouldn't happen often) — check inline
                    is_shopify, shopify_reason = is_shopify_store(store_url)

                # Step 2: browser-based fallback — only when HTTP check hit a network
                # error (redirect chain, bot wall, etc). Don't re-visit confirmed stores
                # or confirmed non-Shopify — that wastes browser time on every cluster.
                shopify_via_browser = False
                if browser and cta_url and not is_shopify and "request error" in shopify_reason:
                    is_shopify, shopify_reason = browser.check_shopify_via_browser(cta_url)
                    shopify_via_browser = is_shopify
                elif is_shopify:
                    shopify_via_browser = False  # confirmed via HTTP, not browser

                start_dates = sorted(
                    {a["_start_date"] for a in cluster if a.get("_start_date")}
                )
                platforms = sorted(
                    {p for a in cluster for p in (a.get("publisher_platforms") or [])}
                )
                sample = cluster[0]
                bodies = sample.get("ad_creative_bodies") or []
                page_name_final = sample.get("page_name", page_id)

                w = WinningProduct(
                    page_id=page_id,
                    page_name=page_name_final,
                    page_followers=fan_count,
                    page_url=page_url,
                    store_url=store_url,
                    ad_count=total_versions,
                    total_page_ads=len(ads),
                    has_shop_now=shop_now > 0,
                    is_shopify=is_shopify,
                    shopify_confirmed_via_browser=shopify_via_browser,
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
                # ── New signals attached after construction ──────────────────────────
                # Saturation: max pages seen per keyword that matched this page
                matched_kws = self._page_keywords.get(page_id, set())
                # _keyword_page_density counts all pages including this winner itself,
                # so subtract 1 to get the number of *other* pages (true competitors).
                w.saturation_count = max(
                    (max(0, self._keyword_page_density.get(kw, 0) - 1) for kw in matched_kws),
                    default=0,
                )
                # Page age: days since the oldest known ad (proxy for how new the page is)
                if start_dates:
                    try:
                        earliest_dt = datetime.strptime(start_dates[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        w.page_age_days = (datetime.now(timezone.utc) - earliest_dt).days
                    except (ValueError, TypeError):
                        w.page_age_days = None
                else:
                    w.page_age_days = None
                # Freshness: what fraction of total ad versions are within the lookback window
                recent_versions = sum(a.get("_ad_versions", 1) for a in recent)
                total_versions_all = sum(a.get("_ad_versions", 1) for a in ads)
                w.recent_ad_count = recent_versions
                w.freshness_rate = round(recent_versions / total_versions_all, 4) if total_versions_all > 0 else 0.0

                # Market stage: classify by age + saturation
                # too_early  < 3 days   — not enough data to validate
                # emerging   3–13 days  — active and growing (sweet spot)
                # stable     14–44 days — proven ROI, still worth entering
                # mature     45+ days   — established; check competition carefully
                _age = getattr(w, "page_age_days", None)
                _sat = getattr(w, "saturation_count", 0)
                if _age is None:
                    w.market_stage = "unknown"
                elif _age < 3:
                    w.market_stage = "too_early"
                elif _age < 14:
                    w.market_stage = "emerging"
                elif _age < 45 or _sat <= 10:
                    w.market_stage = "stable"
                else:
                    w.market_stage = "mature"

                winners.append(w)

        # Score everything, attach score, sort by score descending
        from .scoring import score_product, WINNER_THRESHOLD
        for w in winners:
            w.score, w.score_breakdown = score_product(w)

        winners.sort(key=lambda w: -w.score)

        # AI dropshipping check — only on top candidates to avoid hundreds of calls
        if self.use_ai:
            for w in winners[:30]:
                bodies = w.sample_ad_body.split("\n") if w.sample_ad_body else []
                is_drop, drop_reason = is_dropshipping_page(w.page_name, bodies)
                if not is_drop:
                    # Penalise heavily so it drops below winner threshold
                    w.score = max(0.0, round(w.score - 4.0, 2))
                    w.score_breakdown["ai_filter"] = -4.0
                    logger.debug(f"  AI penalised {w.page_name}: {drop_reason}")

            # Re-sort after penalties
            winners.sort(key=lambda w: -w.score)

        # ── Risk flags, market intel, competitors, opportunity tier ──────────
        from .risk import detect_risks
        from .market_intel import analyze_market, find_competitors, classify_opportunity

        # Compute global market intel once (covers all pages in the scan)
        global_market = analyze_market(self._page_ads, self._page_followers)

        for w in winners:
            # Product risk flags
            bodies_list = [w.sample_ad_body] if w.sample_ad_body else []
            w.risk_flags = detect_risks(
                page_name=w.page_name,
                ad_bodies=bodies_list,
                store_url=getattr(w, "store_url", "") or "",
                product_title=getattr(w, "sourcing_data", {}).get("shopify_product_title", ""),
            )

            # Market intelligence (product-level: only pages sharing this winner's keywords)
            matched_kws  = set(w.keywords_matched or [])
            product_pages = {
                pid: ads for pid, ads in self._page_ads.items()
                if self._page_keywords.get(pid, set()) & matched_kws
            }
            w.market_intel = analyze_market(product_pages, self._page_followers)

            # Competitor list
            w.competitors = find_competitors(
                winner_page_id=w.page_id,
                winner_keywords=matched_kws,
                all_page_ads=product_pages,
                all_page_followers=self._page_followers,
            )

            # Opportunity tier
            w.opportunity_tier, w.opportunity_label = classify_opportunity(
                score=w.score,
                risk_flags=w.risk_flags,
                market=w.market_intel,
                market_stage=getattr(w, "market_stage", ""),
            )

        logger.info(
            f"Filter stats — total:{stats['total']} blocked:{stats['blocked']} "
            f"junk:{stats['junk']} niche_miss:{stats['niche_miss']} "
            f"follower_range:{stats['follower_range']} "
            f"no_recent:{stats['no_recent']} "
            f"low_active_ratio:{stats.get('low_active_ratio', 0)} "
            f"too_many_ads:{stats.get('too_many_ads', 0)} "
            f"low_ads:{stats['low_ads']} "
            f"high_price:{stats['high_price']} "
            f"no_cta:{stats['no_cta']} passed:{stats['passed']}"
        )
        true_winners = [w for w in winners if w.score >= WINNER_THRESHOLD]
        logger.info(
            f"Scored {len(winners)} candidates → "
            f"{len(true_winners)} winners (≥{WINNER_THRESHOLD})"
        )
        for w in true_winners:
            logger.info(
                f"  ✓ {w.page_name} | score={w.score} | {w.ad_count} ads | "
                f"followers={w.page_followers} | Shopify={w.is_shopify}"
            )

        # AliExpress sourcing check — run in parallel for top candidates only
        # (no point checking everything; limits extra network overhead)
        self._run_sourcing_check(winners[:25])

        return winners  # return all so output.py can split winners vs near-misses

    def _run_sourcing_check(self, candidates: list) -> None:
        """
        Fetch AliExpress price + Shopify product info for each candidate in parallel.
        Results are attached directly to the WinningProduct objects as sourcing_data.
        Fails silently — sourcing data is supplementary, never blocks the scan.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from .aliexpress import check_product_sourcing

        if not candidates:
            return

        logger.info(f"AliExpress sourcing check for {len(candidates)} candidates...")

        def _check(w):
            try:
                store = getattr(w, "store_url", "") or ""
                keywords = getattr(w, "keywords_matched", [])
                return w, check_product_sourcing(store, keywords)
            except Exception as e:
                logger.debug(f"Sourcing check error for {getattr(w, 'page_name', '?')}: {e}")
                return w, {}

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_check, w): w for w in candidates}
            for future in as_completed(futures):
                try:
                    w, sourcing = future.result()
                    w.sourcing_data = sourcing
                    if sourcing.get("aliexpress_found"):
                        logger.info(
                            f"  AliExpress: {w.page_name} → "
                            f"${sourcing.get('aliexpress_min_price', '?')}–"
                            f"${sourcing.get('aliexpress_max_price', '?')} "
                            f"| margin {sourcing.get('margin_pct', '?')}%"
                        )
                except Exception:
                    pass
