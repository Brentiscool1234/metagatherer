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
  page_age      +0.5  very new page (< 30 days) / +0.25 relatively new (< 90 days)
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
    # Active ad count — the single strongest dropshipping signal.
    # Based on industry practice: nobody spends on 20+ ads unless profitable.
    # <7    = just testing — very risky, skip
    # 7–13  = early stage, still testing
    # 14–19 = minimum viable — advertiser kept spending past break-even
    # 20–29 = actively scaling with real budget
    # 30+   = proven winner being aggressively scaled, copy immediately
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
    # If the advertiser started with a few ads and kept adding more, that means
    # they're seeing returns and scaling.  Measured by the spread between the
    # oldest and newest start date, plus how recently the last ad was added.
    #
    # spread >= 14 days AND newest <= 3 days ago  →  actively scaling  (1.0)
    # spread >= 7  days AND newest <= 7 days ago  →  growing           (0.6)
    # spread >= 3  days AND newest <= 14 days ago →  some expansion    (0.3)
    # single batch launch or all same date        →  no expansion      (0.0)
    ad_expansion_score = 0.0
    if len(w.ad_start_dates) >= 2:
        try:
            dates = sorted(w.ad_start_dates)
            oldest_dt = datetime.strptime(dates[0],  "%Y-%m-%d").replace(tzinfo=timezone.utc)
            newest_dt = datetime.strptime(dates[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            spread_days      = (newest_dt - oldest_dt).days
            days_since_newest = (datetime.now(timezone.utc) - newest_dt).days
            if spread_days >= 14 and days_since_newest <= 3:
                ad_expansion_score = 1.0
            elif spread_days >= 7 and days_since_newest <= 7:
                ad_expansion_score = 0.6
            elif spread_days >= 3 and days_since_newest <= 14:
                ad_expansion_score = 0.3
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

    # ── 8. Page age bonus (using page_age_days from oldest known ad) ──────────
    page_age_days = getattr(w, "page_age_days", None)
    if page_age_days is not None:
        if page_age_days < 30:
            b["page_age_bonus"] = 0.5   # very new — rare find
        elif page_age_days < 90:
            b["page_age_bonus"] = 0.25  # relatively new
        else:
            b["page_age_bonus"] = 0.0   # established — neutral
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
