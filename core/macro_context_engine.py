"""
core/macro_context_engine.py
Macro Context Engine — Sprint 10

Layer 2: contesto macroeconomico per il MIE.
Calendario economico, news sentiment crypto, cross-market trends.

Usa cache di 4h per evitare chiamate API ad ogni scan (15min).
Se la cache e' fresca, ritorna i dati cached senza chiamata.

Modalita': LIVE MODE.
Dipendenze: pandas, sqlite3, logging, requests (per FMP API).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("macro_context_engine")

SNAPSHOT_VERSION = "1.0.0"

DEFAULT_CONFIG = {
    "cache_hours": 4,
    "news_lookback_hours": 4,
    "news_max_items": 10,
    "blackout_window_minutes": 30,
}

MACRO_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS macro_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    is_blackout         BOOLEAN DEFAULT 0,
    next_event_type     TEXT,
    news_sentiment      TEXT,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_macro_asset_ts
    ON macro_snapshots(asset, timestamp_snapshot);

CREATE TABLE IF NOT EXISTS macro_cache (
    cache_key           TEXT PRIMARY KEY,
    data_json           TEXT NOT NULL,
    cached_at           DATETIME NOT NULL
);
"""


def init_macro_schema(conn: sqlite3.Connection):
    conn.executescript(MACRO_SCHEMA_SQL)
    conn.commit()


# ============================================================
# Cache
# ============================================================

def _get_cached(conn: sqlite3.Connection, key: str,
                max_age_hours: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT data_json, cached_at FROM macro_cache WHERE cache_key = ?",
        (key,)
    ).fetchone()

    if row is None:
        return None

    cached_at = datetime.fromisoformat(row[1])
    if datetime.now(timezone.utc) - cached_at > timedelta(hours=max_age_hours):
        return None

    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


def _set_cache(conn: sqlite3.Connection, key: str, data: dict):
    conn.execute("""
        INSERT OR REPLACE INTO macro_cache (cache_key, data_json, cached_at)
        VALUES (?, ?, ?)
    """, (key, json.dumps(data, default=str),
          datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ============================================================
# Macro Events (from macro_events.yaml or API)
# ============================================================

def _get_next_event(macro_provider, now: datetime,
                     blackout_minutes: int) -> dict:
    """Cerca il prossimo evento macro dalla configurazione locale."""
    result = {
        "type": None,
        "minutes_until": None,
        "impact": None,
    }

    if macro_provider is None:
        return result

    try:
        event = macro_provider.get_active_event(now, blackout_minutes)
        if event:
            result["type"] = event.get("type")
            result["impact"] = event.get("impact", "HIGH")
            result["minutes_until"] = 0
    except Exception:
        pass

    return result


# ============================================================
# News Sentiment (from FMP or cached)
# ============================================================

def _fetch_news_sentiment(conn: sqlite3.Connection,
                           cfg: dict) -> dict:
    """
    Tenta di leggere il sentiment news dalla cache.
    Se la cache e' scaduta, ritorna valori neutri.
    La chiamata API reale a FMP avverra' dal runner se disponibile.
    """
    result = {
        "recent_news_count": 0,
        "sentiment": "NEUTRAL",
        "positive_count": 0,
        "negative_count": 0,
        "neutral_count": 0,
    }

    cached = _get_cached(conn, "news_sentiment", cfg.get("cache_hours", 4))
    if cached:
        return cached

    return result


def update_news_cache(conn: sqlite3.Connection, news_data: list):
    """
    Chiamata dal runner dopo aver ottenuto le news da FMP.
    Calcola il sentiment e salva in cache.
    """
    positive = 0
    negative = 0
    neutral = 0

    for item in news_data:
        title = (item.get("title") or "").lower()
        # Sentiment basico basato su parole chiave
        pos_words = ["surge", "rally", "bull", "gain", "rise", "up", "high", "record", "profit"]
        neg_words = ["crash", "drop", "bear", "fall", "down", "low", "loss", "fear", "sell"]

        if any(w in title for w in pos_words):
            positive += 1
        elif any(w in title for w in neg_words):
            negative += 1
        else:
            neutral += 1

    total = positive + negative + neutral
    if total == 0:
        sentiment = "NEUTRAL"
    elif positive > negative * 1.5:
        sentiment = "POSITIVE"
    elif negative > positive * 1.5:
        sentiment = "NEGATIVE"
    else:
        sentiment = "NEUTRAL"

    data = {
        "recent_news_count": total,
        "sentiment": sentiment,
        "positive_count": positive,
        "negative_count": negative,
        "neutral_count": neutral,
    }

    _set_cache(conn, "news_sentiment", data)
    return data


# ============================================================
# Cross-Market (from FMP or cached)
# ============================================================

def _fetch_cross_market(conn: sqlite3.Connection,
                         cfg: dict) -> dict:
    """Legge i trend cross-market dalla cache."""
    result = {
        "dxy_trend": None,
        "sp500_trend": None,
        "gold_trend": None,
    }

    cached = _get_cached(conn, "cross_market", cfg.get("cache_hours", 4))
    if cached:
        return cached

    return result


def update_cross_market_cache(conn: sqlite3.Connection,
                                dxy_trend: str = None,
                                sp500_trend: str = None,
                                gold_trend: str = None):
    """Chiamata dal runner dopo aver ottenuto dati cross-market da FMP."""
    data = {
        "dxy_trend": dxy_trend,
        "sp500_trend": sp500_trend,
        "gold_trend": gold_trend,
    }
    _set_cache(conn, "cross_market", data)
    return data


# ============================================================
# Entry Point
# ============================================================

def produce_macro_snapshot(
    asset: str,
    conn: sqlite3.Connection,
    macro_provider=None,
    now: datetime = None,
    config: dict = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = {}

    cfg = {**DEFAULT_CONFIG, **config}
    now_iso = now.isoformat()

    # ── Evento macro ─────────────────────────────────────────
    blackout_min = cfg.get("blackout_window_minutes", 30)
    next_event = _get_next_event(macro_provider, now, blackout_min)
    is_blackout = next_event["type"] is not None

    # ── News sentiment ───────────────────────────────────────
    news = _fetch_news_sentiment(conn, cfg)

    # ── Cross-market ─────────────────────────────────────────
    cross = _fetch_cross_market(conn, cfg)

    # ── Snapshot ─────────────────────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,

        "next_event": next_event,
        "is_blackout": is_blackout,

        "news_sentiment": news.get("sentiment", "NEUTRAL"),
        "recent_news_count": news.get("recent_news_count", 0),

        "dxy_trend": cross.get("dxy_trend"),
        "sp500_trend": cross.get("sp500_trend"),
        "gold_trend": cross.get("gold_trend"),
    }

    # ── Salva ────────────────────────────────────────────────
    try:
        conn.execute("""
            INSERT INTO macro_snapshots (
                snapshot_id, asset, timestamp_snapshot,
                is_blackout, next_event_type, news_sentiment, snapshot_json
            ) VALUES (?,?,?,?,?,?,?)
        """, (
            str(uuid.uuid4()), asset, now_iso,
            is_blackout, next_event["type"],
            news.get("sentiment"),
            json.dumps(snapshot, default=str),
        ))
        conn.commit()
    except Exception as e:
        logger.warning("Macro Context [%s]: errore salvataggio: %s", asset, e)

    logger.info(
        "Macro Context [%s]: blackout=%s event=%s news=%s(%d) "
        "dxy=%s sp500=%s gold=%s",
        asset, is_blackout,
        next_event["type"] or "none",
        news.get("sentiment", "?"), news.get("recent_news_count", 0),
        cross.get("dxy_trend", "?"),
        cross.get("sp500_trend", "?"),
        cross.get("gold_trend", "?"),
    )

    return snapshot
