"""
AI-powered keyword expansion using Claude.

Two-step approach:
  1. Generate candidate keywords from ad bodies + page context
  2. Verify/filter — only keep phrases that would actually surface
     dropshipping product ads when searched in Facebook Ads Library

Falls back gracefully if no ANTHROPIC_API_KEY is set.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# Try newest model first, fall back to widely-available Claude 3 Haiku
_MODEL_FALLBACK = [
    "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307",
]


def _sanitize(text: str) -> str:
    """Strip characters that break JSON serialization in API requests."""
    # Remove null bytes, surrogates, and other control chars except newline/tab
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]", "", text)


def _call(client, prompt: str, max_tokens: int = 500) -> str:
    """Call Claude with automatic model fallback. Returns response text."""
    prompt = _sanitize(prompt)
    # Cap prompt at ~12k chars to stay well within token limits
    if len(prompt) > 12000:
        prompt = prompt[:12000] + "\n...[truncated]"

    last_err = None
    for model in _MODEL_FALLBACK:
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text.strip()
        except Exception as e:
            last_err = e
            err_str = str(e)
            # 400 can mean bad model name OR bad request content — try next model
            if any(x in err_str for x in ("400", "invalid_request", "model", "not_found")):
                logger.debug(f"Model {model} failed ({err_str[:120]}), trying next...")
                continue
            # Auth / network errors — no point retrying other models
            logger.warning(f"AI call failed: {err_str[:120]}")
            return ""
    logger.debug(f"All models failed: {str(last_err)[:120]}")
    return ""


def keywords_for_niche(niche: str, count: int = 10) -> list[str]:
    """
    Given a niche description (e.g. "pet products", "home fitness gear"),
    ask Claude for the best FB Ads Library search terms to kick off a scan.

    Returns a list of product-phrase keywords, or [] if AI is unavailable.
    Falls back to a small generic seed list so the scraper still works.
    """
    try:
        import anthropic
    except ImportError:
        logger.debug("anthropic not installed — can't generate niche keywords")
        return []

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set — AI niche keywords unavailable. "
            "Add ANTHROPIC_API_KEY=sk-... to your .env file to enable this."
        )
        return []

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are a dropshipping product researcher. A user wants to find winning products in this niche:

NICHE: "{niche}"

Generate {count} search keyword phrases to type into the Facebook Ads Library search box that will surface active dropshipping product ads in this niche.

Rules:
- Each phrase must describe a SPECIFIC physical product, 2–4 words
- Think like a dropshipper looking for products to test: specific enough to find real product ads
- NO generic words (good, best, cheap, buy, shop, free, fast, sale, discount, shipping)
- NO brand names
- NO single-word terms
- Cover a variety of product types within the niche

Return ONLY a valid JSON array of strings, nothing else.
Example for "pet products": ["cat water fountain", "dog anxiety vest", "automatic pet feeder", "retractable dog leash"]"""

    try:
        text = _call(client, prompt, max_tokens=400)
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if not match:
            logger.debug(f"Niche keyword response had no JSON array: {text[:120]}")
            return []
        keywords = json.loads(match.group())
        result = [
            k.lower().strip() for k in keywords
            if isinstance(k, str) and 3 <= len(k.strip()) <= 80
        ]
        logger.info(f"AI niche keywords for '{niche}': {result}")
        return result[:count]
    except Exception as e:
        logger.warning(f"Niche keyword generation failed: {e}")
        return []


# Known-good dropshipping product phrase patterns — used to seed and
# cross-check the AI's suggestions even without an API key.
_DROPSHIP_SEED_PHRASES = [
    "posture corrector", "compression socks", "led face mask", "teeth whitening",
    "knee brace", "back brace", "wrist brace", "ankle brace",
    "hair growth serum", "hair loss treatment", "scalp massager",
    "neck massager", "back massager", "foot massager", "eye massager",
    "cat water fountain", "dog water fountain", "automatic feeder",
    "pet hair remover", "lint roller", "fabric shaver",
    "kitchen gadget", "vegetable chopper", "garlic press", "peeler set",
    "portable blender", "electric whisk", "egg separator",
    "bluetooth tracker", "phone holder car", "wireless charger pad",
    "ring light selfie", "led strip lights", "galaxy projector",
    "weighted blanket", "cooling blanket", "body pillow",
    "resistance bands set", "ab roller wheel", "pull up bar",
    "jump rope speed", "yoga mat thick",
    "necklace gold", "bracelet set women", "earrings set",
    "sunglasses women polarized", "tote bag canvas",
    "shapewear waist trainer", "sports bra seamless", "leggings high waist",
    "oil diffuser ultrasonic", "humidifier bedroom",
    "electric toothbrush", "water flosser", "tongue scraper",
    "beard trimmer", "hair straightener brush",
    "nail kit gel", "nail lamp uv",
]


def expand_keywords_with_ai(
    ad_bodies: list[str],
    existing: set[str],
    max_new: int = 15,
    page_names: list[str] = None,
) -> list[str]:
    """
    Feed ad copy + page names to Claude to get verified dropshipping search terms.

    Two-step:
      Step 1 — Generate: Claude reads the ad copy and suggests product phrases.
      Step 2 — Verify: Claude filters the list to only phrases confirmed to
                       surface real dropshipping product ads on FB Ads Library.

    Returns [] silently if anthropic isn't installed or no API key is set.
    """
    try:
        import anthropic
    except ImportError:
        logger.debug("anthropic package not installed — skipping AI keyword expansion")
        return []

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("No ANTHROPIC_API_KEY — skipping AI keyword expansion")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    samples = [b.strip() for b in ad_bodies if len(b.strip()) > 30]
    # Take a diverse spread: some from the beginning, some from the end
    if len(samples) > 40:
        samples = samples[:20] + samples[-20:]

    if not samples:
        return []

    page_ctx = ""
    if page_names:
        unique_pages = list(dict.fromkeys(p for p in page_names if p))[:20]
        if unique_pages:
            page_ctx = "\n\nPage/store names running these ads:\n" + \
                       "\n".join(f"- {p}" for p in unique_pages)

    # ── Step 1: Generate candidates ──────────────────────────────────────────
    generate_prompt = f"""You are a dropshipping product researcher. Your job is to find winning products that are being heavily advertised right now.

Below is real ad copy scraped from the Facebook Ads Library:{page_ctx}

AD COPY SAMPLES:
{chr(10).join(f'[{i+1}] {s[:200]}' for i, s in enumerate(samples))}

Already searched (do NOT repeat these): {', '.join(sorted(existing)[:40]) if existing else 'none'}

Task: Generate {max_new + 10} search keyword phrases that, when typed into the Facebook Ads Library search box, would surface MORE dropshipping product ads like these.

Rules for good keywords:
- SPECIFIC product names: "posture corrector belt", "led face mask", "cat water fountain"
- 2-4 words, never a single generic word
- Must describe a PHYSICAL product someone buys online
- Think: what would a dropshipper search to find competition / product ideas?
- Include pain-point phrases if relevant: "knee pain relief brace", "hair thinning serum"
- Do NOT include: brand names, store names, generic adjectives (good/best/great/fast), action words (buy/shop/order/free), shipping terms, discount words

Return ONLY a valid JSON array of strings. No explanation.
Example format: ["posture corrector", "led therapy mask", "knee compression sleeve", "automatic cat feeder"]"""

    try:
        gen_text = _call(client, generate_prompt, max_tokens=600)
        match = re.search(r"\[.*?\]", gen_text, re.DOTALL)
        if not match:
            logger.debug(f"AI generate step: no JSON array in: {gen_text[:120]}")
            return []

        candidates = json.loads(match.group())
        candidates = [
            k.lower().strip() for k in candidates
            if isinstance(k, str) and 3 <= len(k.strip()) <= 80
            and k.lower().strip() not in existing
        ]

        if not candidates:
            return []

        logger.debug(f"AI generate step produced {len(candidates)} candidates: {candidates[:8]}")

        # ── Step 2: Verify — filter to only dropshipping-relevant phrases ────
        verify_prompt = f"""You are vetting search terms for a dropshipping product research tool.

These keyword phrases were generated to search the Facebook Ads Library:
{json.dumps(candidates, indent=2)}

For EACH phrase, decide: if someone types this into Facebook Ads Library search, would they likely see ads for a PHYSICAL dropshipping product (something sold in an online store / Shopify store)?

Reject a phrase if:
- It's too generic and returns lifestyle/brand ads, not product ads (e.g. "feel better", "your life")
- It's an action/CTA word (buy, shop, order, free, save, get)
- It's a shipping/discount term
- It's a single common word
- It wouldn't return product-specific ads

Return ONLY the phrases that PASS as a valid JSON array. Keep the best {max_new} maximum.
Return ONLY the JSON array, nothing else."""

        verify_text = _call(client, verify_prompt, max_tokens=400)
        vmatch = re.search(r"\[.*?\]", verify_text, re.DOTALL)
        if not vmatch:
            logger.debug(f"AI verify step: no JSON array, falling back to raw candidates")
            verified = candidates
        else:
            parsed = json.loads(vmatch.group())
            if not isinstance(parsed, list):
                # Model returned a dict or other type — fall back to candidates
                logger.debug(f"AI verify step: got {type(parsed).__name__} not list, using candidates")
                verified = candidates
            else:
                verified = [
                    k.lower().strip() for k in parsed
                    if isinstance(k, str) and k.lower().strip() not in existing
                ]

        result = verified[:max_new]
        logger.info(
            f"AI keywords: {len(candidates)} generated → {len(verified)} verified → "
            f"using {len(result)}: {result}"
        )
        return result

    except Exception as e:
        logger.debug(f"AI keyword expansion failed: {e}")
        return []


def is_dropshipping_page(page_name: str, ad_bodies: list[str]) -> tuple[bool, str]:
    """
    Use Claude to decide if a page is actually selling a physical dropshipping
    product vs. being a restaurant, SaaS, media company, big brand, etc.

    Returns (is_dropshipping: bool, reason: str).
    Falls back to (True, "ai unavailable") so nothing is wrongly excluded
    when AI isn't configured.
    """
    try:
        import anthropic
    except ImportError:
        return True, "ai unavailable"

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return True, "ai unavailable"

    client = anthropic.Anthropic(api_key=api_key)

    samples = [b.strip() for b in ad_bodies if b.strip()][:8]
    bodies_text = "\n".join(f"- {s[:200]}" for s in samples) if samples else "(no ad body text)"

    prompt = f"""You are classifying Facebook ads to find dropshipping product stores.

Page name: "{page_name}"

Sample ad copy from this page:
{bodies_text}

Is this page a DROPSHIPPING / ECOMMERCE store selling physical products?

A dropshipping store:
- Sells a specific physical product (gadgets, beauty tools, pet items, fitness gear, jewelry, home goods)
- Has generic product-focused ads ("Get 50% off today", "As seen on TV", "Ships worldwide")
- Unknown/small brand name
- Typically uses Shopify

NOT a dropshipping store:
- Restaurant or food chain (Taco Bell, McDonald's)
- SaaS / app / software
- Financial services, insurance, bank
- Large established brand (Nike, Apple, Samsung)
- Media / entertainment
- Service business
- Non-profit / charity

Answer with ONLY: YES or NO, then a comma, then one short reason (max 8 words).
Examples:
YES, posture corrector gadget store
NO, restaurant chain not physical product
YES, pet accessory dropshipping store
NO, software subscription service"""

    try:
        text = _call(client, prompt, max_tokens=30)
        if not text:
            return True, "ai unavailable"
        text = text.strip().upper()
        is_drop = text.startswith("YES")
        # Extract reason after the comma
        reason = text.split(",", 1)[1].strip().lower() if "," in text else ""
        return is_drop, reason
    except Exception as e:
        logger.debug(f"Dropshipping classifier failed: {e}")
        return True, "ai unavailable"
