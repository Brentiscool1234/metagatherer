"""
Dropshipping Expert — real-time AI advisor powered by Claude.

Runs in the GUI process and does two things:
  1. analyze_progress() — called every N keywords; returns a short
     commentary string + a list of new keywords to inject into the queue.
  2. chat() — free-form Q&A for the user, with scan context attached.

Falls back gracefully when ANTHROPIC_API_KEY is absent.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an expert dropshipping product researcher and Facebook Ads Library analyst.

Your knowledge covers:
- What makes a winning dropshipping product: 5–50 active ads, small/new Shopify stores (10–2000 followers), physical products shipped from China or 3PL
- Facebook Ads Library patterns: how to read ad counts, dates, CTAs, creative types
- High-converting product niches: pet accessories, posture/pain relief, beauty gadgets, home organisation, kitchen tools, phone accessories, fitness gear, baby products
- Keyword strategy: specific 2–4 word product phrases outperform broad terms; include pain points ("knee pain brace"), use cases ("dog car seat cover"), and trending suffixes ("led", "electric", "portable", "rechargeable")
- Red flags: food/meal services, software/SaaS, large known brands, restaurants, financial services
- Shopify signal strength: myshopify.com domain > X-Shopify headers > CDN references in HTML

When advising on a live scan:
- Be CONCISE — 1–3 sentences of analysis max
- Suggest concrete, specific product keywords when you see a gap
- Flag when results look weak and explain the likely cause
- Suggest filter adjustments (follower range, min-ads, days) when appropriate
- If you suggest keywords, output them as a JSON array in a ```json block

Example good keywords: "dog anxiety vest", "cat water fountain", "posture corrector belt",
"led face mask", "knee compression sleeve", "electric back massager", "portable blender",
"hair growth serum", "nail lamp uv gel", "kids weighted blanket"
"""


class DropshippingExpert:
    def __init__(self):
        self._client = None
        self._history: list[dict] = []
        self._context: dict = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _client_or_none(self):
        if self._client:
            return self._client
        try:
            import anthropic
        except ImportError:
            return None
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            return None
        self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def _call(self, messages: list[dict], max_tokens: int = 350) -> tuple[str, str]:
        """
        Returns (response_text, error_message).
        On success: (text, "").  On failure: ("", description).
        """
        client = self._client_or_none()
        if not client:
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                return "", "No API key set — paste your key in the AI Expert box and click Save."
            return "", "anthropic package not installed — run: pip install anthropic"

        models = [
            "claude-haiku-4-5-20251001",
            "claude-3-5-haiku-20241022",
            "claude-3-haiku-20240307",
        ]
        last_err = ""
        for model in models:
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=_SYSTEM_PROMPT,
                    messages=messages,
                )
                return resp.content[0].text.strip(), ""
            except Exception as e:
                err = str(e)
                last_err = err
                # Bad model name → try next
                if any(x in err for x in ("400", "model", "not_found", "invalid_request")):
                    logger.debug(f"Model {model} unavailable, trying next: {err[:80]}")
                    continue
                # Auth / rate limit / network → don't retry other models
                logger.debug(f"Expert API call failed ({model}): {err[:200]}")
                return "", err[:200]
        return "", f"All models failed. Last error: {last_err[:200]}"

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """Pull JSON keyword array out of a ```json ... ``` block."""
        m = re.search(r"```json\s*(\[.*?\])\s*```", text, re.DOTALL)
        if not m:
            return []
        try:
            kws = json.loads(m.group(1))
            return [k.lower().strip() for k in kws if isinstance(k, str) and 3 <= len(k) <= 80]
        except Exception:
            return []

    # ── Public API ────────────────────────────────────────────────────────────

    def update_context(self, **kwargs):
        """Store scan parameters so they appear in AI prompts."""
        self._context.update(kwargs)

    def analyze_progress(
        self,
        keywords_searched: list[str],
        products_found: int,
        recent_keyword: str,
        recent_ads_found: int,
        total_ads: int,
        already_queued: set[str] = None,
    ) -> tuple[str, list[str]]:
        """
        Analyse scan progress.  Returns (commentary, new_keywords_to_inject).
        Called automatically every few keywords while a scan is running.
        """
        already = already_queued or set()
        recent_kws = list(keywords_searched)[-12:]

        ctx_str = ""
        if self._context:
            ctx_str = f"\nScan settings: {json.dumps(self._context)}"

        prompt = (
            f"Live scan update:{ctx_str}\n"
            f"- Keywords searched: {', '.join(recent_kws)}\n"
            f"- Total ads collected: {total_ads}\n"
            f"- Qualifying products found so far: {products_found}\n"
            f"- Last keyword: \"{recent_keyword}\" → {recent_ads_found} ads\n"
            f"- Already queued/searched (don't repeat): {', '.join(sorted(already)[:20]) or 'none'}\n\n"
            "In 1–2 sentences: is this scan performing well? "
            "Then suggest 3–5 new product-specific search keywords (NOT already listed above). "
            "Output suggested keywords as a ```json array."
        )

        text, err = self._call([{"role": "user", "content": prompt}], max_tokens=300)
        if not text:
            return err or "", []

        keywords = self._extract_keywords(text)
        commentary = re.sub(r"```json.*?```", "", text, flags=re.DOTALL).strip()
        return commentary, [k for k in keywords if k not in already]

    def chat(self, message: str) -> str:
        """
        Free-form conversation.  Maintains history so follow-up questions work.
        Scan context is automatically appended to each user message.
        """
        # Quick pre-flight: if no key at all, say so clearly
        if not os.getenv("ANTHROPIC_API_KEY"):
            return "No API key set — paste your Anthropic key in the AI Expert box on the left and click Save."

        ctx_str = ""
        if self._context:
            ctx_str = f"[Scan context: {json.dumps(self._context)}]\n\n"

        self._history.append({"role": "user", "content": ctx_str + message})
        if len(self._history) > 20:
            self._history = self._history[-20:]

        response, err = self._call(self._history, max_tokens=450)
        if response:
            self._history.append({"role": "assistant", "content": response})
            return response
        return f"API error: {err}" if err else "No response received."

    @property
    def available(self) -> bool:
        return self._client_or_none() is not None
