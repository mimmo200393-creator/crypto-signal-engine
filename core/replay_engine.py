"""
core/replay_engine.py
Replay Engine — Sprint 12

Infrastruttura di backtesting. Scarica lo storico candele e fa passare
ogni candela attraverso l'intero stack MIE come se fosse uno scan live.

Non e' un modulo Layer — e' un tool di sviluppo che accelera la
validazione da mesi a ore.

Uso:
    python3 -m core.replay_engine --asset BTC_USDT --days 90

Produce un DB di replay con tutti gli snapshot generati, permettendo
di analizzare le performance storiche dell'intero stack MIE.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger("replay_engine")


DEFAULT_CONFIG = {
    "replay_db_path": "data/replay.db",
    "exchange_base_url": "https://api.crypto.com/exchange/v1",
    "request_delay": 0.5,
    "max_candles_per_call": 300,
    "m15_minutes": 15,
}


# ============================================================
# Data Fetching
# ============================================================

def _fetch_historical_candles(asset: str, timeframe: str,
                                start_ts: int, end_ts: int,
                                cfg: dict) -> pd.DataFrame:
    """
    Scarica candele storiche dall'API Crypto.com.
    Pagina automaticamente per coprire l'intero range.
    """
    import requests

    base_url = cfg.get("exchange_base_url", DEFAULT_CONFIG["exchange_base_url"])
    max_candles = cfg.get("max_candles_per_call", 300)
    delay = cfg.get("request_delay", 0.5)

    all_candles = []
    current_end = end_ts

    while current_end > start_ts:
        url = f"{base_url}/public/get-candlestick"
        params = {
            "instrument_name": asset,
            "timeframe": timeframe,
            "count": max_candles,
            "end_ts": current_end,
        }

        try:
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()

            candles = data.get("result", {}).get("data", [])
            if not candles:
                break

            all_candles.extend(candles)

            # Prossima pagina: prima della candela piu' vecchia
            oldest_ts = min(c.get("t", c.get("timestamp", 0)) for c in candles)
            if oldest_ts <= start_ts or oldest_ts >= current_end:
                break
            current_end = oldest_ts - 1

            time.sleep(delay)

        except Exception as e:
            logger.error("Replay: errore fetch %s %s: %s", asset, timeframe, e)
            break

    if not all_candles:
        return pd.DataFrame()

    # Normalizza in DataFrame
    rows = []
    for c in all_candles:
        rows.append({
            "timestamp": c.get("t", c.get("timestamp", 0)),
            "open": float(c.get("o", c.get("open", 0))),
            "high": float(c.get("h", c.get("high", 0))),
            "low": float(c.get("l", c.get("low", 0))),
            "close": float(c.get("c", c.get("close", 0))),
            "volume": float(c.get("v", c.get("volume", 0))),
        })

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


# ============================================================
# ATR Calculation (standalone, no dependency)
# ============================================================

def _add_atr(df: pd.DataFrame, period: int = 14):
    """Aggiunge ATR al DataFrame."""
    if len(df) < period + 1:
        df["atr"] = 0.0
        return

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    tr = []
    tr.append(high[0] - low[0])
    for i in range(1, len(df)):
        tr.append(max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        ))

    atr_values = [0.0] * period
    atr_values.append(sum(tr[:period + 1]) / (period + 1))

    for i in range(period + 1, len(tr)):
        atr_values.append((atr_values[-1] * (period - 1) + tr[i]) / period)

    df["atr"] = atr_values


# ============================================================
# Replay Loop
# ============================================================

def run_replay(asset: str, days: int, config: dict = None):
    """
    Esegue il replay su N giorni di storico.

    1. Scarica le candele M15, H1, H4 per il periodo
    2. Per ogni candela M15, simula uno scan:
       - Prende le candele fino a quel punto
       - Chiama produce_structure_snapshot
       - Chiama tutti i moduli Layer 1-3
       - Salva lo snapshot nel replay DB
    3. Alla fine, il replay DB contiene tutti gli snapshot
       come se il sistema fosse stato attivo per N giorni
    """
    if config is None:
        config = dict(DEFAULT_CONFIG)

    cfg = {**DEFAULT_CONFIG, **config}

    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp() * 1000)
    start_ts = int((now - timedelta(days=days)).timestamp() * 1000)

    logger.info("Replay [%s]: scaricamento %d giorni di dati...", asset, days)

    # ── Scarica dati ─────────────────────────────────────────
    df_m15_full = _fetch_historical_candles(asset, "15m", start_ts, end_ts, cfg)
    df_h1_full = _fetch_historical_candles(asset, "1h", start_ts, end_ts, cfg)
    df_h4_full = _fetch_historical_candles(asset, "4h", start_ts, end_ts, cfg)

    if len(df_m15_full) < 100:
        logger.error("Replay [%s]: dati insufficienti (m15=%d)", asset, len(df_m15_full))
        return

    # Aggiungi ATR
    _add_atr(df_m15_full)
    _add_atr(df_h1_full)
    _add_atr(df_h4_full)

    logger.info(
        "Replay [%s]: dati scaricati — m15=%d h1=%d h4=%d",
        asset, len(df_m15_full), len(df_h1_full), len(df_h4_full),
    )

    # ── Prepara replay DB ────────────────────────────────────
    replay_db_path = cfg.get("replay_db_path", "data/replay.db")
    Path(replay_db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(replay_db_path)

    # Import e init di tutti i moduli
    from core.structure_db import init_structure_schema
    from core.structure_engine_v2 import produce_structure_snapshot
    from core.volatility_engine import produce_volatility_snapshot, init_volatility_schema

    init_structure_schema(conn)
    init_volatility_schema(conn)

    # Import opzionali (Layer 1-3, se disponibili)
    optional_modules = {}
    try:
        from core.order_block_engine import produce_ob_snapshot, init_ob_schema
        init_ob_schema(conn)
        optional_modules["ob"] = produce_ob_snapshot
    except ImportError:
        pass
    try:
        from core.fvg_engine import produce_fvg_snapshot, init_fvg_schema
        init_fvg_schema(conn)
        optional_modules["fvg"] = produce_fvg_snapshot
    except ImportError:
        pass

    # ── Replay loop ──────────────────────────────────────────
    window = 300  # candele di contesto per ogni scan
    total_scans = len(df_m15_full) - window
    scan_count = 0
    event_count = 0

    logger.info("Replay [%s]: inizio replay di %d scan...", asset, total_scans)

    for i in range(window, len(df_m15_full)):
        # Finestra corrente
        df_m15 = df_m15_full.iloc[i - window:i + 1].copy().reset_index(drop=True)
        ts = int(df_m15.iloc[-1]["timestamp"])
        scan_time = datetime.fromtimestamp(
            ts / 1000 if ts > 1e12 else ts, tz=timezone.utc
        )

        # Trova le candele H1/H4 fino a questo timestamp
        h1_mask = df_h1_full["timestamp"] <= ts
        h4_mask = df_h4_full["timestamp"] <= ts
        df_h1 = df_h1_full[h1_mask].iloc[-window:].copy().reset_index(drop=True)
        df_h4 = df_h4_full[h4_mask].iloc[-window:].copy().reset_index(drop=True)

        if len(df_h4) < 15 or len(df_h1) < 20:
            continue

        atr_m15 = float(df_m15.iloc[-1]["atr"]) if "atr" in df_m15.columns else 0

        # ── Structure Engine ─────────────────────────────────
        try:
            snapshot = produce_structure_snapshot(
                asset=asset,
                df_h4=df_h4,
                df_m15=df_m15,
                conn=conn,
                atr_m15=atr_m15,
                now=scan_time,
            )
            events = snapshot.get("events", [])
            if events:
                event_count += len(events)
        except Exception as e:
            if scan_count % 500 == 0:
                logger.warning("Replay scan %d: structure error: %s", scan_count, e)

        # ── Volatility Engine ────────────────────────────────
        try:
            produce_volatility_snapshot(
                asset=asset,
                df_m15=df_m15, df_h1=df_h1, df_h4=df_h4,
                conn=conn, now=scan_time,
            )
        except Exception:
            pass

        # ── Optional Layer 1 ─────────────────────────────────
        if "ob" in optional_modules and snapshot:
            try:
                optional_modules["ob"](asset, df_m15, snapshot, conn, now=scan_time)
            except Exception:
                pass
        if "fvg" in optional_modules and snapshot:
            try:
                optional_modules["fvg"](asset, df_m15, snapshot, conn,
                                         atr_m15=atr_m15, now=scan_time)
            except Exception:
                pass

        scan_count += 1

        if scan_count % 500 == 0:
            logger.info(
                "Replay [%s]: %d/%d scan (%.0f%%) — %d eventi rilevati",
                asset, scan_count, total_scans,
                scan_count / total_scans * 100, event_count,
            )

    conn.close()

    logger.info(
        "Replay [%s]: COMPLETATO — %d scan, %d eventi rilevati. "
        "DB salvato in %s",
        asset, scan_count, event_count, replay_db_path,
    )


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="MIE Replay Engine")
    parser.add_argument("--asset", default="BTC_USDT", help="Asset da replayare")
    parser.add_argument("--days", type=int, default=30, help="Giorni di storico")
    parser.add_argument("--db", default="data/replay.db", help="Path del replay DB")
    args = parser.parse_args()

    run_replay(args.asset, args.days, {"replay_db_path": args.db})
