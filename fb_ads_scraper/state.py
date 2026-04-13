"""
Persistent scraper state — saves/loads BFS progress so runs can be resumed.

State file (JSON) stores:
  - searched_keywords: set of already-visited keywords (never re-searched)
  - queue:             remaining BFS queue as [[keyword, depth], ...]
  - page_ads:          ads collected per page
  - page_keywords:     keywords matched per page
  - page_followers:    follower counts per page
  - seen_keys:         unique ad keys already ingested
"""

import json
import logging
import os
from collections import defaultdict

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = "scraper_state.json"


def save_state(path: str, state: dict) -> None:
    """Persist scraper state to disk (atomic write via temp file)."""
    tmp = path + ".tmp"
    try:
        payload = {
            "searched_keywords": sorted(state["searched_keywords"]),
            "queue": [[kw, depth] for kw, depth in state["queue"]],
            "page_ads": {
                pid: ads for pid, ads in state["page_ads"].items()
            },
            "page_keywords": {
                pid: sorted(kws) for pid, kws in state["page_keywords"].items()
            },
            "page_followers": state["page_followers"],
            "seen_keys": sorted(state["seen_keys"]),
            "seen_winner_ids": sorted(state.get("seen_winner_ids", set())),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug(f"State save failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def load_state(path: str) -> dict | None:
    """
    Load saved state from disk.
    Returns None if no file exists or the file is corrupt.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        page_ads = defaultdict(list, {pid: ads for pid, ads in raw.get("page_ads", {}).items()})
        page_keywords = defaultdict(set, {
            pid: set(kws) for pid, kws in raw.get("page_keywords", {}).items()
        })

        return {
            "searched_keywords": set(raw.get("searched_keywords", [])),
            "queue": [(kw, int(depth)) for kw, depth in raw.get("queue", [])],
            "page_ads": page_ads,
            "page_keywords": page_keywords,
            "page_followers": {str(k): int(v) for k, v in raw.get("page_followers", {}).items()},
            "seen_keys": set(raw.get("seen_keys", [])),
            "seen_winner_ids": set(raw.get("seen_winner_ids", [])),
        }
    except Exception as e:
        logger.warning(f"Could not load state from {path}: {e} — starting fresh.")
        return None


def state_summary(state: dict) -> str:
    searched = len(state["searched_keywords"])
    queued = len(state["queue"])
    pages = len(state["page_ads"])
    ads = len(state["seen_keys"])
    return (
        f"{searched} keywords already searched, "
        f"{queued} in queue, "
        f"{pages} pages collected, "
        f"{ads} ads seen"
    )
