"""
core/trade_manager_v2.py
Gestione del ciclo di vita dei segnali V2 nella tabella `signals`.

Per ogni segnale OPEN, ad ogni nuova candela H1 chiusa:
    - Verifica se TP o SL sono stati toccati
    - Calcola MAE (Maximum Adverse Excursion)
    - Calcola MFE (Maximum Favorable Excursion)
    - Incrementa bars_open
    - Marca TP / SL / EXPIRED (dopo TRADE_EXPIRY_BARS candele H1)

In caso TP e SL vengano toccati nella stessa candela,
si assume SL colpito per primo (worst-case prudente).
"""

import logging
from datetime import datetime, timezone

from storage import db

logger = logging.getLogger("trade_manager_v2")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_open_signals_for_asset(conn, asset: str, new_h1_candle: dict,
                                   expiry_bars: int):
    """
    Per ogni segnale OPEN su `asset` (tutte le strategie, entrambe le direzioni),
    verifica se la nuova candela H1 ha toccato TP o SL, aggiorna MAE/MFE
    e bars_open, e marca lo stato finale se applicabile.
    """
    open_signals = db.get_open_signals(conn)
    if open_signals.empty:
        return

    asset_signals = open_signals[open_signals["asset"] == asset]
    if asset_signals.empty:
        return

    high = float(new_h1_candle["high"])
    low  = float(new_h1_candle["low"])

    for _, sig in asset_signals.iterrows():
        signal_id   = str(sig["signal_id"])
        direction   = str(sig["direction"])
        entry       = float(sig["entry"])
        stop_loss   = float(sig["stop_loss"])
        take_profit = float(sig["take_profit"])
        bars_open   = int(sig["bars_open"]) + 1

        # MAE / MFE
        prev_mae = float(sig["mae"]) if sig["mae"] is not None else 0.0
        prev_mfe = float(sig["mfe"]) if sig["mfe"] is not None else 0.0

        if direction == "LONG":
            adverse_excursion   = max(entry - low,  0.0)
            favorable_excursion = max(high - entry, 0.0)
        else:
            adverse_excursion   = max(high - entry, 0.0)
            favorable_excursion = max(entry - low,  0.0)

        new_mae = max(prev_mae, adverse_excursion)
        new_mfe = max(prev_mfe, favorable_excursion)

        # TP / SL check
        if direction == "LONG":
            hit_sl = low  <= stop_loss
            hit_tp = high >= take_profit
        else:
            hit_sl = high >= stop_loss
            hit_tp = low  <= take_profit

        # Minuti dal segnale
        candle_ts = int(new_h1_candle["timestamp"])
        try:
            signal_ts_ms = int(
                datetime.fromisoformat(sig["timestamp_setup"]).timestamp() * 1000
            )
            minutes_elapsed = int((candle_ts - signal_ts_ms) / 60000)
        except Exception:
            minutes_elapsed = None

        # Stato finale (SL ha priorità su TP)
        final_status = None
        time_to_tp = None
        time_to_sl = None

        if hit_sl:
            final_status = "SL"
            time_to_sl = minutes_elapsed
        elif hit_tp:
            final_status = "TP"
            time_to_tp = minutes_elapsed
        elif bars_open >= expiry_bars:
            final_status = "EXPIRED"

        if final_status:
            db.update_signal_status(
                conn, signal_id,
                trade_status=final_status,
                timestamp_closed=_now_iso(),
                mae=new_mae,
                mfe=new_mfe,
                bars_open=bars_open,
                time_to_tp=time_to_tp,
                time_to_sl=time_to_sl,
            )
            logger.info(
                "Signal %s (%s %s %s) -> %s | bars=%d mae=%.6f mfe=%.6f",
                signal_id[:8], sig["strategy_name"], asset, direction,
                final_status, bars_open, new_mae, new_mfe
            )
        else:
            db.update_signal_status(
                conn, signal_id,
                trade_status="OPEN",
                mae=new_mae,
                mfe=new_mfe,
                bars_open=bars_open,
            )
