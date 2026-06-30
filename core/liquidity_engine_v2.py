"""
core/liquidity_engine_v2.py
Liquidity Engine V2 — Sprint 6

Layer 1: dipende da Structure Engine (Layer 0).
Unifica Money Flow Map, Edge Lab Liquidity Engine, e V41 inline
Liquidity Map in un UNICO motore.

Fix critico: separa structural_score da proximity_score.
Il Priority Score attuale mescola forza strutturale e distanza,
rendendo impossibile distinguere "livello forte ma lontano" da
"livello debole ma vicino".

Produce un LiquiditySnapshot unificato consumato da Reaction Map
e strategie.

Modalita': LIVE MODE.
Dipendenze: pandas, sqlite3, logging. Consuma StructureSnapshot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger("liquidity_engine_v2")

SNAPSHOT_VERSION = "2.0.0"

DEFAULT_CONFIG = {
    "weekly_lookback_days": 7,
    "equal_level_tolerance_pct": 0.001,
    "proximity_pct": 0.01,
    "sweep_penetration_min_pct": 0.0005,
    "sweep_lookback_candles": 20,
    "max_levels": 30,
}

# ============================================================
# Schema DB
# ============================================================

LIQ_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS liquidity_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    total_levels        INTEGER DEFAULT 0,
    active_sweeps       INTEGER DEFAULT 0,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_liq_snap_asset_ts
    ON liquidity_snapshots(asset, timestamp_snapshot);
"""


def init_liquidity_schema(conn: sqlite3.Connection):
    conn.executescript(LIQ_SCHEMA_SQL)
    conn.commit()


# ============================================================
# Level Building
# ============================================================

def _build_levels(df_h4: pd.DataFrame, df_d1: pd.DataFrame,
                   session_data: dict, structure_snapshot: dict,
                   cfg: dict) -> list:
    """
    Costruisce tutti i livelli di liquidita' da fonti multiple.
    Ogni livello ha structural_score e proximity_score SEPARATI.
    """
    levels = []
    h4_pivot_lookback = 3

    # ── Daily / Weekly ───────────────────────────────────────
    weekly_days = cfg.get("weekly_lookback_days", 7)

    if len(df_d1) >= 1:
        recent_d1 = df_d1.iloc[-weekly_days:]
        levels.append(_make_level("Weekly High", float(recent_d1["high"].max()), "high", "D1", "WEEKLY", 0.7))
        levels.append(_make_level("Weekly Low", float(recent_d1["low"].min()), "low", "D1", "WEEKLY", 0.7))

        last_d1 = df_d1.iloc[-1]
        levels.append(_make_level("Daily High", float(last_d1["high"]), "high", "D1", "DAILY", 0.6))
        levels.append(_make_level("Daily Low", float(last_d1["low"]), "low", "D1", "DAILY", 0.6))

        if len(df_d1) >= 2:
            prev_d1 = df_d1.iloc[-2]
            levels.append(_make_level("Daily High (prev)", float(prev_d1["high"]), "high", "D1", "DAILY", 0.5))
            levels.append(_make_level("Daily Low (prev)", float(prev_d1["low"]), "low", "D1", "DAILY", 0.5))

    # ── Session ──────────────────────────────────────────────
    if session_data:
        sh = session_data.get("session_high", 0)
        sl = session_data.get("session_low", 0)
        name = session_data.get("session_name", "Session")
        if sh > 0:
            levels.append(_make_level(f"{name} High", sh, "high", "SESSION", "SESSION", 0.5))
        if sl > 0:
            levels.append(_make_level(f"{name} Low", sl, "low", "SESSION", "SESSION", 0.5))

    # ── H4 Swing ─────────────────────────────────────────────
    struct_h4 = structure_snapshot.get("structure_h4", {})
    for key, kind in [("last_hh", "high"), ("last_hl", "low"),
                       ("last_lh", "high"), ("last_ll", "low")]:
        val = struct_h4.get(key)
        if val is not None:
            label = f"H4 {key.replace('last_', '').upper()}"
            levels.append(_make_level(label, val, kind, "H4", "SWING", 0.65))

    # ── Equal Highs / Equal Lows ─────────────────────────────
    if len(df_h4) >= h4_pivot_lookback * 2 + 1:
        tol = cfg.get("equal_level_tolerance_pct", 0.001)
        h_vals = []
        l_vals = []

        highs_arr = df_h4["high"].values
        lows_arr = df_h4["low"].values
        n = len(df_h4)

        for i in range(h4_pivot_lookback, n - h4_pivot_lookback):
            bh = highs_arr[i - h4_pivot_lookback:i]
            ah = highs_arr[i + 1:i + 1 + h4_pivot_lookback]
            if highs_arr[i] > bh.max() and highs_arr[i] > ah.max():
                h_vals.append(float(highs_arr[i]))

            bl = lows_arr[i - h4_pivot_lookback:i]
            al = lows_arr[i + 1:i + 1 + h4_pivot_lookback]
            if lows_arr[i] < bl.min() and lows_arr[i] < al.min():
                l_vals.append(float(lows_arr[i]))

        for i in range(len(h_vals)):
            for j in range(i + 1, len(h_vals)):
                if h_vals[i] != 0 and abs(h_vals[i] - h_vals[j]) / h_vals[i] <= tol:
                    levels.append(_make_level(
                        "Equal Highs", (h_vals[i] + h_vals[j]) / 2,
                        "high", "H4", "EQUAL", 0.8
                    ))

        for i in range(len(l_vals)):
            for j in range(i + 1, len(l_vals)):
                if l_vals[i] != 0 and abs(l_vals[i] - l_vals[j]) / l_vals[i] <= tol:
                    levels.append(_make_level(
                        "Equal Lows", (l_vals[i] + l_vals[j]) / 2,
                        "low", "H4", "EQUAL", 0.8
                    ))

    return levels


def _make_level(label: str, price: float, kind: str, timeframe: str,
                level_type: str, base_score: float) -> dict:
    return {
        "label": label,
        "price": round(price, 4),
        "kind": kind,
        "timeframe": timeframe,
        "type": level_type,
        "structural_score": round(base_score, 3),
        "proximity_score": 0.0,
        "status": "ACTIVE",
        "historical_touches": 0,
        "swept": False,
        "swept_timestamp": None,
    }


# ============================================================
# Proximity & Sweep
# ============================================================

def _compute_proximity(levels: list, current_price: float, cfg: dict) -> list:
    prox_pct = cfg.get("proximity_pct", 0.01)

    for lv in levels:
        if lv["price"] <= 0 or current_price <= 0:
            continue
        dist = abs(lv["price"] - current_price) / current_price
        lv["distance_pct"] = round(dist, 6)
        lv["proximity_score"] = round(max(0, 1.0 - dist / prox_pct), 3)

    return levels


def _detect_sweeps(levels: list, df_m15: pd.DataFrame, cfg: dict) -> list:
    """Rileva sweep: prezzo penetra un livello e poi inverte."""
    sweep_lookback = cfg.get("sweep_lookback_candles", 20)
    min_pen = cfg.get("sweep_penetration_min_pct", 0.0005)
    active_sweeps = []

    if len(df_m15) < 3:
        return active_sweeps

    last = df_m15.iloc[-1]
    last_high = float(last["high"])
    last_low = float(last["low"])
    last_close = float(last["close"])
    last_open = float(last["open"])

    for lv in levels:
        price = lv["price"]
        if price <= 0:
            continue

        # Sweep high: prezzo va sopra e poi chiude sotto
        if lv["kind"] == "high":
            pen = (last_high - price) / price if price > 0 else 0
            if pen > min_pen and last_close < price and last_close < last_open:
                lv["swept"] = True
                lv["swept_timestamp"] = str(last.get("timestamp", ""))
                active_sweeps.append({
                    "label": lv["label"],
                    "price": price,
                    "direction": "BEARISH",
                    "penetration_pct": round(pen, 6),
                })

        # Sweep low: prezzo va sotto e poi chiude sopra
        if lv["kind"] == "low":
            pen = (price - last_low) / price if price > 0 else 0
            if pen > min_pen and last_close > price and last_close > last_open:
                lv["swept"] = True
                lv["swept_timestamp"] = str(last.get("timestamp", ""))
                active_sweeps.append({
                    "label": lv["label"],
                    "price": price,
                    "direction": "BULLISH",
                    "penetration_pct": round(pen, 6),
                })

    return active_sweeps


# ============================================================
# Entry Point
# ============================================================

def produce_liquidity_snapshot(
    asset: str,
    df_h4: pd.DataFrame,
    df_d1: pd.DataFrame,
    df_m15: pd.DataFrame,
    structure_snapshot: dict,
    conn: sqlite3.Connection,
    session_data: dict = None,
    now: datetime = None,
    config: dict = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = {}

    cfg = {**DEFAULT_CONFIG, **config}
    now_iso = now.isoformat()
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0

    # ── 1. Costruisci livelli ────────────────────────────────
    levels = _build_levels(df_h4, df_d1, session_data or {}, structure_snapshot, cfg)

    # ── 2. Proximity ─────────────────────────────────────────
    levels = _compute_proximity(levels, current_price, cfg)

    # ── 3. Sweep detection ───────────────────────────────────
    active_sweeps = _detect_sweeps(levels, df_m15, cfg)

    # ── 4. Sort & filter ─────────────────────────────────────
    max_levels = cfg.get("max_levels", 30)
    levels = sorted(levels, key=lambda l: l["structural_score"], reverse=True)[:max_levels]

    # Nearest above/below
    above = [lv for lv in levels if lv["price"] > current_price]
    below = [lv for lv in levels if lv["price"] < current_price]

    nearest_above = min(above, key=lambda l: l["price"] - current_price) if above else None
    nearest_below = min(below, key=lambda l: current_price - l["price"]) if below else None

    # Targets by direction
    buy_targets = sorted(
        [lv for lv in levels if lv["kind"] == "high" and lv["price"] > current_price],
        key=lambda l: l["structural_score"], reverse=True
    )
    sell_targets = sorted(
        [lv for lv in levels if lv["kind"] == "low" and lv["price"] < current_price],
        key=lambda l: l["structural_score"], reverse=True
    )

    # ── 5. Snapshot ──────────────────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,
        "levels": levels,
        "nearest_above": nearest_above,
        "nearest_below": nearest_below,
        "buy_targets": buy_targets[:5],
        "sell_targets": sell_targets[:5],
        "active_sweeps": active_sweeps,
        "total_levels": len(levels),
    }

    # ── 6. Salva ─────────────────────────────────────────────
    try:
        conn.execute("""
            INSERT INTO liquidity_snapshots (snapshot_id, asset, timestamp_snapshot,
                total_levels, active_sweeps, snapshot_json)
            VALUES (?,?,?,?,?,?)
        """, (str(uuid.uuid4()), asset, now_iso,
              len(levels), len(active_sweeps),
              json.dumps(snapshot, default=str)))
        conn.commit()
    except Exception as e:
        logger.warning("Liquidity V2 [%s]: errore salvataggio: %s", asset, e)

    # ── Log ──────────────────────────────────────────────────
    logger.info(
        "Liquidity V2 [%s]: levels=%d sweeps=%d above=%s below=%s "
        "buy_targets=%d sell_targets=%d",
        asset, len(levels), len(active_sweeps),
        f"{nearest_above['label']}({nearest_above['price']:.2f})" if nearest_above else "none",
        f"{nearest_below['label']}({nearest_below['price']:.2f})" if nearest_below else "none",
        len(buy_targets), len(sell_targets),
    )

    return snapshot
