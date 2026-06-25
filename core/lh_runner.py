"""
core/lh_runner.py
Liquidity Hunter — Runner

Per ogni asset (BTC_USDT, PAXG_USDT):
    1. Carica candele M15 e MFM
    2. Monitora segnali aperti
    3. Genera segnale LH
    4. Se valido e nessun duplicato → inserisce e notifica
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import v3_db
from core import lh_db
from strategies.liquidity_hunter import generate_lh_signal
from strategies.money_flow_map import build_money_flow_map

logger = logging.getLogger("lh.runner")

LH_ASSETS      = ["BTC_USDT", "PAXG_USDT"]
LH_TIMEFRAMES  = {"H4": "4h", "M15": "15m", "D1": "1D"}


def _run_for_asset(conn, asset: str, config: dict, now: datetime):
    logger.info("LH Runner: inizio ciclo per %s", asset)

    limit  = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    df_h4  = core_db.get_candles_df(conn, asset, LH_TIMEFRAMES["H4"], limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["M15"], limit=limit)
    df_d1  = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["D1"], limit=60)

    if len(df_m15) < 20 or len(df_h4) < 10:
        logger.warning("LH [%s]: dati insufficienti, skip.", asset)
        return

    # Monitora segnali aperti
    try:
        last_m15 = df_m15.iloc[-1]
        updated  = lh_db.monitor_open_lh_signals(
            conn, asset,
            current_high=float(last_m15["high"]),
            current_low=float(last_m15["low"]),
            now_iso=now.isoformat(),
        )
        for upd in updated:
            logger.info(
                "LH Monitor [%s]: %s → outcome=%s bars=%d",
                asset, upd["signal_id"][:8], upd["outcome"], upd["bars_open"],
            )
    except Exception as e:
        logger.error("LH Monitor [%s]: errore: %s", asset, e)

    # Costruisce MFM
    current_price = float(df_m15.iloc[-1]["close"])
    mfm = build_money_flow_map(df_h4, df_d1, current_price)

    # Check segnale aperto
    for direction in ("BUY", "SELL"):
        if lh_db.has_open_lh_signal(conn, asset, direction):
            logger.debug("LH [%s %s]: segnale OPEN già presente, skip.", asset, direction)
            continue

    # Genera segnale
    try:
        result = generate_lh_signal(asset, df_m15, mfm, now)
    except Exception as e:
        logger.error("LH [%s]: errore generazione: %s", asset, e)
        return

    signal = result["signal"]
    diag   = result["diagnostics"]

    if signal is None:
        logger.info("LH [%s]: no signal — %s", asset, diag.get("rejection", "UNKNOWN"))
        return

    direction = signal["direction"]

    # Check duplicati: stesso livello sweepato nelle ultime 4h
    if lh_db.has_recent_lh_signal(
        conn, asset, direction, signal["swept_level_label"], hours=4
    ):
        logger.info(
            "LH [%s %s]: duplicato (livello=%s), skip.",
            asset, direction, signal["swept_level_label"],
        )
        return

    try:
        signal_id = lh_db.insert_lh_signal(conn, signal)
    except Exception as e:
        logger.error("LH [%s]: errore inserimento: %s", asset, e)
        return

    logger.info(
        "LH [%s %s]: SEGNALE entry=%.4f sl=%.4f tp=%.4f rr=%.2f "
        "level=%s sweep=%s trigger=%s quality=%d (%s) (id=%s)",
        asset, direction,
        signal["entry"], signal["stop_loss"], signal["tp"], signal["rr"],
        signal["swept_level_label"], signal["sweep_direction"],
        signal["trigger_type"], signal["quality_score"], signal["quality_label"],
        signal_id,
    )

    _notify(signal, config)


def _notify(signal: dict, config: dict):
    try:
        from notifications import telegram_bot, ntfy_bot

        if signal["quality_label"] == "LOW":
            return

        direction = signal["direction"]
        asset     = signal["asset"]
        emoji     = "🟢" if direction == "BUY" else "🔴"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"

        text = (
            f"{emoji} *LIQUIDITY HUNTER v1.0*\n\n"
            f"*{asset.replace('_',' ')}* — {direction}\n\n"
            f"Score: *{signal['quality_score']}* ({signal['quality_label']})\n\n"
            f"Entry:  `{fp(signal['entry'])}`\n"
            f"SL:     `{fp(signal['stop_loss'])}`\n"
            f"TP:     `{fp(signal['tp'])}` ({signal['rr']:.2f}R)\n\n"
            f"Livello: {signal['swept_level_label']} "
            f"({signal.get('swept_level_priority','?')})\n"
            f"Sweep:   {signal['sweep_direction']}\n"
            f"Trigger: {signal['trigger_type']}\n"
            f"Target:  {signal['tp_label']}"
        )

        bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id    = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = f"LH {asset.replace('_',' ')} {direction} | Q{signal['quality_score']} {signal['quality_label']}"
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*","").replace("`",""))

    except Exception as e:
        logger.warning("LH _notify: %s", e)


def run_lh_scan(config: dict):
    conn = core_db.get_connection(config["DB_PATH"])
    lh_db.init_lh_schema(conn)

    now    = datetime.now(timezone.utc)
    assets = config.get("LH_SCANNER", {}).get("assets", LH_ASSETS)

    logger.info("=== LH Scanner: inizio ciclo (%s) ===", ", ".join(assets))

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, now)
        except Exception as e:
            logger.error("LH [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== LH Scanner: fine ciclo ===")
