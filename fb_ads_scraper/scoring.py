"""
Scoring engine — rates each WinningProduct candidate out of 10.0.

Criteria and max points:
  ad_count      2.5  — volume of active ad versions signals commitment
  shopify       2.5  — Shopify = dropshipping infra confirmed
  video_ads     1.0  — video converts better; winners usually test video
  shop_now_cta  1.0  — direct purchase CTA present
  followers     1.5  — sweet spot 50–500: early-stage brand scaling fast
  cross_platform 0.5 — running on Instagram too = larger budget / reach
  recent_start  1.0  — recently launched ads = currently scaling product
                ───
  max total    10.0

Score ≥ 7  → winner
Score 5–6.9 → near-miss (show but flag)
Score < 5  → excluded
"""

from datetime import datetime, timezone

WINNER_THRESHOLD = 7.0
NEAR_MISS_THRESHOLD = 5.0

CRITERIA = {
    "ad_count":      ("Active Ads",    2.5),
    "shopify":       ("Shopify",       2.5),
    "video_ads":     ("Video",         1.0),
    "shop_now_cta":  ("Shop Now CTA",  1.0),
    "followers":     ("Followers",     1.5),
    "cross_platform":("Cross-Platform",0.5),
    "recent_start":  ("Recent Start",  1.0),
}


def score_product(w) -> tuple[float, dict]:
    """
    Score a WinningProduct. Returns (total_score, breakdown_dict).
    breakdown keys match CRITERIA keys above.
    """
    b = {}

    # ── 1. Ad count (2.5 pts) ─────────────────────────────────────
    c = w.ad_count
    b["ad_count"] = 2.5 if c >= 30 else 2.0 if c >= 20 else 1.5 if c >= 12 else 1.0 if c >= 6 else 0.0

    # ── 2. Shopify (2.5 pts) ──────────────────────────────────────
    if w.is_shopify and getattr(w, "shopify_confirmed_via_browser", False):
        b["shopify"] = 2.5          # confirmed by actually visiting the store
    elif w.is_shopify:
        b["shopify"] = 1.5          # confirmed via HTTP headers only
    else:
        b["shopify"] = 0.0

    # ── 3. Video ads (1 pt) ───────────────────────────────────────
    b["video_ads"] = 1.0 if w.is_video else 0.0

    # ── 4. Shop Now CTA (1 pt) ────────────────────────────────────
    b["shop_now_cta"] = 1.0 if w.has_shop_now else 0.0

    # ── 5. Followers (1.5 pts) ────────────────────────────────────
    fc = w.page_followers
    if 50 <= fc <= 500:
        b["followers"] = 1.5        # sweet spot
    elif 10 <= fc <= 2000:
        b["followers"] = 1.0        # in range but outside sweet spot
    elif fc == 0:
        b["followers"] = 0.5        # unknown — benefit of the doubt
    else:
        b["followers"] = 0.0        # outside target range

    # ── 6. Cross-platform (0.5 pts) ───────────────────────────────
    platforms = {p.lower() for p in (w.publisher_platforms or [])}
    b["cross_platform"] = 0.5 if len(platforms - {"facebook"}) > 0 else 0.0

    # ── 7. Recent ad start (1 pt) ─────────────────────────────────
    recent = 0.0
    if w.ad_start_dates:
        try:
            latest = max(w.ad_start_dates)
            dt = datetime.strptime(latest, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).days
            recent = 1.0 if age <= 14 else 0.75 if age <= 30 else 0.5 if age <= 90 else 0.0
        except (ValueError, TypeError):
            recent = 0.25
    b["recent_start"] = recent

    total = round(min(sum(b.values()), 10.0), 2)
    return total, b
