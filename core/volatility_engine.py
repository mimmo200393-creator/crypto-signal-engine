"""
core/volatility_engine.py
Volatility Regime Engine — Sprint 2

Modulo Layer 0 indipendente. Classifica il regime di volatilita'
corrente e traccia la sua evoluzione.

NON dipende dallo Structure Engine. Riceve DataFrame come input.
Produce un VolatilitySnapshot immutabile ad ogni scan.

Modalita': LIVE MODE — osserva, produce snapshot, salva nel DB.
Non modifica il comportamento delle strategie.

Dipendenze: solo pandas, numpy, sqlite3, logging.
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

logger = logging.getLogger("volatility_engine")

# ============================================================
# Versione e configurazione
# ============================================================

SNAPSHOT_VERSION = "1.0.0"

DEFAULT_CONFIG = {
    "atr_avg_period": 20,
    "atr_percentile_window": 100,
    "regime_extreme_threshold": 3.0,
    "regime_high_threshold": 1.5,
    "regime_low_threshold": 0.7,
    "expanding_threshold": 1.1,
    "contracting_threshold": 0.9,
    "expanding_short_period": 5,
    "expanding_long_period": 20,
    "range_24h_bars_m15": 96,
    "min_bars_for_percentile": 50,
}


# ============================================================
# Schema DB
# ============================================================

VOLATILITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS volatility_snapshots (
    snapshot_id          TEXT PRIMARY KEY,
    asset                TEXT NOT NULL,
    timestamp_snapshot   DATETIME NOT NULL,
    snapshot_version     TEXT NOT NULL DEFAULT '1.0.0',
    regime               TEXT,
    atr_m15              REAL,
    atr_h1               REAL,
    atr_h4               REAL,
    atr_ratio_m15        REAL,
    atr_ratio_h1         REAL,
    atr_ratio_h4         REAL,
    atr_percentile_h1    REAL,
    expanding            BOOLEAN DEFAULT 0,
    contracting          BOOLEAN DEFAULT 0,
    range_24h            REAL,
    range_24h_pct        REAL,
    snapshot_json         TEXT
);

CREATE INDEX IF NOT EXISTS idx_vol_asset_ts
    ON volatility_snapshots(asset, timestamp_snapshot);
CREATE INDEX IF NOT EXISTS idx_vol_regime
    ON volatility_snapshots(asset, regime);
"""


def init_volatility_schema(conn: sqlite3.Connection):
    """Crea la tabella volatility_snapshots (idempotente)."""
    conn.executescript(VOLATILITY_SCHEMA_SQL)
    conn.commit()


# ============================================================
# Calcoli interni
# ============================================================

def _get_atr_current(df: pd.DataFrame) -> float:
    """Ritorna l'ATR corrente dal DataFrame."""
    if "atr" not in df.columns or len(df) < 1:
        return 0.0
    return float(df.iloc[-1]["atr"])


def _compute_atr_ratio(df: pd.DataFrame, avg_period: int) -> float:
    """ATR corrente / media degli ultimi avg_period periodi."""
    if "atr" not in df.columns or len(df) < avg_period + 1:
        return 1.0

    current = float(df.iloc[-1]["atr"])
    avg = float(df.iloc[-(avg_period + 1):-1]["atr"].mean())

    if avg <= 0:
        return 1.0

    return round(current / avg, 3)


def _compute_atr_percentile(df: pd.DataFrame, window: int,
                              min_bars: int) -> float:
    """
    Dove si colloca l'ATR corrente nella distribuzione storica.
    Ritorna un valore 0-100.
    """
    if "atr" not in df.columns or len(df) < min_bars:
        return 50.0  # neutrale se dati insufficienti

    n = min(window, len(df))
    atr_values = df.iloc[-n:]["atr"].values
    current = atr_values[-1]

    if len(atr_values) < 2:
        return 50.0

    percentile = (atr_values[:-1] < current).sum() / (len(atr_values) - 1) * 100
    return round(float(percentile), 1)


def _classify_regime(atr_ratio: float, cfg: dict) -> str:
    """Classifica il regime di volatilita' basandosi sull'ATR ratio H1."""
    if atr_ratio >= cfg["regime_extreme_threshold"]:
        return "EXTREME"
    elif atr_ratio >= cfg["regime_high_threshold"]:
        return "HIGH"
    elif atr_ratio <= cfg["regime_low_threshold"]:
        return "LOW"
    else:
        return "NORMAL"


def _compute_expanding_contracting(df: pd.DataFrame, cfg: dict) -> dict:
    """
    Confronta la media ATR corta con la media ATR lunga per
    determinare se la volatilita' sta crescendo o diminuendo.
    """
    result = {
        "expanding": False,
        "contracting": False,
        "stable": True,
    }

    if "atr" not in df.columns:
        return result

    short_period = cfg["expanding_short_period"]
    long_period = cfg["expanding_long_period"]

    if len(df) < long_period + 1:
        return result

    atr_short = float(df.iloc[-short_period:]["atr"].mean())
    atr_long = float(df.iloc[-long_period:]["atr"].mean())

    if atr_long <= 0:
        return result

    if atr_short > atr_long * cfg["expanding_threshold"]:
        result["expanding"] = True
        result["stable"] = False
    elif atr_short < atr_long * cfg["contracting_threshold"]:
        result["contracting"] = True
        result["stable"] = False

    return result


def _compute_range_24h(df_m15: pd.DataFrame, bars: int,
                        current_price: float) -> dict:
    """Range delle ultime 24h (96 candele M15)."""
    result = {
        "range_24h": 0.0,
        "range_24h_pct": 0.0,
    }

    if len(df_m15) < bars:
        return result

    recent = df_m15.iloc[-bars:]
    high_24h = float(recent["high"].max())
    low_24h = float(recent["low"].min())
    range_val = high_24h - low_24h

    result["range_24h"] = round(range_val, 4)
    if current_price > 0:
        result["range_24h_pct"] = round(range_val / current_price * 100, 4)

    return result


# ============================================================
# Salvataggio DB
# ============================================================

def _save_volatility_snapshot(conn: sqlite3.Connection,
                                snapshot: dict) -> str:
    """Salva lo snapshot nel DB (append-only). Ritorna lo snapshot_id."""
    snapshot_id = str(uuid.uuid4())

    conn.execute("""
        INSERT INTO volatility_snapshots (
            snapshot_id, asset, timestamp_snapshot, snapshot_version,
            regime, atr_m15, atr_h1, atr_h4,
            atr_ratio_m15, atr_ratio_h1, atr_ratio_h4,
            atr_percentile_h1,
            expanding, contracting,
            range_24h, range_24h_pct,
            snapshot_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        snapshot_id,
        snapshot["asset"],
        snapshot["timestamp"],
        snapshot.get("snapshot_version", SNAPSHOT_VERSION),
        snapshot["regime"],
        snapshot["atr_m15"],
        snapshot["atr_h1"],
        snapshot["atr_h4"],
        snapshot["atr_ratio_m15"],
        snapshot["atr_ratio_h1"],
        snapshot["atr_ratio_h4"],
        snapshot["atr_percentile_h1"],
        snapshot["expanding"],
        snapshot["contracting"],
        snapshot["range_24h"],
        snapshot["range_24h_pct"],
        json.dumps(snapshot),
    ))
    conn.commit()
    return snapshot_id


# ============================================================
# Entry Point Principale
# ============================================================

def produce_volatility_snapshot(
    asset: str,
    df_m15: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_h4: pd.DataFrame,
    conn: sqlite3.Connection,
    now: datetime = None,
    config: dict = None,
) -> dict:
    """
    Produce il VolatilitySnapshot per un asset.

    1. Calcola ATR e ratio per ogni timeframe
    2. Calcola il percentile ATR su H1
    3. Classifica il regime
    4. Determina expanding/contracting
    5. Calcola il range 24h
    6. Salva nel DB
    7. Ritorna lo snapshot (dict immutabile)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = {}

    cfg = {**DEFAULT_CONFIG, **config}
    now_iso = now.isoformat()

    # ── ATR per timeframe ────────────────────────────────────
    atr_m15 = _get_atr_current(df_m15)
    atr_h1 = _get_atr_current(df_h1)
    atr_h4 = _get_atr_current(df_h4)

    # ── Ratio (corrente / media) ─────────────────────────────
    atr_ratio_m15 = _compute_atr_ratio(df_m15, cfg["atr_avg_period"])
    atr_ratio_h1 = _compute_atr_ratio(df_h1, cfg["atr_avg_period"])
    atr_ratio_h4 = _compute_atr_ratio(df_h4, cfg["atr_avg_period"])

    # ── Percentile H1 ───────────────────────────────────────
    atr_percentile_h1 = _compute_atr_percentile(
        df_h1,
        cfg["atr_percentile_window"],
        cfg["min_bars_for_percentile"],
    )

    # ── Regime (basato su H1) ────────────────────────────────
    regime = _classify_regime(atr_ratio_h1, cfg)

    # ── Expanding / Contracting (basato su H1) ───────────────
    exp_contr = _compute_expanding_contracting(df_h1, cfg)

    # ── Range 24h ────────────────────────────────────────────
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0
    range_data = _compute_range_24h(
        df_m15, cfg["range_24h_bars_m15"], current_price
    )

    # ── Costruisci snapshot ──────────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,

        "regime": regime,

        "atr_m15": round(atr_m15, 4),
        "atr_h1": round(atr_h1, 4),
        "atr_h4": round(atr_h4, 4),

        "atr_ratio_m15": atr_ratio_m15,
        "atr_ratio_h1": atr_ratio_h1,
        "atr_ratio_h4": atr_ratio_h4,

        "atr_percentile_h1": atr_percentile_h1,

        "expanding": exp_contr["expanding"],
        "contracting": exp_contr["contracting"],
        "stable": exp_contr["stable"],

        "range_24h": range_data["range_24h"],
        "range_24h_pct": range_data["range_24h_pct"],

        "config": cfg,
    }

    # ── Salva nel DB ─────────────────────────────────────────
    try:
        _save_volatility_snapshot(conn, snapshot)
    except Exception as e:
        logger.warning("Volatility Engine: errore salvataggio: %s", e)

    # ── Log ──────────────────────────────────────────────────
    logger.info(
        "Volatility [%s]: regime=%s atr_ratio=%.2f percentile=%.0f "
        "expanding=%s contracting=%s range_24h=%.2f%%",
        asset,
        regime,
        atr_ratio_h1,
        atr_percentile_h1,
        exp_contr["expanding"],
        exp_contr["contracting"],
        range_data["range_24h_pct"],
    )

    return snapshot
