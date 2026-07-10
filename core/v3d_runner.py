"""
core/v3d_runner.py
Orchestratore di Institutional Scanner Framework V3.2 Dynamic (sperimentale).

Identico a v3_runner.py tranne per:
- Chiama generate_v3d_signal() invece di generate_v3_signal()
- Salva in v3d_signals (tabella separata, stesso schema di v3_signals)
- Logging con prefisso V3D per distinguere dai log V3.2 Frozen

── MULTI-PROVIDER: perché qui NON si fetcha ──────────────────────
v3d_runner scarica GLI STESSI timeframe (D1/M30/M15) nella STESSA
tabella v3_candles_cache di v3_runner. Nel workflow v3_scanner_runner.py
gira PRIMA, quindi quando parte V3D le candele sono già aggiornate.

Su Crypto.com il refetch è solo spreco (chiamate gratuite).
Su provider a consumo (Twelve Data per XAU_USD, 800 chiamate/giorno)
RADDOPPIA i crediti: 198 → 396 chiamate/giorno per D1/M30/M15.

Quindi: per gli asset a consumo il fetch viene saltato. Per Crypto.com
resta il comportamento di prima (refetch ridondante ma innocuo, e
protegge il caso in cui V3 non abbia girato).
"""

import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import indicators
from core import data_source
from core import v3_db
from strategies import institutional_scanner_v3_dynamic as v3d
from notifications import v3_telegram
from notifications import ntfy_bot

logger = logging.getLogger("v3d_runner")

V3D_TIMEFRAMES = {"D1": "1D", "H4": "4h", "H1": "1h", "M30": "30m", "M15": "15m"}


def _update_candles(conn, asset: str, config: dict):
    """
    Aggiorna D1/M30/M15 per un asset.

    NOTA: chiamata solo per gli asset NON a consumo (vedi _run_for_asset).
    Per XAU_USD & co. le candele arrivano già da v3_runner, che gira prima
    e scrive nella stessa tabella.
    """
    base_url = config["EXCHANGE_BASE_URL"]
    max_per_call = config.get("MAX_CANDLES_PER_CALL", 300)
    request_delay = config.get("REQUEST_DELAY_SECONDS", 0.75)
    target_candles = config.get("BOOTSTRAP_TARGET_CANDLES", 300)

    exchange = data_source.get_provider(asset, family="v3")

    for tf_label in ("D1", "M30", "M15"):
        tf = V3D_TIMEFRAMES[tf_label]
        existing_count = v3_db.count_v3_candles(conn, asset, tf)

        if existing_count < 50:
            try:
                candles = exchange.bootstrap_history(
                    base_url, asset, tf, target_candles, max_per_call, request_delay
                )
            except Exception as e:
                logger.error("V3D bootstrap %s %s fallito: %s", asset, tf, e)
                continue
            v3_db.upsert_v3_candles(conn, asset, tf, candles)
        else:
            last_ts = v3_db.get_v3_latest_timestamp(conn, asset, tf)

            if not data_source.should_fetch(asset, tf, last_ts):
                continue

            try:
                new_candles = exchange.fetch_new_candles_since(
                    base_url, asset, tf, last_ts, max_per_call, request_delay
                )
            except Exception as e:
                logger.error("V3D update %s %s fallito: %s", asset, tf, e)
                continue

            if new_candles:
                v3_db.upsert_v3_candles(conn, asset, tf, new_candles)


def _prepare_dataframes(conn, asset: str, config: dict):
    limit = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    df_h4 = core_db.get_candles_df(conn, asset, V3D_TIMEFRAMES["H4"], limit=limit)
    df_h1 = core_db.get_candles_df(conn, asset, V3D_TIMEFRAMES["H1"], limit=limit)
    df_d1 = v3_db.get_v3_candles_df(conn, asset, V3D_TIMEFRAMES["D1"], limit=limit)
    df_m30 = v3_db.get_v3_candles_df(conn, asset, V3D_TIMEFRAMES["M30"], limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, V3D_TIMEFRAMES["M15"], limit=limit)

    ema_periods = config.get("EMA_PERIODS", [21, 50, 100, 200])
    atr_period = config.get("ATR_PERIOD", 14)

    for df in (df_h4, df_h1, df_d1, df_m30, df_m15):
        if len(df) > atr_period:
            indicators.add_atr(df, atr_period)

    if len(df_h4) > max(ema_periods):
        indicators.add_emas(df_h4, ema_periods)
    if len(df_h1) > max(ema_periods):
        indicators.add_emas(df_h1, ema_periods)
    if len(df_d1) > 200:
        indicators.add_emas(df_d1, [50, 200])

    return df_d1, df_h4, df_h1, df_m30, df_m15


def _run_for_asset(conn, asset: str, config: dict):
    logger.info("V3D Scanner: inizio ciclo per %s", asset)

    # ── Fetch candele ────────────────────────────────────────
    # Per gli asset a consumo (Twelve Data) le candele D1/M30/M15 sono
    # già state aggiornate da v3_runner: stessa tabella, gira prima nel
    # workflow. Rifetchare qui raddoppierebbe i crediti consumati.
    if data_source.is_metered(asset):
        logger.info(
            "V3D [%s]: skip fetch, candele già aggiornate da V3 (provider a consumo)",
            asset,
        )
    else:
        try:
            _update_candles(conn, asset, config)
        except Exception as e:
            logger.error("V3D Scanner [%s]: errore update candele: %s", asset, e)
            return

    df_d1, df_h4, df_h1, df_m30, df_m15 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 35 or len(df_m30) < 20 or len(df_m15) < 10:
        logger.warning("V3D Scanner [%s]: dati insufficienti, skip.", asset)
        return

    market_data = {
        "asset": asset,
        "df_d1": df_d1, "df_h4": df_h4, "df_h1": df_h1,
        "df_m30": df_m30, "df_m15": df_m15,
        "timestamp": datetime.now(timezone.utc),
    }

    result = v3d.generate_v3d_signal(market_data)
    signal = result["signal"]
    diagnostics = result["diagnostics"]

    h4_status = diagnostics.get("h4_structure", "?")
    bidirectional = diagnostics.get("h4_neutral_bidirectional", False)
    logger.info(
        "V3D Scanner [%s] diagnostics: h4_structure=%s bidirectional=%s rejections=%s",
        asset, h4_status, bidirectional, diagnostics.get("rejections", [])
    )

    if signal is None:
        logger.info("V3D Scanner [%s]: nessun segnale generato.", asset)
        return

    import uuid
    signal_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO v3d_signals (
            signal_id, timestamp_setup, asset, direction,
            entry, stop_loss, tp1, tp2, tp3, rr, signal_quality,
            daily_context_status, h4_structure_status, h4_zone_status,
            ote_present, pullback_type, pullback_invalidated,
            m30_transition_status, m15_bos_confirmed, session,
            trader_decision, final_outcome
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unknown','OPEN')
    """, (
        signal_id, signal["timestamp_setup"], signal["asset"], signal["direction"],
        signal["entry"], signal["stop_loss"], signal.get("tp1"), signal.get("tp2"),
        signal.get("tp3"), signal["rr"], signal["signal_quality"],
        signal.get("daily_context_status"), signal.get("h4_structure_status"),
        signal.get("h4_zone_status"), signal.get("ote_present", False),
        signal.get("pullback_type"), signal.get("pullback_invalidated", False),
        signal.get("m30_transition_status"), signal.get("m15_bos_confirmed", False),
        signal.get("session"),
    ))
    conn.commit()

    logger.info(
        "V3D Scanner [%s]: SEGNALE generato [%s] score=%.0f/10 RR=%.2f h4=%s%s (id=%s)",
        asset, signal["direction"], signal["signal_quality"], signal["rr"],
        h4_status, " [NEUTRAL-bidirectional]" if bidirectional else "", signal_id
    )

    bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.get("TELEGRAM_CHAT_ID", "")
    ntfy_topic = config.get("NTFY_TOPIC", "")
    if bot_token and chat_id:
        sent = v3_telegram.send_v3_signal_alert(bot_token, chat_id, signal)
        logger.info("V3D Scanner [%s]: notifica Telegram inviata=%s", asset, sent)
    if ntfy_topic:
        ntfy_sent = v3_telegram.send_v3_signal_alert_ntfy(ntfy_topic, signal)
        logger.info("V3D Scanner [%s]: notifica ntfy inviata=%s", asset, ntfy_sent)


def run_v3d_scan(config: dict):
    conn = core_db.get_connection(config["DB_PATH"])

    conn.execute("""
        CREATE TABLE IF NOT EXISTS v3d_signals AS
        SELECT * FROM v3_signals WHERE 0
    """)
    conn.commit()

    # Asset da config (fonte di verità unica). Fallback allineato al nuovo
    # setup: BTC su Crypto.com, oro su Twelve Data. PAXG rimosso.
    assets = config.get("V3D_SCANNER", {}).get("assets", ["BTC_USDT", "XAU_USD"])

    logger.info("=== V3D Scanner (Dynamic): inizio ciclo (%s) ===", ", ".join(assets))

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config)
        except Exception as e:
            logger.error("V3D Scanner [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== V3D Scanner (Dynamic): fine ciclo ===")
