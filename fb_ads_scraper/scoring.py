"""
Scoring engine — rates each WinningProduct candidate out of 10.0.

Criteria and max points:
  ad_versions    3.0  — active ad count = real testing budget (14+ min, 20+ good, 30+ great)
  shopify        2.5  — Shopify = dropshipping infra confirmed
  ad_age         1.5  — running 14+ days = proven ROI, not just testing
  ad_expansion   1.0  — still launching new creatives = momentum / active scaling
  video_ads      1.0  — video converts better; serious advertisers use video
  followers      1.0  — sweet spot 50–500: early-stage brand scaling fast
  cross_platform 0.5  — running on Instagram too = larger budget / reach
  shop_now_cta   0.5  — direct purchase CTA present
               ───
  base max      10.0  (hard-capped after bonuses/penalties)

Bonuses / penalties applied after the base score:
  price_viab    +0.25 price $25–120 (viable Facebook margin)
               -0.25  price $20–24 (borderline)
               -0.5   price < $20  (too cheap for FB ad economics)
  page_age      +0.5  very new page (< 30 days) / +0.25 relatively new (< 90 days)
               -0.5   old page (> 180 days) — market already saturated
  freshness     +0.5  most ads fresh (> 70%) / +0.25 mostly fresh (> 40%)
  competition   +0.25 moderate competitors (3–10) = proof of demand
               -0.75  saturated (> 10 pages)
               -1.5   very saturated (> 20 pages)

Score ≥ 6  → winner
Score 4–5.9 → near-miss (show but flag)
Score < 4  → excluded

Validation factors (from spec):
  1. Active ad count   — 0–5 weak, 6–13 moderate, 14+ strong
  2. Run duration      — <3 days too early, 7+ good, 14+ very good, 30+ mature/stable
  3. Expansion signal  — new ads added after launch = advertiser scaling = strongest momentum sign
  4. Follower count    — supporting signal only; 10+ positive, 100+ credible
  5. Competition count — 3–10 = proof of demand; >20 = saturation risk
"""

from datetime import datetime, timezone

WINNER_THRESHOLD    = 6.0
NEAR_MISS_THRESHOLD = 4.0

CRITERIA = {
    "ad_versions":   ("Ad Creatives",   3.0),
    "shopify":       ("Shopify",        2.5),
    "ad_age":        ("Ad Age",         1.5),
    "ad_expansion":  ("Expansion",      1.0),
    "video_ads":     ("Video",          1.0),
    "followers":     ("Followers",      1.0),
    "cross_platform":("Cross-Platform", 0.5),
    "shop_now_cta":  ("Shop Now CTA",   0.5),
}


def score_product(w) -> tuple[float, dict]:
    """
    Score a WinningProduct. Returns (total_score, breakdown_dict).
    breakdown keys match CRITERIA keys above.
    """
    b = {}

    # ── 1. Ad versions / creatives (3.0 pts) ─────────────────────────────────
    # For Apify data: ad_count = sum of collation_count across the page's ads.
    # collation_count = "N adsets use this creative" in the Ads Library UI.
    #
    # Modern Meta broad/ASC/DCT targeting: advertisers run 1–5 creatives each
    # duplicated across many adsets (high collation_count) rather than launching
    # dozens of distinct ads.  A single creative with collation_count=14 is
    # identical in signal strength to 14 separate ads in the old model.
    #
    # Thresholds:
    # <4    = just testing — very risky, skip
    # 4–6   = early test (few adsets, low spend commitment)
    # 7–13  = showing promise — advertiser kept budget alive
    # 14–19 = minimum viable — proven past break-even
    # 20–29 = actively scaling
    # 30+   = aggressive scale — copy immediately
    c = w.ad_count
    if c >= 30:
        b["ad_versions"] = 3.0
    elif c >= 20:
        b["ad_versions"] = 2.5
    elif c >= 14:
        b["ad_versions"] = 1.75
    elif c >= 7:
        b["ad_versions"] = 1.0
    elif c >= 4:
        b["ad_versions"] = 0.25
    else:
        b["ad_versions"] = 0.0

    # ── 2. Shopify (2.5 pts) ──────────────────────────────────────────────────
    if w.is_shopify and getattr(w, "shopify_confirmed_via_browser", False):
        b["shopify"] = 2.5      # browser-confirmed (visited store, saw Shopify)
    elif w.is_shopify:
        b["shopify"] = 1.75     # HTTP header / HTML confirmed
    else:
        b["shopify"] = 0.0

    # ── 3. Ad age — how long has the campaign been running (1.5 pts) ──────────
    # Ads running 14+ days have proven ROI — the advertiser kept spending.
    # Fresh ads (< 7 days) could just be a test that will be killed.
    ad_age_score = 0.0
    if w.ad_start_dates:
        try:
            # Use the EARLIEST start date — how long has ANY version been live?
            earliest = min(w.ad_start_dates)
            dt = datetime.strptime(earliest, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days >= 30:
                ad_age_score = 1.5   # 30+ days: proven product
            elif age_days >= 14:
                ad_age_score = 1.25  # 14–29 days: solid test
            elif age_days >= 7:
                ad_age_score = 0.75  # 7–13 days: early but promising
            elif age_days >= 3:
                ad_age_score = 0.25  # < 7 days: very fresh, wait and see
        except (ValueError, TypeError):
            ad_age_score = 0.5   # date parse failed — neutral
    b["ad_age"] = ad_age_score

    # ── 3b. Ad expansion / momentum (1.0 pt) ─────────────────────────────────
    # Measures whether the advertiser is actively scaling vs just launched.
    #
    # Modern broad/ASC/DCT note: advertisers often launch ALL creatives on the
    # same day (single batch), then scale via budget bumps — NOT by adding new
    # creatives.  So a single-launch-date scenario with high collation_count is
    # normal and should NOT be penalised.
    #
    # Two scoring paths:
    #   Multi-date spread (advertiser adding new creatives over time):
    #     spread >= 14d AND newest <= 3d ago   → actively expanding  (1.0)
    #     spread >= 7d  AND newest <= 7d ago   → growing             (0.6)
    #     spread >= 3d  AND newest <= 14d ago  → some expansion      (0.3)
    #   Single-date batch launch (broad/ASC/DCT pattern):
    #     all same launch date AND launched <= 7d ago → fresh test    (0.3)
    #     all same launch date AND launched 7–30d ago → stable run   (0.5)
    #     (the ad_age criterion already rewards long-running campaigns)
    ad_expansion_score = 0.0
    if w.ad_start_dates:
        try:
            dates = sorted(set(w.ad_start_dates))
            newest_dt = datetime.strptime(dates[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_since_newest = (datetime.now(timezone.utc) - newest_dt).days

            if len(dates) >= 2:
                oldest_dt    = datetime.strptime(dates[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                spread_days  = (newest_dt - oldest_dt).days
                if spread_days >= 14 and days_since_newest <= 3:
                    ad_expansion_score = 1.0
                elif spread_days >= 7 and days_since_newest <= 7:
                    ad_expansion_score = 0.6
                elif spread_days >= 3 and days_since_newest <= 14:
                    ad_expansion_score = 0.3
            else:
                # Single batch launch — broad/ASC/DCT pattern
                # Score by how recently the campaign is still running
                if days_since_newest <= 7:
                    ad_expansion_score = 0.3   # fresh test, too early to confirm
                elif days_since_newest <= 30:
                    ad_expansion_score = 0.5   # campaign survived past testing window
                # >30 days since last (only) launch date → ad_age criterion handles it
        except (ValueError, TypeError):
            ad_expansion_score = 0.0
    b["ad_expansion"] = ad_expansion_score

    # ── 4. Video ads (1.0 pt) ────────────────────────────────────────────────
    b["video_ads"] = 1.0 if w.is_video else 0.0

    # ── 5. Followers (1.0 pt) ────────────────────────────────────────────────
    # Sweet spot: 50–500. Under 10k but above 0 = real but small brand.
    fc = w.page_followers
    if 50 <= fc <= 500:
        b["followers"] = 1.0        # sweet spot — early-stage scaling
    elif 10 <= fc < 50:
        b["followers"] = 0.75       # very new, still good
    elif 500 < fc <= 2000:
        b["followers"] = 0.5        # a bit more established, still fine
    elif fc == 0:
        b["followers"] = 0.4        # unknown — slight benefit of the doubt
    else:
        b["followers"] = 0.0        # outside target range

    # ── 6. Cross-platform (0.5 pt) ───────────────────────────────────────────
    platforms = {p.lower() for p in (w.publisher_platforms or [])}
    b["cross_platform"] = 0.5 if len(platforms - {"facebook"}) > 0 else 0.0

    # ── 7. Shop Now CTA (0.5 pt) ─────────────────────────────────────────────
    b["shop_now_cta"] = 0.5 if w.has_shop_now else 0.0

    total = sum(b.values())

    # ── 8. Price viability (from detected_price in ad copy) ──────────────────────
    # Andrew's rule: need $30-45 minimum sell price on Facebook to cover ad costs.
    # < $20: product likely won't be profitable with Facebook advertising costs.
    # $20-29: borderline / risky for FB (TikTok Shop can make it work, FB can't).
    # $25-120: sweet spot — clear margin after 2-3x AliExpress markup + ad spend.
    # No price detected: neutral (benefit of the doubt).
    detected_price = getattr(w, "detected_price", 0) or 0
    if detected_price > 0:
        if 25 <= detected_price <= 120:
            b["price_viability"] = 0.25
        elif detected_price < 20:
            b["price_viability"] = -0.5   # too cheap for FB ad economics
        elif 20 <= detected_price < 25:
            b["price_viability"] = -0.25  # borderline
        else:
            b["price_viability"] = 0.0   # high-ticket, neutral
        total += b["price_viability"]

    # ── 9. Page age bonus/penalty (using page_age_days from oldest known ad) ─────
    # < 30d  → very new, rare find (+0.5)
    # < 90d  → relatively new (+0.25)
    # 90–180d → market is filling up, neutral (0)
    # > 180d  → market is likely mature and crowded; copycatters should skip (-0.5)
    # (Validated from Andrew's framework: "recent AND scaling" — old stores have
    #  already saturated their slice of the market even if ads are still running.)
    page_age_days = getattr(w, "page_age_days", None)
    if page_age_days is not None:
        if page_age_days < 30:
            b["page_age_bonus"] = 0.5
        elif page_age_days < 90:
            b["page_age_bonus"] = 0.25
        elif page_age_days < 180:
            b["page_age_bonus"] = 0.0
        else:
            b["page_age_bonus"] = -0.5  # too old — market likely saturated
        total += b["page_age_bonus"]

    # ── 9. Ad freshness rate bonus ─────────────────────────────────────────────
    freshness = getattr(w, "freshness_rate", None)
    if freshness is not None:
        if freshness > 0.7:
            b["freshness_bonus"] = 0.5
        elif freshness > 0.4:
            b["freshness_bonus"] = 0.25
        else:
            b["freshness_bonus"] = 0.0
        total += b["freshness_bonus"]

    # ── 10. Competition signal (saturation penalty / demand proof) ────────────
    # Some competition = demand is proven (people ARE buying this product).
    # Too much competition = market may be saturated and hard to enter.
    # 0–2 competitors   → neutral (0.0)  — unproven niche
    # 3–10 competitors  → +0.25          — proof of demand without oversaturation
    # 11–20 competitors → -0.75          — moderately saturated
    # 20+ competitors   → -1.5           — very saturated
    saturation = getattr(w, "saturation_count", None)
    if saturation is not None:
        if saturation > 20:
            b["saturation_penalty"] = -1.5
        elif saturation > 10:
            b["saturation_penalty"] = -0.75
        elif saturation >= 3:
            b["saturation_penalty"] = 0.25   # moderate competition = demand proof
        else:
            b["saturation_penalty"] = 0.0
        total += b["saturation_penalty"]

    total = round(min(max(total, 0.0), 10.0), 2)
    return total, b
