"""
Market intelligence — analyzes the competitive landscape for a product,
tells you who's selling it, how crowded the market is, and whether you
can still enter profitably.

Answers:
  • Is this dominated by one big brand or several newer stores?
  • Are competitors old (entrenched) or new (still early)?
  • Is this category too entrenched to enter?
  • Is the product early-stage, scaling, or already mature?
  • Who else is selling the same product (competitor list)?
"""

from datetime import datetime, timezone


# ── Market structure labels ────────────────────────────────────────────────────

STRUCTURE = {
    "single_dominant": "Single dominant brand",
    "multi_new":       "Several new advertisers",
    "mixed":           "Mix of old + new advertisers",
    "entrenched":      "Entrenched — established brands dominate",
    "fragmented":      "Fragmented — many small advertisers",
    "unknown":         "Insufficient data",
}

ENTRY_TIMING = {
    "early":    "🟢 Early — strong entry window",
    "good":     "🟡 Good — viable, move fast",
    "late":     "🟠 Late — possible but crowded",
    "too_late": "🔴 Too late — market locked up",
}

# ── Opportunity tiers ─────────────────────────────────────────────────────────

OPPORTUNITY_TIER = {
    "proven_winner":   "✅ Proven Winner",
    "good_test":       "🟡 Good Test Candidate",
    "risky_winner":    "⚠️  High-Risk Winner",
    "bad_opportunity": "🚫 Bad Opportunity",
    "weak_signal":     "📉 Weak Signal",
}


# ── Market analysis ────────────────────────────────────────────────────────────

def analyze_market(
    page_ads: dict,       # page_id → list[ad_dict]
    page_followers: dict, # page_id → int
    saturation_count: int = 0,
) -> dict:
    """
    Analyze the competitive landscape across all pages in the scan.

    Returns a dict with:
      structure        — one of STRUCTURE keys
      structure_label
      entry_timing     — one of ENTRY_TIMING keys
      entry_timing_label
      new_advertisers  — pages whose oldest ad is < 30 days old
      mid_advertisers  — 30–90 days
      established_advertisers — > 90 days
      big_brands       — pages with > 5 000 followers
      total_competitors
      entrenched       — bool
      avg_age_days
    """
    now = datetime.now(timezone.utc)

    new_adv   = 0   # < 30 days
    mid_adv   = 0   # 30–90 days
    old_adv   = 0   # > 90 days
    big_brands = 0  # followers > 5 000
    ages: list[int] = []

    for page_id, ads in page_ads.items():
        followers = page_followers.get(page_id, 0)
        if followers > 5_000:
            big_brands += 1

        dates = [a.get("_start_date") for a in ads if a.get("_start_date")]
        if not dates:
            continue
        try:
            earliest = min(
                datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                for d in dates
            )
            age = (now - earliest).days
            ages.append(age)
            if age < 30:
                new_adv += 1
            elif age <= 90:
                mid_adv += 1
            else:
                old_adv += 1
        except Exception:
            pass

    total = len(page_ads) or 1
    avg_age = int(sum(ages) / len(ages)) if ages else 0

    # ── Classify structure ────────────────────────────────────────────────────
    if total < 3:
        structure = "unknown"
    elif big_brands >= 2 and old_adv >= 3:
        structure = "entrenched"
    elif new_adv >= total * 0.65:
        structure = "multi_new"
    elif old_adv >= total * 0.55:
        structure = "single_dominant" if big_brands >= 1 else "fragmented"
    elif new_adv > 0 and old_adv > 0:
        structure = "mixed"
    else:
        structure = "fragmented"

    # ── Entry timing ─────────────────────────────────────────────────────────
    if structure == "entrenched":
        entry_timing = "too_late"
    elif structure == "multi_new" and avg_age < 20:
        entry_timing = "early"
    elif avg_age < 35 and big_brands == 0:
        entry_timing = "good"
    elif avg_age < 70 and big_brands <= 1:
        entry_timing = "late"
    else:
        entry_timing = "too_late"

    return {
        "structure":                structure,
        "structure_label":          STRUCTURE.get(structure, structure),
        "entry_timing":             entry_timing,
        "entry_timing_label":       ENTRY_TIMING.get(entry_timing, entry_timing),
        "new_advertisers":          new_adv,
        "mid_advertisers":          mid_adv,
        "established_advertisers":  old_adv,
        "big_brands":               big_brands,
        "total_competitors":        total,
        "entrenched":               structure == "entrenched",
        "avg_age_days":             avg_age,
    }


# ── Competitor finder ─────────────────────────────────────────────────────────

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "for", "to", "in", "of", "with",
    "your", "you", "our", "we", "this", "that", "is", "are", "it",
    "get", "now", "new", "best", "free", "just", "only", "buy", "shop",
    "order", "use", "try", "our", "all", "more", "from", "not", "no",
    "at", "on", "be", "so", "up", "if", "as", "into", "by", "was",
}


def find_competitors(
    winner_page_id: str,
    winner_keywords: set,
    all_page_ads: dict,
    all_page_followers: dict,
) -> list[dict]:
    """
    Find other pages that appear to be selling the same/similar product.

    Matching criteria (either is enough):
      1. Shares ≥ 2 keywords with the winner
      2. High word-level overlap in ad copy bodies (≥ 8 meaningful words)

    Returns up to 10 competitors sorted by relevance (ad scale + similarity).
    """
    winner_ads  = all_page_ads.get(winner_page_id, [])
    winner_text = _ad_wordset(winner_ads[:5])

    results = []
    for page_id, ads in all_page_ads.items():
        if page_id == winner_page_id:
            continue

        # Keyword overlap
        page_keywords = {a.get("_keyword", "") for a in ads if a.get("_keyword")}
        shared_kws = page_keywords & winner_keywords

        # Ad-copy text overlap
        page_text   = _ad_wordset(ads[:5])
        common_words = winner_text & page_text - _STOP_WORDS
        text_score  = len(common_words)

        if len(shared_kws) < 2 and text_score < 8:
            continue

        page_name = next(
            (a.get("page_name", "") for a in ads if a.get("page_name")), page_id
        )
        page_url = next(
            (a.get("page_url", "") for a in ads if a.get("page_url")), ""
        )
        page_id_str = str(page_id)
        total_versions = sum(a.get("_ad_versions", 1) for a in ads)
        followers = all_page_followers.get(page_id, 0)
        dates = sorted(d for d in (a.get("_start_date") for a in ads) if d)
        oldest = dates[0] if dates else ""

        # Infer advertiser age
        age_days = None
        if oldest:
            try:
                dt = datetime.strptime(oldest, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - dt).days
            except Exception:
                pass

        ads_lib_url = (
            f"https://www.facebook.com/ads/library/?active_status=all"
            f"&ad_type=all&country=US&search_type=page&view_all_page_id={page_id_str}"
            if page_id_str.isdigit() else
            f"https://www.facebook.com/ads/library/?active_status=all"
            f"&ad_type=all&country=US&search_type=page&q={page_name}"
        )

        results.append({
            "page_id":         page_id_str,
            "page_name":       page_name,
            "page_url":        page_url,
            "ads_library_url": ads_lib_url,
            "followers":       followers,
            "ad_versions":     total_versions,
            "age_days":        age_days,
            "oldest_ad":       oldest,
            "shared_keywords": sorted(shared_kws),
            "text_similarity": text_score,
        })

    results.sort(key=lambda x: -(x["ad_versions"] + x["text_similarity"] * 2))
    return results[:10]


def _ad_wordset(ads: list[dict]) -> set[str]:
    words = set()
    for a in ads:
        for body in (a.get("ad_creative_bodies") or []):
            words.update(w for w in body.lower().split() if len(w) > 3)
    return words


# ── Opportunity tier classification ───────────────────────────────────────────

def classify_opportunity(
    score: float,
    risk_flags: list,        # list[RiskFlag]
    market: dict,            # from analyze_market()
    market_stage: str = "",
) -> tuple[str, str]:
    """
    Returns (tier_code, tier_label).

    proven_winner   — score ≥ 7, no high risks, early/good entry, not entrenched
    good_test       — score 5–7 with no disqualifiers
    risky_winner    — score ≥ 7 but high-risk product category
    bad_opportunity — market entrenched, or too_late entry, despite good score
    weak_signal     — score < 5
    """
    has_high_risk = any(getattr(f, "severity", "") == "high" for f in (risk_flags or []))
    entrenched    = (market or {}).get("entrenched", False)
    entry         = (market or {}).get("entry_timing", "good")

    if entrenched or entry == "too_late":
        return "bad_opportunity", OPPORTUNITY_TIER["bad_opportunity"]

    if score >= 7.0 and not has_high_risk and entry in ("early", "good"):
        return "proven_winner", OPPORTUNITY_TIER["proven_winner"]

    if score >= 7.0 and has_high_risk:
        return "risky_winner", OPPORTUNITY_TIER["risky_winner"]

    if score >= 5.0:
        return "good_test", OPPORTUNITY_TIER["good_test"]

    return "weak_signal", OPPORTUNITY_TIER["weak_signal"]
