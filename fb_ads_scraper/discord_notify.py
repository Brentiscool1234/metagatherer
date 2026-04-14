"""
Discord webhook notifications for MetaGatherer winners.
Fails silently — never blocks the scan.
"""

import logging

logger = logging.getLogger(__name__)


def notify_winner(webhook_url: str, product, is_winner: bool = True) -> None:
    """
    POST a rich embed to *webhook_url* for a winning/near-miss product.

    Color:
      - green  (0x2ecc71) for winners
      - yellow (0xf1c40f) for near-misses
    """
    if not webhook_url:
        return
    try:
        import urllib.request
        import json

        score = getattr(product, "score", 0.0)
        page_name = getattr(product, "page_name", "Unknown")
        ad_count = getattr(product, "ad_count", 0)
        page_followers = getattr(product, "page_followers", 0)
        is_shopify = getattr(product, "is_shopify", False)
        store_url = getattr(product, "store_url", "") or ""
        page_url = getattr(product, "page_url", "") or ""
        keywords = ", ".join(getattr(product, "keywords_matched", [])[:6])
        sourcing = getattr(product, "sourcing_data", {}) or {}
        margin_pct = sourcing.get("margin_pct")

        color = 0x2ECC71 if is_winner else 0xF1C40F
        label = "WINNER" if is_winner else "NEAR-MISS"

        fields = [
            {"name": "Score", "value": f"`{score:.2f}/10`", "inline": True},
            {"name": "Ads", "value": str(ad_count), "inline": True},
            {"name": "Followers", "value": f"{page_followers:,}", "inline": True},
            {"name": "Shopify", "value": "Yes" if is_shopify else "No", "inline": True},
        ]
        if store_url:
            fields.append({"name": "Store", "value": store_url[:200], "inline": False})
        if keywords:
            fields.append({"name": "Keywords", "value": keywords, "inline": False})
        if margin_pct is not None:
            be_roas = sourcing.get("break_even_roas", "?")
            fields.append({
                "name": "Margin",
                "value": f"{margin_pct}% (BE ROAS: {be_roas}x)",
                "inline": True,
            })

        embed = {
            "title": f"[{label}] {page_name}",
            "url": page_url or None,
            "color": color,
            "fields": fields,
            "footer": {"text": "MetaGatherer"},
        }
        # Remove None url to avoid Discord validation error
        if not embed["url"]:
            del embed["url"]

        payload = json.dumps({"embeds": [embed]}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
    except Exception as e:
        logger.debug(f"Discord notify failed (silent): {e}")
