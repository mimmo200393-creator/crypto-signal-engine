"""
trade_manager.py
Gestione del ciclo di vita dei trade ACTIVE: verifica TP/SL/EXPIRED,
calcolo MAE/MFE, incremento bars_open.

IMPORTANTE: bars_open viene incrementato SOLO quando si chiude una
nuova candela H1 (non ad ogni ciclo di scan ogni 15 minuti), altrimenti
72 barre H1 corrisponderebbero a 18h invece di 72h. Questo modulo deve
quindi essere chiamato dal signal_engine SOLO nel blocco
"new_h1_candle_closed", passando la candela H1 appena chiusa per
ciascun asset.
"""

import logging
from datetime import datetime, timezone
from storage import db

logger = logging.getLogger("trade_manager")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def update_open_trades_for_asset(conn, asset: str, new_h1_candle: dict, expiry_bars: int):
    """
    Per ogni trade ACTIVE su `asset` (entrambe le direzioni), verifica se
    la nuova candela H1 ha toccato TP o SL, aggiorna MAE/MFE e bars_open,
    e marca lo stato finale (TP/SL/EXPIRED) se applicabile.

    `new_h1_candle`: dict con keys open/high/low/close/timestamp della
    candela H1 appena chiusa.
    """
    active_trades = db.get_active_trades(conn)
    if active_trades.empty:
        return

    asset_trades = active_trades[active_trades["asset"] == asset]

    for _, trade in asset_trades.iterrows():
        trade_id = int(trade["id"])
        direzione = trade["direzione"]
        entry = float(trade["entry"])
        stop_loss = float(trade["stop_loss"])
        take_profit = float(trade["take_profit"])

        high = new_h1_candle["high"]
        low = new_h1_candle["low"]

        bars_open = int(trade["bars_open"]) + 1

        # --- Calcolo MAE / MFE ---
        # MAE (Maximum Adverse Excursion): massima escursione contraria
        # MFE (Maximum Favorable Excursion): massima escursione favorevole
        prev_mae = trade["mae"] if trade["mae"] is not None else 0.0
        prev_mfe = trade["mfe"] if trade["mfe"] is not None else 0.0

        if direzione == "LONG":
            adverse_excursion = entry - low      # quanto e' sceso sotto entry
            favorable_excursion = high - entry   # quanto e' salito sopra entry
        else:  # SHORT
            adverse_excursion = high - entry     # quanto e' salito sopra entry (contrario per short)
            favorable_excursion = entry - low    # quanto e' sceso sotto entry (favorevole per short)

        new_mae = max(prev_mae, adverse_excursion, 0.0)
        new_mfe = max(prev_mfe, favorable_excursion, 0.0)

        # --- Verifica TP / SL ---
        hit_tp = False
        hit_sl = False

        if direzione == "LONG":
            hit_tp = high >= take_profit
            hit_sl = low <= stop_loss
        else:  # SHORT
            hit_tp = low <= take_profit
            hit_sl = high >= stop_loss

        # Se entrambi TP e SL sono toccati nella stessa candela (gap/volatilita'
        # estrema), si assume conservativamente che lo SL sia stato colpito
        # per primo (worst-case, criterio di gestione del rischio prudente).
        final_status = None
        if hit_sl:
            final_status = "SL"
        elif hit_tp:
            final_status = "TP"
        elif bars_open >= expiry_bars:
            final_status = "EXPIRED"

        if final_status:
            db.update_trade_status(
                conn, trade_id, stato=final_status,
                timestamp_closed=_now_iso(),
                mae=new_mae, mfe=new_mfe, bars_open=bars_open,
            )
            logger.info(
                "Trade #%d (%s %s) chiuso come %s | entry=%.6f sl=%.6f tp=%.6f | bars_open=%d",
                trade_id, asset, direzione, final_status, entry, stop_loss, take_profit, bars_open
            )
        else:
            db.update_trade_status(
                conn, trade_id, stato="ACTIVE",
                mae=new_mae, mfe=new_mfe, bars_open=bars_open,
            )


def check_cooldown(conn, asset: str, direzione: str, setup_name: str,
                    current_timestamp_ms: int, cooldown_hours: int) -> bool:
    """
    Ritorna True se l'asset+direzione+setup e' attualmente in cooldown
    (cioe' un segnale identico va SCARTATO), False se si puo' procedere.

    Un segnale e' "identico" se stesso asset, stessa direzione, stesso setup.
    Cooldown calcolato dal timestamp_setup dell'ultimo trade registrato
    (indipendentemente dal suo stato finale).
    """
    last_trade = db.get_last_trade(conn, asset, direzione, setup_name)
    if last_trade is None:
        return False

    last_ts = last_trade["timestamp_setup"]
    if last_ts is None:
        return False

    # timestamp_setup e' salvato come timestamp ms (int) convertito a stringa
    # nel DB -> normalizziamo a int per il confronto.
    try:
        last_ts_ms = int(last_ts)
    except (ValueError, TypeError):
        return False

    elapsed_hours = (current_timestamp_ms - last_ts_ms) / (1000 * 60 * 60)
    return elapsed_hours < cooldown_hours
