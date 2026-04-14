"""
SQLite persistence for MetaGatherer scan history and winners.
"""

import sqlite3
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_FILE = "metagatherer.db"

_CREATE_SCANS = """
CREATE TABLE IF NOT EXISTS scans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT    NOT NULL,
    niche          TEXT    NOT NULL DEFAULT '',
    keywords_count INTEGER NOT NULL DEFAULT 0,
    ads_count      INTEGER NOT NULL DEFAULT 0,
    pages_count    INTEGER NOT NULL DEFAULT 0,
    winner_count   INTEGER NOT NULL DEFAULT 0,
    near_miss_count INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_WINNERS = """
CREATE TABLE IF NOT EXISTS winners (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id         INTEGER NOT NULL REFERENCES scans(id),
    found_at        TEXT    NOT NULL,
    page_id         TEXT    NOT NULL DEFAULT '',
    page_name       TEXT    NOT NULL DEFAULT '',
    score           REAL    NOT NULL DEFAULT 0.0,
    ad_count        INTEGER NOT NULL DEFAULT 0,
    page_followers  INTEGER NOT NULL DEFAULT 0,
    is_shopify      INTEGER NOT NULL DEFAULT 0,
    store_url       TEXT    NOT NULL DEFAULT '',
    page_url        TEXT    NOT NULL DEFAULT '',
    keywords        TEXT    NOT NULL DEFAULT '',
    margin_pct      TEXT    NOT NULL DEFAULT '',
    break_even_roas TEXT    NOT NULL DEFAULT '',
    saturation_count INTEGER NOT NULL DEFAULT 0,
    freshness_rate  REAL    NOT NULL DEFAULT 0.0,
    is_winner       INTEGER NOT NULL DEFAULT 0
)
"""


def get_conn(path: str = DB_FILE) -> sqlite3.Connection:
    """Return a connection with tables created if they don't exist."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_SCANS)
    conn.execute(_CREATE_WINNERS)
    conn.commit()
    return conn


def save_scan(
    niche: str,
    keywords_count: int,
    ads_count: int,
    pages_count: int,
    winner_count: int,
    near_miss_count: int,
    path: str = DB_FILE,
) -> int:
    """Insert a scan record and return its auto-generated id."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_conn(path)
    try:
        cur = conn.execute(
            """
            INSERT INTO scans
                (timestamp, niche, keywords_count, ads_count, pages_count,
                 winner_count, near_miss_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, niche or "", keywords_count, ads_count, pages_count,
             winner_count, near_miss_count),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.debug(f"DB save_scan failed: {e}")
        return -1
    finally:
        conn.close()


def save_winners(
    scan_id: int,
    products,
    near_miss_threshold: float,
    path: str = DB_FILE,
) -> None:
    """Persist all WinningProduct objects tied to a scan."""
    if not products:
        return
    conn = get_conn(path)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        for w in products:
            sourcing = getattr(w, "sourcing_data", {}) or {}
            score = getattr(w, "score", 0.0)
            conn.execute(
                """
                INSERT INTO winners
                    (scan_id, found_at, page_id, page_name, score, ad_count,
                     page_followers, is_shopify, store_url, page_url, keywords,
                     margin_pct, break_even_roas, saturation_count, freshness_rate,
                     is_winner)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    ts,
                    getattr(w, "page_id", ""),
                    getattr(w, "page_name", ""),
                    round(score, 3),
                    getattr(w, "ad_count", 0),
                    getattr(w, "page_followers", 0),
                    int(bool(getattr(w, "is_shopify", False))),
                    getattr(w, "store_url", "") or "",
                    getattr(w, "page_url", "") or "",
                    ", ".join(getattr(w, "keywords_matched", [])),
                    str(sourcing.get("margin_pct", "") or ""),
                    str(sourcing.get("break_even_roas", "") or ""),
                    getattr(w, "saturation_count", 0),
                    round(getattr(w, "freshness_rate", 0.0), 4),
                    int(score >= near_miss_threshold),
                ),
            )
        conn.commit()
    except Exception as e:
        logger.debug(f"DB save_winners failed: {e}")
    finally:
        conn.close()


def load_recent_scans(limit: int = 100, path: str = DB_FILE) -> list[dict]:
    """Return the most recent *limit* scans as plain dicts."""
    try:
        conn = get_conn(path)
        rows = conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"DB load_recent_scans failed: {e}")
        return []


def load_winners_for_scan(scan_id: int, path: str = DB_FILE) -> list[dict]:
    """Return all winner rows for a given scan_id."""
    try:
        conn = get_conn(path)
        rows = conn.execute(
            "SELECT * FROM winners WHERE scan_id = ? ORDER BY score DESC",
            (scan_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"DB load_winners_for_scan failed: {e}")
        return []
