"""
Product risk flags — warns when a product falls into categories that are
hard to dropship, legally risky, or have high return/chargeback rates.

Risk severity:
  high   — avoid unless you have compliance infrastructure
  medium — proceed with caution; extra due diligence required
  low    — informational; minor concern
"""

import re
from typing import NamedTuple


class RiskFlag(NamedTuple):
    code: str
    label: str
    severity: str   # "high" | "medium" | "low"
    reason: str


# ── Keyword sets ───────────────────────────────────────────────────────────────

_INGESTIBLE = {
    "supplement", "capsule", "tablet", "pill", "powder", "gummies", "gummy",
    "probiotic", "prebiotic", "protein shake", "collagen powder", "collagen drink",
    "vitamin", "mineral", "extract", "appetite", "detox", "cleanse",
    "fat burn", "fat burner", "weight loss pill", "weight loss supplement",
    "keto supplement", "keto gummies", "metabolism booster", "digestive enzyme",
    "gut health", "nootropic", "energy drink", "energy shot", "pre-workout",
    "electrolyte powder", "protein bar", "greens powder", "mushroom powder",
    "adaptogen", "superfood powder", "super greens", "chewable vitamin",
    "liquid vitamin", "softgel", "sublingual", "ingestible", "edible",
    "eat", "drink mix", "drink powder", "swallow", "serving size",
    "daily dose", "mg per", "mcg per",
}

_CHILD_HEALTH = {
    "baby formula", "infant formula", "breast milk", "breastfeed",
    "nursing supplement", "baby food", "toddler formula", "pediatric supplement",
    "children's vitamin", "kids vitamin", "baby vitamin", "infant vitamin",
    "baby supplement", "teething gel", "newborn care", "infant sleep aid",
    "baby medicine", "children's medicine", "kids health", "child health",
    "baby probiotics", "infant probiotic",
}

_MEDICAL_CLAIM_WORDS = {
    "cure", "cures", "cured", "treat", "treats", "treatment", "therapeutic",
    "clinically proven", "clinically tested", "fda approved", "fda cleared",
    "doctor recommended", "doctor approved", "prescription strength",
    "diagnose", "diagnosis", "symptom relief", "chronic pain relief",
    "inflammation reducer", "anti-inflammatory supplement",
    "blood pressure", "blood sugar", "cholesterol", "diabetes",
    "arthritis relief", "joint pain cure", "cancer", "dementia",
    "alzheimer", "seizure", "anxiety disorder", "depression treatment",
    "mental health treatment", "adhd", "autism",
}

_MEDICAL_CLAIM_PATTERNS = [
    r"\bcure[sd]?\b",
    r"\btreat(?:ment|s|ed)?\s+(?:for|of)\b",
    r"\bFDA[- ](?:approved|cleared|registered)\b",
    r"\bclinically\s+(?:proven|tested|studied)\b",
    r"\bmedically\s+(?:proven|tested|approved)\b",
    r"\bprescription\s+strength\b",
    r"\bno\.?\s*1\s+doctor\b",
    r"\b\d+%\s+(?:of\s+)?(?:doctors?|physicians?)\b",
    r"\bchronic\s+pain\b",
    r"\bblood\s+(?:pressure|sugar|glucose)\b",
    r"\bcholesterol\b",
    r"\bdiabete",
]

_APPAREL_SIGNALS = {
    "shirt", "t-shirt", "tshirt", "dress", "pants", "jeans", "leggings",
    "hoodie", "sweater", "sweatshirt", "jacket", "coat", "shorts", "skirt",
    "blouse", "top", "swimsuit", "bikini", "underwear", "bra", "socks",
    "shoes", "sneakers", "boots", "sandals", "hat", "cap", "beanie",
    "gloves", "scarf", "outfit", "clothing", "apparel", "wear", "fashion",
    "garment", "size xs", "size s", "size m", "size l", "size xl",
    "size xxl", "true to size", "runs small", "runs large", "sizing chart",
}

_PATENT_RISK_PHRASES = {
    "patent pending", "patented design", "registered design", "trademarked",
    "licensed technology", "proprietary formula", "proprietary blend",
    "exclusive license", "utility patent",
}

# Brand names that own wide IP portfolios — adjacent products get flagged
_BIG_IP_BRANDS = {
    "apple", "airpod", "airtag", "magsafe", "iphone", "ipad", "macbook",
    "samsung", "galaxy", "google pixel", "fitbit",
    "lego", "nerf", "barbie", "hot wheels",
    "disney", "marvel", "star wars", "pokemon",
    "popsocket", "fidget cube",
    "yeti", "hydroflask",
}


# ── Detection logic ────────────────────────────────────────────────────────────

def detect_risks(
    page_name: str,
    ad_bodies: list[str],
    store_url: str = "",
    product_title: str = "",
) -> list[RiskFlag]:
    """
    Scan page name + ad copy for product risk signals.
    Returns a list of RiskFlag namedtuples (empty list = no risks detected).
    """
    text = " ".join([
        (page_name or "").lower(),
        " ".join((b or "").lower() for b in (ad_bodies or [])),
        (store_url or "").lower(),
        (product_title or "").lower(),
    ])

    flags: list[RiskFlag] = []

    # ── Ingestible / supplement ───────────────────────────────────────────────
    ingest_hits = [kw for kw in _INGESTIBLE if kw in text]
    if ingest_hits:
        flags.append(RiskFlag(
            code="ingestible",
            label="Ingestible / supplement product",
            severity="high",
            reason=(
                f"Supplement/consumable signals: {', '.join(ingest_hits[:4])}. "
                "Requires FDA compliance, high chargeback rate, complex returns, "
                "Facebook/Meta ad policy restrictions."
            ),
        ))

    # ── Child-health-adjacent ─────────────────────────────────────────────────
    child_hits = [kw for kw in _CHILD_HEALTH if kw in text]
    if child_hits:
        flags.append(RiskFlag(
            code="child_health",
            label="Child-health-adjacent product",
            severity="high",
            reason=(
                f"Child health signals: {', '.join(child_hits[:4])}. "
                "Extreme liability exposure. Shopify/Meta may restrict or ban "
                "accounts selling unvetted child-health products."
            ),
        ))

    # ── Medical / health claims ───────────────────────────────────────────────
    med_word_hits = [w for w in _MEDICAL_CLAIM_WORDS if w in text]
    med_pattern_hits = [p for p in _MEDICAL_CLAIM_PATTERNS if re.search(p, text, re.I)]
    if med_word_hits or med_pattern_hits:
        sample = (med_word_hits[:3] or [p[:30] for p in med_pattern_hits[:2]])
        flags.append(RiskFlag(
            code="medical_claim",
            label="Medical / health claim in ad copy",
            severity="high",
            reason=(
                f"Medical-claim language detected: {', '.join(sample)}. "
                "FTC enforcement risk, Meta ad disapproval, high chargeback from "
                "buyers who expected clinical results."
            ),
        ))

    # ── Apparel / fit risk ────────────────────────────────────────────────────
    apparel_hits = [kw for kw in _APPAREL_SIGNALS if kw in text]
    if len(apparel_hits) >= 2:
        flags.append(RiskFlag(
            code="apparel",
            label="Apparel — high fit/return risk",
            severity="medium",
            reason=(
                f"Apparel signals: {', '.join(apparel_hits[:4])}. "
                "30–40% return rate typical; sizing complaints, slow shipping "
                "from China, and PayPal/Stripe chargeback risk are common."
            ),
        ))

    # ── Patent / IP risk ─────────────────────────────────────────────────────
    ip_phrase_hits = [p for p in _PATENT_RISK_PHRASES if p in text]
    ip_brand_hits  = [b for b in _BIG_IP_BRANDS if b in text]
    if ip_phrase_hits or ip_brand_hits:
        sample = (ip_phrase_hits + ip_brand_hits)[:4]
        flags.append(RiskFlag(
            code="patent_risk",
            label="Potential IP / patent risk",
            severity="medium",
            reason=(
                f"IP risk signals: {', '.join(sample)}. "
                "Verify patent status and trademark clearance before sourcing. "
                "DMCA takedowns and store shutdowns are common in this category."
            ),
        ))

    return flags


def risk_summary(flags: list[RiskFlag]) -> str:
    """One-line risk summary for display."""
    if not flags:
        return "✓ No risk flags"
    high = [f for f in flags if f.severity == "high"]
    med  = [f for f in flags if f.severity == "medium"]
    parts = []
    if high:
        parts.append(f"⛔ {len(high)} HIGH")
    if med:
        parts.append(f"⚠ {len(med)} MEDIUM")
    codes = ", ".join(f.code for f in flags)
    return "  ".join(parts) + f"  ({codes})"
