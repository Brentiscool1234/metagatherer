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
    """Return the smallest dollar amount found in text, or 0 if none found."""
    prices = []
    for m in _PRICE_RE.finditer(text):
        try:
            val = float(m.group(1).replace(",", ""))
            if 1 < val < 50_000:   # ignore $0 and absurd numbers
                prices.append(val)
        except ValueError:
            pass
    return min(prices) if prices else 0.0


def _is_blocked(raw: dict) -> bool:
    """Return True if this ad should be skipped (marketplace, food, chemicals)."""
    check = " ".join([
        (raw.get("page_name") or "").lower(),
        (raw.get("page_url") or "").lower(),
        (raw.get("ad_body") or "").lower(),
    ])
    if any(term in check for term in _PLATFORM_BLOCKLIST):
        return True
    body = (raw.get("ad_body") or "").lower()
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
        "_cta_url": raw.get("cta_url", ""),
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
                f"https://www.facebook.com/ads/library/?active_status=active"
                f"&ad_type=all&country=US&q={self.page_id}&search_type=page"
                if not self.page_id.isdigit() else
                f"https://www.facebook.com/ads/library/?active_status=active"
                f"&ad_type=all&country=US&search_type=page&view_all_page_id={self.page_id}"
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
        }


class FBAdsScraper:
    def __init__(
        self,
        countries: list[str] = None,
        days: int = 7,
        min_ads: int = 5,
        min_followers: int = 10,
        max_followers: int = 2000,
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
        }

    def run(self, extra_keywords: list[str] = None) -> list[WinningProduct]:
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

        # ── API fast-path detection ───────────────────────────────────────────
        # If FB_ACCESS_TOKEN is set, Phase 1 runs via the official API (no
        # browser needed — 10-30× faster). Phase 2 still needs the browser for
        # accurate active-ad counts. Get a token at:
        # developers.facebook.com → My Apps → Tools → Graph API Explorer
        _fb_token = os.getenv("FB_ACCESS_TOKEN", "").strip()
        _api_client = None
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

        browser = AdsLibraryBrowser(countries=self.countries, headless=self.headless)
        browser.start()

        try:
            # ── Phase 1: Keyword sweep ────────────────────────────────────
            logger.info(
                "Phase 1: Keyword sweep"
                + (" [API fast-mode]" if _api_client else " [browser mode]")
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

            while queue and total < self.max_keywords:
                keyword, depth = queue.popleft()
                if keyword in self._searched_keywords:
                    continue
                self._searched_keywords.add(keyword)
                total += 1

                logger.info(f"[{depth}] '{keyword}' ({total}/{self.max_keywords})")

                if _api_client:
                    raw_ads = _api_search_keyword(
                        _api_client, keyword,
                        countries=self.countries,
                        limit=min(self.max_ads_per_keyword, 200),
                    )
                else:
                    raw_ads = browser.search_keyword(keyword, max_ads=self.max_ads_per_keyword)

                new_ads = []

                for raw in raw_ads:
                    key = raw.get("_key", "") or raw.get("id", "")
                    if not key or key in self._seen_keys:
                        continue
                    if _is_blocked(raw):
                        self._seen_keys.add(key)
                        continue
                    self._seen_keys.add(key)
                    # API ads are already in standard format; browser ads need conversion
                    if _api_client:
                        ad = _api_ad_to_standard(raw, keyword)
                    else:
                        ad = _to_standard_ad(raw, keyword)
                    page_id = ad.get("page_id", "")
                    if page_id and page_id != "unknown":
                        self._page_ads[page_id].append(ad)
                        self._page_keywords[page_id].add(keyword)
                        new_ads.append(ad)
                        bodies = ad.get("ad_creative_bodies") or []
                        body = bodies[0] if bodies else ""
                        if body:
                            all_bodies_for_ai.append(body)
                        pname = ad.get("page_name", "")
                        if pname:
                            all_page_names_for_ai.append(pname)

                if depth < self.max_keyword_depth and new_ads:
                    expanded = False
                    if self.use_ai and all_bodies_for_ai:
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
                    # Wait in a tight loop until resume or stop
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
                            break  # breaks inner while; outer while exits next iteration
                    else:
                        continue  # keep scanning
                    break  # stop was received while paused — exit Phase 1

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

        # Pre-check all CTA URLs for Shopify in parallel (HTTP, no browser needed)
        shopify_cache: dict[str, tuple[bool, str]] = {}
        try:
            from .shopify import batch_check_shopify, decode_facebook_redirect as _dfr
            all_real_urls: set[str] = set()
            for _ads in self._page_ads.values():
                for _ad in _ads:
                    cta = _ad.get("_cta_url", "")
                    if cta:
                        real = _dfr(cta)
                        if real and real.startswith("http"):
                            all_real_urls.add(real)
            if all_real_urls:
                logger.info(
                    f"Pre-checking {len(all_real_urls)} store URLs for Shopify "
                    f"(multithreaded HTTP)..."
                )
                shopify_cache = batch_check_shopify(all_real_urls)
                confirmed = sum(1 for v in shopify_cache.values() if v[0])
                logger.info(f"  Shopify confirmed: {confirmed}/{len(all_real_urls)}")
        except Exception as e:
            logger.debug(f"Shopify pre-check failed: {e}")

        # Evaluate with browser still alive so it can visit store URLs
        try:
            results = self._evaluate_pages(browser, shopify_cache=shopify_cache)
        finally:
            browser.stop()

        return results

    def _avg_followers(self, ads: list[dict]) -> int:
        counts = [a["_follower_count"] for a in ads if a.get("_follower_count", 0) > 0]
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
                        b for a in ads[:5]
                        for b in (a.get("ad_creative_bodies") or [])
                    )).lower()
                    if not any(t in all_text for t in terms):
                        logger.debug(f"  SKIP {page_id}: not in niche '{self.niche}'")
                        stats["niche_miss"] += 1; continue

            fan_count = self._page_followers.get(page_id, self._avg_followers(ads))
            if fan_count > 0 and not (self.min_followers <= fan_count <= self.max_followers):
                logger.debug(f"  SKIP {page_id}: {fan_count} followers outside range")
                stats["follower_range"] += 1; continue

            recent = [a for a in ads if _within_days(a, max(self.days * 10, 90))]
            if not recent:
                recent = ads

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

                # Step 2: browser-based fallback — visit the actual store URL
                shopify_via_browser = False
                if browser and cta_url and not is_shopify:
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

        logger.info(
            f"Filter stats — total:{stats['total']} blocked:{stats['blocked']} "
            f"junk:{stats['junk']} niche_miss:{stats['niche_miss']} "
            f"follower_range:{stats['follower_range']} low_ads:{stats['low_ads']} "
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
