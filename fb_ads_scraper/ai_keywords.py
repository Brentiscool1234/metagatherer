"""
AI-powered keyword expansion using Claude.
Falls back gracefully if no ANTHROPIC_API_KEY is set.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def expand_keywords_with_ai(
    ad_bodies: list[str],
    existing: set[str],
    max_new: int = 15,
) -> list[str]:
    """
    Feed a sample of ad copy to Claude and get back specific product
    search terms — much more useful than frequency-based extraction.

    Returns [] silently if anthropic isn't installed or no API key is set.
    """
    try:
        import anthropic
    except ImportError:
        logger.debug("anthropic package not installed — skipping AI keyword expansion")
        return []

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("No ANTHROPIC_API_KEY in environment — skipping AI keyword expansion")
        return []

    # Pick diverse, meaningful samples
    samples = [b.strip() for b in ad_bodies if len(b.strip()) > 40][:30]
    if not samples:
        return []

    prompt = f"""You are a dropshipping product researcher analyzing Facebook ad copy to find winning ecommerce products.

Here are ad descriptions scraped from the Facebook Ads Library:
{chr(10).join(f'- {s[:180]}' for s in samples)}

Based on these ads, generate {max_new} specific search terms to find more winning ecommerce/dropshipping ads.

Rules:
- Be SPECIFIC: "posture corrector belt" not "posture"
- Product types: "led face mask", "cat water fountain", "knee compression sleeve"
- Pain-point phrases: "back pain relief", "hair thinning treatment", "knee pain brace"
- NO generic words: not "good", "better", "free", "now", "best", "great", "style", "right"
- NO single common words
- NO brand names
- Prefer 2-3 word product phrases
- Think like someone searching for dropshipping products to test

Return ONLY a valid JSON array of strings, nothing else.
Example: ["posture corrector", "led therapy mask", "knee brace for pain", "cat fountain automatic"]"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Extract JSON array from response
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if not match:
            logger.debug(f"AI response had no JSON array: {text[:100]}")
            return []
        keywords = json.loads(match.group())
        new_kws = [
            k.lower().strip()
            for k in keywords
            if isinstance(k, str)
            and k.strip()
            and k.lower().strip() not in existing
        ]
        logger.info(f"AI keyword expansion: {len(new_kws)} new terms → {new_kws[:6]}")
        return new_kws[:max_new]
    except Exception as e:
        logger.debug(f"AI keyword expansion failed: {e}")
        return []
