"""
core/edge_lab_runner.py
Edge Lab — Runner (Step 10)

Orchestratore chiamato dal workflow GitHub Actions.
Per ogni asset (BTC_USDT, PAXG_USDT):
    1. Carica candele H4/H1 da candles_cache, M15/D1 da v3_candles_cache
    2. Calcola indicatori (EMA/ATR)
    3. Monitora segnali aperti (TP/SL/EXPIRED)
    4. Costruisce Market Context (Step 8) → salva snapshot
    5. Valuta OTE-SC (Step 9) per BUY e SELL
    6. Se segnale valido e nessun duplicato → inserisce e notifica
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import indicators, macro
from core import v3_db
from core import edge_lab_db
from strategies.edge_lab.market_context_engine import (
    build_market_context,
    serialize_for_db,
)
from strategies.edge_lab.ote_sc import generate_ote_sc_signal

logger = logging.getLogger("edge_lab.runner")

EDGE_LAB_ASSETS     = ["BTC_USDT", "PAXG_USDT"]
EDGE_LAB_TIMEFRAMES = {"H4": "4h", "H1": "1h", "M15": "15m", "D1": "1D"}


def _prepare_dataframes(conn, asset: str, config: dict):
    limit       = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    ema_periods = config.get("EMA_PERIODS", [21, 50, 100, 200])
    atr_period  = config.get("ATR_PERIOD", 14)

    df_h4  = core_db.get_candles_df(conn, asset, EDGE_LAB_TIMEFRAMES["H4"],  limit=limit)
    df_h1  = core_db.get_candles_df(conn, asset, EDGE_LAB_TIMEFRAMES["H1"],  limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, EDGE_LAB_TIMEFRAMES["M15"], limit=limit)
    df_d1  = v3_db.get_v3_candles_df(conn, asset, EDGE_LAB_TIMEFRAMES["D1"],  limit=60)

    for df in (df_h4, df_h1, df_m15):
        if len(df) > atr_period:
            indicators.add_atr(df, atr_period)

    if len(df_h4) > max(ema_periods):
        indicators.add_emas(df_h4, ema_periods)
    if len(df_h1) > max(ema_periods):
        indicators.add_emas(df_h1, ema_periods)

    return df_h4, df_h1, df_m15, df_d1


def _run_for_asset(conn, asset: str, config: dict, macro_provider, now: datetime):
    logger.info("Edge Lab: inizio ciclo per %s", asset)

    df_h4, df_h1, df_m15, df_d1 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 20 or len(df_m15) < 25:
        logger.warning(
            "Edge Lab [%s]: dati insufficienti (h4=%d h1=%d m15=%d), skip.",
            asset, len(df_h4), len(df_h1), len(df_m15),
        )
        return

    # ── Monitoraggio segnali aperti ──────────────────────────
    try:
        last_m15 = df_m15.iloc[-1]
        updated  = edge_lab_db.monitor_open_el_signals(
            conn, asset,
            current_high=float(last_m15["high"]),
            current_low=float(last_m15["low"]),
            now_iso=now.isoformat(),
        )
        for upd in updated:
            logger.info(
                "Edge Lab Monitor [%s]: %s → outcome=%s bars=%d mae=%.4f mfe=%.4f",
                asset, upd["signal_id"][:8], upd["outcome"],
                upd["bars_open"], upd["mae"], upd["mfe"],
            )
    except Exception as e:
        logger.error("Edge Lab Monitor [%s]: errore: %s", asset, e)

    # ── Market Context Engine ─────────────────────────────────
    try:
        market_ctx = build_market_context(
            asset=asset,
            df_h4=df_h4, df_h1=df_h1, df_m15=df_m15, df_d1=df_d1,
            now=now,
            macro_provider=macro_provider,
            config=config,
        )
    except Exception as e:
        logger.error("Edge Lab [%s]: errore Market Context: %s", asset, e)
        return

    # Salva snapshot
    try:
        edge_lab_db.insert_market_context(conn, serialize_for_db(market_ctx))
    except Exception as e:
        logger.warning("Edge Lab [%s]: errore salvataggio snapshot: %s", asset, e)

    if not market_ctx.get("is_tradeable", False):
        logger.info(
            "Edge Lab [%s]: market NOT tradeable — blocks=%s",
            asset, market_ctx.get("block_reasons", []),
        )
        return

    # ── OTE-SC: valuta BUY e SELL ───────────────────────────
    for direction in ("BUY", "SELL"):

        # Check 1: segnale già OPEN sulla stessa direzione
        if edge_lab_db.has_open_el_signal(conn, asset, direction, "OTE-SC"):
            logger.debug(
                "Edge Lab [%s %s]: segnale OPEN già presente, skip.",
                asset, direction,
            )
            continue

        # Check 2: segnale identico generato nelle ultime 2 ore
        # Evita duplicati quando il TP viene colpito in 1 bar e il setup
        # è ancora attivo al prossimo scan
        if edge_lab_db.has_recent_el_signal(conn, asset, direction, "OTE-SC", hours=2):
            logger.info(
                "Edge Lab [%s %s]: segnale già generato nelle ultime 2h, skip.",
                asset, direction,
            )
            continue

        try:
            result = generate_ote_sc_signal(market_ctx, df_m15, direction)
        except Exception as e:
            logger.error("Edge Lab [%s %s]: errore OTE-SC: %s", asset, direction, e)
            continue

        signal = result["signal"]
        diag   = result["diagnostics"]

        if signal is None:
            logger.info(
                "Edge Lab [%s %s]: no signal — %s",
                asset, direction, diag.get("rejection", "UNKNOWN"),
            )
            continue

        try:
            signal_id = edge_lab_db.insert_el_signal(conn, signal)
        except Exception as e:
            logger.error(
                "Edge Lab [%s %s]: errore inserimento segnale: %s",
                asset, direction, e,
            )
            continue

        logger.info(
            "Edge Lab [%s %s]: SEGNALE entry=%.4f sl=%.4f tp=%.4f rr=%.2f "
            "quality=%d/%s session=%s ref=%s target=%s flags=%s (id=%s)",
            asset, direction,
            signal["entry"], signal["stop_loss"], signal["tp"], signal["rr"],
            signal["quality_score"], signal["quality_label"],
            signal.get("session"), signal.get("ref_session"),
            signal.get("liquidity_target"),
            signal.get("tradeability_flags", []),
            signal_id,
        )

        _notify(signal, config)


def _notify(signal: dict, config: dict):
    try:
        from notifications import telegram_bot, ntfy_bot

        direction = signal["direction"]
        asset     = signal["asset"]
        emoji     = "🟢" if direction == "BUY" else "🔴"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if v > 1000 else f"{v:.4f}"

        text = (
            f"{emoji} *EDGE LAB — OTE-SC*\n\n"
            f"Asset: *{asset.replace('_',' ')}*\n"
            f"Direzione: *{direction}*\n\n"
            f"Entry:     `{fp(signal['entry'])}`\n"
            f"Stop Loss: `{fp(signal['stop_loss'])}`\n"
            f"TP:        `{fp(signal.get('tp'))}`\n"
            f"R/R: *{signal.get('rr',0):.2f}*\n\n"
            f"Quality: *{signal['quality_score']}/10* ({signal['quality_label']})\n"
            f"Session: {signal.get('session','N/A')} → Ref: {signal.get('ref_session','N/A')}\n"
            f"Target: {signal.get('liquidity_target','N/A')} "
            f"({signal.get('liquidity_target_priority','?')})\n"
            f"OTE: `{fp(signal.get('ote_low'))} – {fp(signal.get('ote_high'))}`\n"
            f"Trend: {signal.get('trend_combined','N/A')}"
        )

        if signal.get("tradeability_flags"):
            text += f"\n⚠️ Flags: {', '.join(signal['tradeability_flags'])}"

        bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id    = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            sent = telegram_bot.send_message(bot_token, chat_id, text)
            logger.info("Edge Lab [%s %s]: Telegram inviato=%s", asset, direction, sent)

        if ntfy_topic:
            title = f"OTE-SC {asset.replace('_',' ')} {direction} | Q{signal['quality_score']}/10"
            plain = text.replace("*","").replace("`","")
            ntfy_bot.send_message(ntfy_topic, title, plain)
            logger.info("Edge Lab [%s %s]: ntfy inviato", asset, direction)

    except Exception as e:
        logger.warning("Edge Lab _notify: errore notifica: %s", e)


def run_edge_lab_scan(config: dict):
    conn = core_db.get_connection(config["DB_PATH"])
    edge_lab_db.init_edge_lab_schema(conn)

    macro_provider = macro.get_provider(config)
    now    = datetime.now(timezone.utc)
    assets = config.get("EDGE_LAB", {}).get("assets", EDGE_LAB_ASSETS)

    logger.info("=== Edge Lab Scanner: inizio ciclo (%s) ===", ", ".join(assets))

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, macro_provider, now)
        except Exception as e:
            logger.error("Edge Lab [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== Edge Lab Scanner: fine ciclo ===")
