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

_SYSTEM_PROMPT = """You are the AI brain embedded inside MetaGatherer — an automated Facebook Ads Library scraper that finds winning dropshipping products.

YOU ARE IN CONTROL OF THE SCAN. You are not an external advisor — you are running inside the tool.

HOW YOU STEER THE SCAN:
- Whenever you output a ```json array of strings, those keywords are IMMEDIATELY injected into the live scan queue and searched automatically. No user action needed.
- Example: if you output ```json ["dog anxiety vest", "cat cooling mat"] ``` those two keywords will be searched in the Facebook Ads Library right now.
- Use this power proactively. If the scan is using weak keywords like "buy now", replace them with specific product terms.
- If the user says "search for X" or "try Y instead" — output a ```json block with those keywords and they will be queued immediately.

THE RABBIT HOLE METHOD (how this tool finds winners):
- Start with a broad keyword → find an interesting ad → extract specific product phrases from that ad's copy
- Those phrases become new search keywords → which find more ads → which yield more keywords
- The tool does this automatically, but YOU should also inject targeted keywords to guide the rabbit hole in productive directions
- Example path: "shop now" → finds felt Christmas tree ad → extract "reduces screen time" → finds kids yoga socks with 48 ads → winner

ACTIVE AD COUNT — THE #1 SIGNAL:
- <7 ads  = just testing, skip it (very risky to copy)
- 7–13    = early testing, still uncertain
- 14+ ads = MINIMUM viable — nobody spends on 14+ ads unless profitable (this is the floor)
- 20+ ads = actively scaling, strong buy signal
- 30+ ads = proven winner being aggressively scaled — copy immediately
- IMPORTANT: look at ACTIVE ads only, not total. A page showing 34 total with most inactive = actually just 5 active = testing phase

AD AGE — THE #2 SIGNAL:
- Running today or yesterday only → just launched, skip, too risky
- 7–13 days → promising early signs
- 14–29 days → solid — they've kept spending past the test phase (= profitable)
- 30+ days → proven ROI, serious product

PRODUCT QUALITY SIGNALS:
- Bundle pricing on store (buy 1/2/3 options) = committed dropshipper, professional setup, often doubles revenue
- Video ads = higher converting, serious advertiser
- Shopify store = dropshipping infrastructure confirmed
- Instagram + Facebook = larger budget / multi-platform scaling
- Page followers 50–2000 = sweet spot (early-stage brand scaling fast)

SATURATION WARNING:
- 5+ different pages all selling the SAME product and all launched within last 2–3 days = probably saturated, skip
- If you see multiple competitors all just starting, find the ORIGINAL (highest ad count + oldest start date) — they validated it
- One strong original seller with 30+ ads + 14+ days > five new copycats all just launched

ROAS & SCALING FRAMEWORK:
- Break-even ROAS = 1 / gross_margin (e.g. 30% margin → break-even at 3.3x ROAS)
- Typical dropshipping gross margins: 20–40%, so break-even ROAS is usually 2.5x–5x
- At 2x break-even ROAS → strong signal to scale (double budget)
- Target markets for best conversion: US, Canada, Australia (in order of priority)
- Campaign type for scaling: Advantage+ Shopping Campaigns (Facebook's AI targeting)

BEST NICHES RIGHT NOW:
- Pet accessories (anxiety vests, water fountains, cooling mats)
- Pain/posture relief (knee sleeves, back massagers, posture correctors)
- Kids/baby products (educational toys, safety gear, weighted blankets)
- Beauty gadgets (LED face masks, hair growth devices, scalp massagers)
- Kitchen/home gadgets (portable blenders, organizers, LED strips)
- Car accessories (phone mounts, seat organizers, dash cams)
- Fitness gear (resistance bands, massage guns, balance boards)

KEYWORD STRATEGY:
- Use specific 2–4 word product phrases ("dog anxiety vest" not "dogs")
- Pain-point framing: "knee pain relief brace", "back pain posture brace"
- Trending modifiers: "electric", "portable", "rechargeable", "led", "wireless", "heated"
- Season-aware: Q4 = gift-focused ("gift for dog lovers", "christmas led projector")
- Never use: "buy now", "free shipping", "shop now" alone — too broad

RED FLAGS TO SKIP:
- Food/meal delivery services
- SaaS/software/apps
- Large known retail brands
- Restaurants, financial services, insurance
- Services that can't ship physically (coaching, courses)

SHOPIFY SIGNALS (strongest first):
- myshopify.com in the store URL = confirmed
- X-Shopify-Stage header = confirmed
- Shopify CDN in page HTML = likely
- Bundle pricing app present = very likely Shopify

WHEN THE USER TALKS TO YOU:
- If the scan finds bad results → inject better keywords in a ```json block immediately
- If they say "try dogs" or "focus on fitness" → translate to specific product keywords, output ```json block
- If they ask a question → answer in 1–3 sentences, then suggest keywords if relevant
- Always be direct and action-oriented. You control the scan — act like it.

EXAMPLE WINNING KEYWORD SETS:
```json
["dog anxiety vest", "cat water fountain", "posture corrector belt", "led face mask", "knee compression sleeve", "electric back massager", "portable blender", "scalp massager electric", "kids yoga socks", "weighted blanket kids", "car phone mount wireless", "hair growth serum"]
```
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
                # Billing / auth errors — no point trying other models
                if any(x in err.lower() for x in ("credit", "billing", "balance", "401", "authentication", "permission")):
                    msg = "No credits — go to console.anthropic.com → Plans & Billing to top up." \
                          if "credit" in err.lower() or "balance" in err.lower() \
                          else f"Auth error: {err[:150]}"
                    return "", msg
                # Bad model name → try next
                if any(x in err for x in ("400", "model", "not_found", "invalid_request")):
                    logger.debug(f"Model {model} unavailable, trying next: {err[:80]}")
                    continue
                # Other error (network etc.)
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
