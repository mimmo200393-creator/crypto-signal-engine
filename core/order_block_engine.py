"""
core/order_block_engine.py
Order Block Engine — Sprint 4

Layer 1: dipende da Structure Engine (Layer 0).
Identifica, classifica e traccia Order Block su M15.

Un OB e' l'ultima candela contraria prima di un displacement
che causa un BOS o CHOCH. Un OB senza displacement precedente
NON viene identificato — e' la differenza fondamentale rispetto
a un semplice "cluster di pivot".

Modalita': LIVE MODE — osserva, produce snapshot, salva nel DB.
Non modifica il comportamento delle strategie.

5 criteri di qualita' dal doc 005:
    1. Presenza FVG dopo l'OB
    2. Sweep precedente alla formazione
    3. Ultimo OB del movimento (non il primo)
    4. Mai mitigato (fresco)
    5. Sessione ad alta volatilita' (LONDON/NY)

Dipendenze: pandas, numpy, sqlite3, logging.
Consuma StructureSnapshot — non ricalcola struttura.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("order_block_engine")

# ============================================================
# Configurazione
# ============================================================

SNAPSHOT_VERSION = "1.0.0"

DEFAULT_CONFIG = {
    "ob_lookback": 20,              # candele indietro per cercare OB
    "ob_max_tracked": 20,           # max OB tracciati per asset
    "ob_max_age_bars": 200,         # OB piu' vecchio di 200 candele = scartato
    "disp_body_pct": 0.60,         # corpo minimo per candela impulsiva
    "mitigation_touch_pct": 0.002,  # prezzo entro 0.2% della zona = touch
}


# ============================================================
# Schema DB
# ============================================================

OB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS order_block_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    snapshot_version    TEXT DEFAULT '1.0.0',
    fresh_bullish       INTEGER DEFAULT 0,
    fresh_bearish       INTEGER DEFAULT 0,
    total_tracked       INTEGER DEFAULT 0,
    snapshot_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_ob_asset_ts
    ON order_block_snapshots(asset, timestamp_snapshot);

CREATE TABLE IF NOT EXISTS order_blocks (
    ob_id               TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    direction           TEXT NOT NULL,
    timeframe           TEXT DEFAULT 'M15',
    zone_high           REAL NOT NULL,
    zone_low            REAL NOT NULL,
    formation_ts        DATETIME NOT NULL,
    status              TEXT DEFAULT 'FRESH',
    quality_score       INTEGER DEFAULT 0,
    has_fvg             BOOLEAN DEFAULT 0,
    has_sweep_before    BOOLEAN DEFAULT 0,
    is_last_ob          BOOLEAN DEFAULT 0,
    session_quality     TEXT,
    displacement_atr    REAL DEFAULT 0,
    mitigation_count    INTEGER DEFAULT 0,
    first_mitigation_ts DATETIME,
    invalidation_ts     DATETIME,
    age_bars            INTEGER DEFAULT 0,
    trend_at_formation  TEXT,
    in_discount         BOOLEAN DEFAULT 0,
    in_premium          BOOLEAN DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ob_asset_status
    ON order_blocks(asset, status);
"""


def init_ob_schema(conn: sqlite3.Connection):
    conn.executescript(OB_SCHEMA_SQL)
    conn.commit()


# ============================================================
# OB Identification
# ============================================================

def _find_order_blocks(df_m15: pd.DataFrame, structure_snapshot: dict,
                        session: str, cfg: dict) -> list:
    """
    Cerca Order Block nelle candele recenti.

    Un OB bullish: ultima candela BEARISH prima di un displacement UP.
    Un OB bearish: ultima candela BULLISH prima di un displacement DOWN.

    Richiede che il displacement sia confermato nello snapshot.
    """
    obs = []
    disp = structure_snapshot.get("displacement", {})

    if not disp.get("confirmed", False):
        return obs  # nessun displacement = nessun OB

    disp_dir = disp.get("direction")
    lookback = cfg.get("ob_lookback", 20)

    if len(df_m15) < lookback:
        return obs

    recent = df_m15.iloc[-lookback:]
    disp_candles = disp.get("candle_count", 2)
    disp_atr = disp.get("magnitude_atr", 0)

    # Cerchiamo l'ultima candela contraria PRIMA del displacement
    # Il displacement e' nelle ultime disp_candles candele
    # L'OB e' la candela immediatamente prima

    search_end = len(recent) - disp_candles
    if search_end < 1:
        return obs

    # Cerchiamo andando indietro dalla fine del pre-displacement
    for i in range(search_end - 1, max(search_end - 6, -1), -1):
        if i < 0:
            break

        candle = recent.iloc[i]
        open_ = float(candle["open"])
        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])

        is_bullish_candle = close > open_
        is_bearish_candle = close < open_

        # OB Bullish: displacement UP, cerco ultima candela bearish
        if disp_dir == "BULLISH" and is_bearish_candle:
            ob = _build_ob(candle, "BULLISH", structure_snapshot,
                           session, disp_atr, cfg)
            obs.append(ob)
            break

        # OB Bearish: displacement DOWN, cerco ultima candela bullish
        if disp_dir == "BEARISH" and is_bullish_candle:
            ob = _build_ob(candle, "BEARISH", structure_snapshot,
                           session, disp_atr, cfg)
            obs.append(ob)
            break

    return obs


def _build_ob(candle, direction: str, snapshot: dict,
              session: str, disp_atr: float, cfg: dict) -> dict:
    """Costruisce il dict di un Order Block."""
    high = float(candle["high"])
    low = float(candle["low"])
    ts = str(candle.get("timestamp", ""))

    # Premium / Discount dalla posizione nel range
    pd_info = snapshot.get("premium_discount", {})
    in_discount = pd_info.get("zone") == "DISCOUNT"
    in_premium = pd_info.get("zone") == "PREMIUM"

    # Session quality
    session_quality = session if session in ("LONDON", "NEW_YORK") else "OTHER"

    # Trend al momento della formazione
    trend = snapshot.get("trend_health", {}).get("current_trend", "NEUTRAL")

    # Quality Score (0-5) — 5 criteri dal doc 005
    quality = 0
    # Criterio 1: FVG presente — sarà popolato dal FVG Engine (Sprint 5)
    has_fvg = False  # placeholder fino a Sprint 5
    # Criterio 2: sweep prima — check dagli eventi recenti
    events = snapshot.get("event_history", [])
    has_sweep = any(
        e.get("type") in ("BOS", "CHOCH") and e.get("displacement", False)
        for e in events[-5:]
    )
    if has_sweep:
        quality += 1
    # Criterio 3: ultimo OB del movimento (per ora sempre True — singolo OB)
    is_last = True
    quality += 1
    # Criterio 4: fresco (appena creato)
    quality += 1
    # Criterio 5: sessione London/NY
    if session_quality in ("LONDON", "NEW_YORK"):
        quality += 1
    # Displacement forte (> 2 ATR)
    if disp_atr > 2.0:
        quality += 1

    return {
        "id": str(uuid.uuid4())[:8],
        "direction": direction,
        "timeframe": "M15",
        "zone_high": high,
        "zone_low": low,
        "zone_midpoint": round((high + low) / 2, 4),
        "formation_timestamp": ts,
        "status": "FRESH",
        "quality_score": min(quality, 5),
        "has_fvg": has_fvg,
        "has_sweep_before": has_sweep,
        "is_last_ob_of_move": is_last,
        "session_quality": session_quality,
        "displacement_atr": round(disp_atr, 3),
        "mitigation_count": 0,
        "first_mitigation_ts": None,
        "age_bars": 0,
        "trend_at_formation": trend,
        "in_discount": in_discount,
        "in_premium": in_premium,
    }


# ============================================================
# OB State Management
# ============================================================

def _load_active_obs(conn: sqlite3.Connection, asset: str) -> list:
    """Carica gli OB attivi (FRESH) dal DB."""
    rows = conn.execute(
        "SELECT * FROM order_blocks WHERE asset = ? AND status = 'FRESH' "
        "ORDER BY formation_ts DESC",
        (asset,)
    ).fetchall()

    if not rows:
        return []

    cols = [d[0] for d in conn.execute(
        "SELECT * FROM order_blocks WHERE 1=0"
    ).description]

    return [dict(zip(cols, row)) for row in rows]


def _save_ob(conn: sqlite3.Connection, asset: str, ob: dict):
    """Salva o aggiorna un OB nel DB."""
    conn.execute("""
        INSERT OR REPLACE INTO order_blocks (
            ob_id, asset, direction, timeframe,
            zone_high, zone_low, formation_ts,
            status, quality_score,
            has_fvg, has_sweep_before, is_last_ob,
            session_quality, displacement_atr,
            mitigation_count, first_mitigation_ts, invalidation_ts,
            age_bars, trend_at_formation,
            in_discount, in_premium
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ob.get("id") or ob.get("ob_id"),
        asset,
        ob["direction"],
        ob.get("timeframe", "M15"),
        ob["zone_high"],
        ob["zone_low"],
        ob.get("formation_timestamp") or ob.get("formation_ts"),
        ob["status"],
        ob.get("quality_score", 0),
        ob.get("has_fvg", False),
        ob.get("has_sweep_before", False),
        ob.get("is_last_ob_of_move") or ob.get("is_last_ob", False),
        ob.get("session_quality"),
        ob.get("displacement_atr", 0),
        ob.get("mitigation_count", 0),
        ob.get("first_mitigation_ts"),
        ob.get("invalidation_ts"),
        ob.get("age_bars", 0),
        ob.get("trend_at_formation"),
        ob.get("in_discount", False),
        ob.get("in_premium", False),
    ))
    conn.commit()


def _update_ob_states(active_obs: list, current_price: float,
                       cfg: dict) -> list:
    """
    Aggiorna lo stato degli OB attivi:
    - Se il prezzo entra nella zona OB → MITIGATED (primo touch)
    - Se l'OB e' troppo vecchio → rimuovilo dalla lista attiva
    - Incrementa age_bars per ogni OB
    """
    touch_pct = cfg.get("mitigation_touch_pct", 0.002)
    max_age = cfg.get("ob_max_age_bars", 200)
    updated = []

    for ob in active_obs:
        ob["age_bars"] = ob.get("age_bars", 0) + 1

        if ob["age_bars"] > max_age:
            ob["status"] = "EXPIRED"
            updated.append(ob)
            continue

        zone_high = ob["zone_high"]
        zone_low = ob["zone_low"]

        # Check mitigation: prezzo entra nella zona OB
        in_zone = zone_low <= current_price <= zone_high
        near_zone = False
        if not in_zone and zone_high > 0:
            dist = min(abs(current_price - zone_high), abs(current_price - zone_low))
            near_zone = dist / zone_high <= touch_pct

        if in_zone or near_zone:
            ob["mitigation_count"] = ob.get("mitigation_count", 0) + 1
            if ob["mitigation_count"] == 1:
                ob["first_mitigation_ts"] = datetime.now(timezone.utc).isoformat()
            if ob["mitigation_count"] >= 3:
                ob["status"] = "INVALIDATED"
            else:
                ob["status"] = "MITIGATED"

        updated.append(ob)

    return updated


# ============================================================
# Entry Point
# ============================================================

def produce_ob_snapshot(
    asset: str,
    df_m15: pd.DataFrame,
    structure_snapshot: dict,
    conn: sqlite3.Connection,
    session: str = "ASIA",
    now: datetime = None,
    config: dict = None,
) -> dict:
    """
    Produce l'OrderBlockSnapshot per un asset.

    1. Carica OB attivi dal DB
    2. Cerca nuovi OB (se c'e' displacement)
    3. Aggiorna stato OB esistenti (mitigation, age)
    4. Salva tutto nel DB
    5. Ritorna lo snapshot
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = {}

    cfg = {**DEFAULT_CONFIG, **config}
    now_iso = now.isoformat()
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0

    # ── 1. Carica OB attivi ──────────────────────────────────
    active_obs = _load_active_obs(conn, asset)

    # ── 2. Cerca nuovi OB ────────────────────────────────────
    new_obs = _find_order_blocks(df_m15, structure_snapshot, session, cfg)

    # Deduplicazione: non aggiungere OB nella stessa zona di uno esistente
    for new_ob in new_obs:
        is_duplicate = any(
            abs(new_ob["zone_midpoint"] - existing.get("zone_high", 0)) /
            max(existing.get("zone_high", 1), 1) < 0.003
            for existing in active_obs
        )
        if not is_duplicate:
            active_obs.append(new_ob)
            _save_ob(conn, asset, new_ob)
            logger.info(
                "OB Engine [%s]: NUOVO %s OB @ %.2f-%.2f quality=%d disp=%.1fATR",
                asset, new_ob["direction"],
                new_ob["zone_low"], new_ob["zone_high"],
                new_ob["quality_score"], new_ob["displacement_atr"],
            )

    # ── 3. Aggiorna stato OB esistenti ───────────────────────
    updated_obs = _update_ob_states(active_obs, current_price, cfg)

    for ob in updated_obs:
        _save_ob(conn, asset, ob)

    # ── 4. Filtra per lo snapshot ────────────────────────────
    max_tracked = cfg.get("ob_max_tracked", 20)
    all_obs = [ob for ob in updated_obs if ob["status"] != "EXPIRED"]
    all_obs = all_obs[-max_tracked:]

    fresh_bullish = [ob for ob in all_obs if ob["status"] == "FRESH" and ob["direction"] == "BULLISH"]
    fresh_bearish = [ob for ob in all_obs if ob["status"] == "FRESH" and ob["direction"] == "BEARISH"]

    # Nearest fresh OB
    nearest_bull = None
    nearest_bear = None
    if fresh_bullish and current_price > 0:
        below = [ob for ob in fresh_bullish if ob["zone_high"] < current_price]
        if below:
            nearest_bull = min(below, key=lambda ob: current_price - ob["zone_high"])
    if fresh_bearish and current_price > 0:
        above = [ob for ob in fresh_bearish if ob["zone_low"] > current_price]
        if above:
            nearest_bear = min(above, key=lambda ob: ob["zone_low"] - current_price)

    # ── 5. Costruisci snapshot ───────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,

        "order_blocks": all_obs,

        "fresh_bullish_count": len(fresh_bullish),
        "fresh_bearish_count": len(fresh_bearish),
        "nearest_fresh_bullish": nearest_bull,
        "nearest_fresh_bearish": nearest_bear,
        "total_tracked": len(all_obs),
    }

    # ── 6. Salva snapshot ────────────────────────────────────
    try:
        snapshot_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO order_block_snapshots (
                snapshot_id, asset, timestamp_snapshot, snapshot_version,
                fresh_bullish, fresh_bearish, total_tracked, snapshot_json
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (
            snapshot_id, asset, now_iso, SNAPSHOT_VERSION,
            len(fresh_bullish), len(fresh_bearish),
            len(all_obs), json.dumps(snapshot, default=str),
        ))
        conn.commit()
    except Exception as e:
        logger.warning("OB Engine [%s]: errore salvataggio snapshot: %s", asset, e)

    # ── Log ──────────────────────────────────────────────────
    nb = nearest_bull
    nbe = nearest_bear
    logger.info(
        "OB Engine [%s]: fresh_bull=%d fresh_bear=%d total=%d "
        "nearest_bull=%s nearest_bear=%s",
        asset,
        len(fresh_bullish), len(fresh_bearish), len(all_obs),
        f"{nb['zone_low']:.2f}-{nb['zone_high']:.2f}(q{nb['quality_score']})" if nb else "none",
        f"{nbe['zone_low']:.2f}-{nbe['zone_high']:.2f}(q{nbe['quality_score']})" if nbe else "none",
    )

    return snapshot
