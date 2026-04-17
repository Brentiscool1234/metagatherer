"""
AliExpress product sourcing check.

Given a product name / keyword, this module:
  1. Fetches the store's product title from Shopify's public /products.json
     endpoint (most accurate search term — avoids brand name noise).
  2. Searches AliExpress for that title and extracts price range + supplier count.
  3. Calculates estimated gross margin and break-even ROAS.

Designed to fail gracefully — AliExpress anti-bot will block some requests.
All functions return None / empty dicts rather than raising on failure.
"""

import json
import logging
import re
import time
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_session = requests.Session()
_session.headers.update(_HEADERS)

# Simple in-process cache so we don't double-fetch the same keyword
_aliexpress_cache: dict = {}
_shopify_cache: dict = {}


# ── Shopify product title lookup ──────────────────────────────────────────────

def get_shopify_product_info(store_url: str, timeout: int = 8) -> dict:
    """
    Fetch the first few products from a Shopify store's public /products.json.
    Returns dict with:
      title       str   — first product title (use for AliExpress search)
      price       float — lowest variant price in store currency (usually USD)
      products    list  — raw list of up to 5 products
    Returns {} on failure.
    """
    if not store_url:
        return {}

    cache_key = store_url
    if cache_key in _shopify_cache:
        return _shopify_cache[cache_key]

    parsed = urlparse(store_url)
    if not parsed.netloc:
        return {}
    base = f"https://{parsed.netloc}"

    try:
        resp = _session.get(
            f"{base}/products.json?limit=5",
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            _shopify_cache[cache_key] = {}
            return {}

        data = resp.json()
        products = data.get("products", [])
        if not products:
            _shopify_cache[cache_key] = {}
            return {}

        prices = []
        for product in products[:5]:
            for variant in (product.get("variants") or []):
                raw_price = variant.get("price")
                if raw_price:
                    try:
                        prices.append(float(raw_price))
                    except (ValueError, TypeError):
                        pass

        first_product = products[0]
        first_title = first_product.get("title", "")
        first_handle = first_product.get("handle", "")
        product_url = f"{base}/products/{first_handle}" if first_handle else ""

        result = {
            "title": first_title,
            "price": round(min(prices), 2) if prices else None,
            "product_url": product_url,
            "products": [p.get("title", "") for p in products[:5]],
        }
        _shopify_cache[cache_key] = result
        return result

    except Exception as e:
        logger.debug(f"Shopify products.json failed for {store_url}: {e}")
        _shopify_cache[cache_key] = {}
        return {}


# ── AliExpress search ─────────────────────────────────────────────────────────

def _clean_product_title(title: str) -> str:
    """
    Strip brand/model noise from a Shopify product title to get a generic
    AliExpress-searchable term.
    E.g. "PawComfort™ Dog Anxiety Relief Vest – Size M/L" → "dog anxiety relief vest"
    """
    # Remove trademark symbols
    title = re.sub(r"[™®©]", "", title)
    # Remove size/variant info after dash or pipe
    title = re.split(r"\s*[–—|\-]\s*", title)[0]
    # Remove words in ALL CAPS (usually brand codes)
    title = " ".join(w for w in title.split() if not (w.isupper() and len(w) > 2))
    # Normalise
    title = title.strip().lower()
    # Truncate to keep search focused
    words = title.split()
    return " ".join(words[:6])


def _keyword_overlap_score(search_term: str, html: str) -> float:
    """
    Measure how many of the search term's key words appear in AliExpress results.
    Returns 0.0–1.0 confidence that AliExpress has this product category.

    This is fuzzy matching, not exact product confirmation, but sufficient
    for dropshipping sourcing validation (we want the same type, not the same unit).
    """
    # Generic stop words to ignore in overlap scoring
    _stop = {
        "for", "the", "and", "with", "in", "of", "to", "a", "an",
        "pro", "mini", "plus", "max", "new", "hot",
    }
    search_words = {
        w.lower() for w in search_term.split()
        if len(w) >= 3 and w.lower() not in _stop
    }
    if not search_words:
        return 0.0

    html_lower = html.lower()
    matched = sum(1 for w in search_words if w in html_lower)
    return round(matched / len(search_words), 2)


def search_aliexpress(search_term: str, timeout: int = 12) -> dict:
    """
    Search AliExpress for a product and return sourcing data.

    Returns dict with:
      found           bool  — True if any price results were extracted
      min_price       float — cheapest listing found (USD)
      max_price       float — most expensive listing in top results (USD)
      avg_price       float — average of top-10 prices
      supplier_count  int   — how many distinct listings were found
      search_term     str   — the actual term searched
    Returns {"found": False} on failure / block.
    """
    if not search_term or len(search_term.strip()) < 3:
        return {"found": False}

    search_term = search_term.strip().lower()
    if search_term in _aliexpress_cache:
        return _aliexpress_cache[search_term]

    url = (
        "https://www.aliexpress.com/wholesale"
        f"?SearchText={quote(search_term)}&SortType=default"
    )

    try:
        resp = _session.get(url, timeout=timeout)
        if resp.status_code != 200:
            result = {"found": False}
            _aliexpress_cache[search_term] = result
            return result

        html = resp.text
        prices, item_urls = _extract_prices_from_html(html)

        # Keyword overlap: how many search words appear in the page?
        # High overlap = AliExpress definitely has this product category.
        match_confidence = _keyword_overlap_score(search_term, html)

        if prices:
            prices_sorted = sorted(prices)
            result = {
                "found": True,
                "min_price": round(prices_sorted[0], 2),
                "max_price": round(prices_sorted[-1], 2),
                "avg_price": round(sum(prices_sorted) / len(prices_sorted), 2),
                "supplier_count": len(prices_sorted),
                "match_confidence": match_confidence,
                "search_term": search_term,
                "item_url": item_urls[0] if item_urls else "",
            }
        else:
            # Even without price data, high keyword overlap means product exists
            result = {
                "found": match_confidence >= 0.7,
                "match_confidence": match_confidence,
                "search_term": search_term,
                "item_url": item_urls[0] if item_urls else "",
            }

        _aliexpress_cache[search_term] = result
        return result

    except Exception as e:
        logger.debug(f"AliExpress search failed for '{search_term}': {e}")
        result = {"found": False}
        _aliexpress_cache[search_term] = result
        return result


def _extract_prices_from_html(html: str) -> tuple[list, list]:
    """
    Try multiple extraction patterns on the AliExpress search page HTML.
    Returns (prices: list[float], item_urls: list[str]).
    AliExpress embeds product data in several formats; we try them all.
    """
    prices = []
    item_urls = []

    # ── Pattern 1: window.runParams embedded JSON ────────────────────────────
    # AliExpress server-side renders product data into window.runParams for SEO
    m = re.search(
        r'window\.runParams\s*=\s*(\{.+?\});\s*(?:var\s+\w|window\.|</script>)',
        html,
        re.DOTALL,
    )
    if m:
        try:
            data = json.loads(m.group(1))
            items = (
                data.get("data", {})
                .get("result", {})
                .get("mods", {})
                .get("itemList", {})
                .get("content", [])
            )
            for item in items[:15]:
                price_info = item.get("prices", {})
                sale = price_info.get("salePrice", {})
                raw = sale.get("minPrice") or sale.get("value")
                if raw:
                    try:
                        v = float(str(raw).replace(",", "."))
                        if 0.50 <= v <= 250:  # align upper bound with MAX_PRODUCT_PRICE
                            prices.append(v)
                            # Extract item URL — productId or itemId field
                            item_id = (item.get("productId") or item.get("itemId")
                                       or item.get("id") or "")
                            if item_id and not item_urls:
                                item_urls.append(
                                    f"https://www.aliexpress.com/item/{item_id}.html"
                                )
                    except (ValueError, TypeError):
                        pass
        except (json.JSONDecodeError, AttributeError, KeyError):
            pass

    if prices:
        # Also try regex URL extraction as a fallback for item URLs
        if not item_urls:
            url_matches = re.findall(
                r'https://www\.aliexpress\.com/item/(\d+)\.html', html
            )
            if url_matches:
                item_urls.append(
                    f"https://www.aliexpress.com/item/{url_matches[0]}.html"
                )
        return prices, item_urls

    # ── Pattern 2: JSON salePrice fields inline ──────────────────────────────
    for m in re.finditer(
        r'"salePrice"\s*:\s*\{[^}]*?"minPrice"\s*:\s*"?([\d.]+)"?', html
    ):
        try:
            v = float(m.group(1))
            if 0.30 <= v <= 300:
                prices.append(v)
        except ValueError:
            pass

    if prices:
        url_matches = re.findall(r'https://www\.aliexpress\.com/item/(\d+)\.html', html)
        if url_matches:
            item_urls.append(f"https://www.aliexpress.com/item/{url_matches[0]}.html")
        return prices, item_urls

    # ── Pattern 3: generic "price" JSON fields (last resort) ────────────────
    for m in re.finditer(r'"price"\s*:\s*"?([\d.]+)"?', html):
        try:
            v = float(m.group(1))
            if 0.50 <= v <= 150:  # tight bounds to avoid random numbers
                prices.append(v)
        except ValueError:
            pass

    url_matches = re.findall(r'https://www\.aliexpress\.com/item/(\d+)\.html', html)
    if url_matches:
        item_urls.append(f"https://www.aliexpress.com/item/{url_matches[0]}.html")

    return prices, item_urls


# ── Margin calculator ─────────────────────────────────────────────────────────

def estimate_margin(store_price: float, source_price: float) -> dict:
    """
    Calculate estimated gross margin and break-even ROAS.

    store_price  — what the consumer pays (from Shopify products.json)
    source_price — what the dropshipper pays to AliExpress (min price)

    Returns dict with margin_pct, gross_profit, break_even_roas.
    Returns {} if either price is missing or invalid.
    """
    if not store_price or not source_price or store_price <= 0 or source_price <= 0:
        return {}
    if source_price >= store_price:
        # Source is more expensive than selling price — bad product or mismatch
        return {
            "margin_pct": 0.0,
            "gross_profit": round(store_price - source_price, 2),
            "break_even_roas": None,
            "viable": False,
        }

    gross = store_price - source_price
    margin_pct = (gross / store_price) * 100
    # Break-even ROAS = 1 / gross_margin_fraction
    # e.g. 70% margin → break-even at 1.43x ROAS
    # e.g. 30% margin → break-even at 3.33x ROAS
    break_even_roas = round(100 / margin_pct, 2) if margin_pct > 0 else None

    return {
        "margin_pct": round(margin_pct, 1),
        "gross_profit": round(gross, 2),
        "break_even_roas": break_even_roas,
        "viable": margin_pct >= 30,  # <30% gross margin = very tight for paid ads
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def check_product_sourcing(store_url: str, fallback_keyword: str) -> dict:
    """
    Full sourcing check for a winning product.

    1. Fetches product info from Shopify /products.json (gets real product name + price)
    2. Searches AliExpress using the product title (falling back to fallback_keyword)
    3. Calculates margin estimate

    Returns a flat dict ready to merge into WinningProduct.to_dict():
      shopify_product_title   str
      shopify_product_price   float | None
      aliexpress_found        bool
      aliexpress_min_price    float | None
      aliexpress_max_price    float | None
      aliexpress_suppliers    int
      aliexpress_search_term  str
      margin_pct              float | None
      gross_profit            float | None
      break_even_roas         float | None
      margin_viable           bool | None
    """
    result = {
        "shopify_product_title": "",
        "shopify_product_url": "",
        "shopify_product_price": None,
        "aliexpress_found": False,
        "aliexpress_product_url": "",
        "aliexpress_match_confidence": None,
        "aliexpress_min_price": None,
        "aliexpress_max_price": None,
        "aliexpress_suppliers": 0,
        "aliexpress_search_term": "",
        "margin_pct": None,
        "gross_profit": None,
        "break_even_roas": None,
        "margin_viable": None,
    }

    # Step 1: Shopify product info
    shopify_info = {}
    if store_url:
        shopify_info = get_shopify_product_info(store_url)

    store_price = shopify_info.get("price")
    product_title = shopify_info.get("title", "")

    if product_title:
        result["shopify_product_title"] = product_title
    if store_price:
        result["shopify_product_price"] = store_price
    if shopify_info.get("product_url"):
        result["shopify_product_url"] = shopify_info["product_url"]

    # Step 2: pick best AliExpress search term
    if product_title:
        search_term = _clean_product_title(product_title)
    elif fallback_keyword:
        # Use the most specific (longest) keyword as fallback
        kws = sorted(
            (k for k in (fallback_keyword if isinstance(fallback_keyword, list)
                         else [fallback_keyword]) if k),
            key=len, reverse=True,
        )
        search_term = kws[0] if kws else ""
    else:
        search_term = ""

    result["aliexpress_search_term"] = search_term

    # Step 3: AliExpress search
    if search_term:
        ali = search_aliexpress(search_term)
        result["aliexpress_match_confidence"] = ali.get("match_confidence")
        if ali.get("item_url"):
            result["aliexpress_product_url"] = ali["item_url"]
        if ali.get("found"):
            result["aliexpress_found"] = True
            result["aliexpress_min_price"] = ali.get("min_price")
            result["aliexpress_max_price"] = ali.get("max_price")
            result["aliexpress_suppliers"] = ali.get("supplier_count", 0)

    # Step 4: Margin calculation
    if store_price and result["aliexpress_min_price"]:
        margin = estimate_margin(store_price, result["aliexpress_min_price"])
        if margin:
            result["margin_pct"] = margin.get("margin_pct")
            result["gross_profit"] = margin.get("gross_profit")
            result["break_even_roas"] = margin.get("break_even_roas")
            result["margin_viable"] = margin.get("viable")

    return result
