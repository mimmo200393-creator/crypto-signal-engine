"""
core/daily_brief.py
Daily Brief — Zone + Confirmation Strategy V1.0

Genera ogni giorno alle 08:00 UTC una mappa operativa per
BTC_USDT, ETH_USDT, PAXG_USDT con:
- Zone H4 significative (supporti/resistenze)
- Bias direzionale
- ATR giornaliero
- Suggerimento operativo

Invia su Telegram e ntfy.
"""

import logging
from datetime import datetime, timezone

from core.indicators import find_pivots, cluster_levels
from storage import db
from notifications import telegram_bot, ntfy_bot

logger = logging.getLogger("daily_brief")

ZONE_ASSETS = ["BTC_USDT", "ETH_USDT", "PAXG_USDT"]
ZONE_LOOKBACK_H4 = 20
ZONE_MIN_TOUCHES = 2
ZONE_CLUSTER_ATR = 0.5


def _get_bias(df_h4) -> str:
    last = df_h4.iloc[-1]
    e50, e100, e200 = last["ema_50"], last["ema_100"], last["ema_200"]
    if e50 > e100 > e200:
        return "RIALZISTA 🟢"
    if e50 < e100 < e200:
        return "RIBASSISTA 🔴"
    return "NEUTRALE ⚪"


def _get_zones(df_h4, zone_type: str, atr_h4: float) -> list:
    lookback = df_h4.iloc[-ZONE_LOOKBACK_H4:].copy().reset_index(drop=True)
    pivots = find_pivots(lookback, lookback=3)

    if zone_type == "support":
        raw = pivots["pivot_lows"]
    else:
        raw = pivots["pivot_highs"]

    clusters = cluster_levels(raw, atr_h4, ZONE_CLUSTER_ATR)
    return sorted(
        [c for c in clusters if c["count"] >= ZONE_MIN_TOUCHES],
        key=lambda z: z["price"],
        reverse=(zone_type == "resistance")
    )


def _atr_daily(df_h4) -> float:
    if len(df_h4) < 6:
        return 0.0
    highs = df_h4["high"].values[-6:]
    lows  = df_h4["low"].values[-6:]
    return float((highs - lows).mean())


def _fmt_price(v: float) -> str:
    if v > 1000:
        return f"{v:,.2f}"
    elif v > 1:
        return f"{v:.4f}"
    elif v > 0.001:
        return f"{v:.5f}"
    else:
        return f"{v:.8f}"


def _build_brief_message(asset: str, df_h1, df_h4) -> str:
    price   = float(df_h1.iloc[-1]["close"])
    atr_h4  = float(df_h4.iloc[-1]["atr"]) if "atr" in df_h4.columns else 0
    bias    = _get_bias(df_h4)
    atr_day = _atr_daily(df_h4)

    supports    = _get_zones(df_h4, "support", atr_h4)
    resistances = _get_zones(df_h4, "resistance", atr_h4)

    sup_list = [z for z in supports if z["price"] < price][:3]
    res_list = [z for z in resistances if z["price"] > price][:3]

    last_h4 = df_h4.iloc[-1]
    ema50   = float(last_h4["ema_50"])
    ema200  = float(last_h4["ema_200"])

    if "RIALZISTA" in bias:
        if sup_list:
            hint = f"→ Cercare LONG sui pullback verso {_fmt_price(sup_list[0]['price'])}"
        else:
            hint = "→ Trend rialzista, attendere pullback"
    elif "RIBASSISTA" in bias:
        if res_list:
            hint = f"→ Cercare SHORT sui rimbalzi verso {_fmt_price(res_list[0]['price'])}"
        else:
            hint = "→ Trend ribassista, attendere rimbalzo"
    else:
        hint = "→ Mercato neutrale, attendere direzionalità"

    lines = [
        f"📊 *DAILY BRIEF — {datetime.now(timezone.utc).strftime('%d %b %Y 08:00 UTC')}*",
        "",
        f"*{asset}*",
        f"Prezzo: `{_fmt_price(price)}`",
        f"Bias H4: {bias}",
        f"ATR Daily: `{_fmt_price(atr_day)}`",
        f"EMA50 H4: `{_fmt_price(ema50)}` | EMA200 H4: `{_fmt_price(ema200)}`",
        "",
    ]

    if sup_list:
        lines.append("*Supporti:*")
        for z in sup_list:
            lines.append(f"  `{_fmt_price(z['price'])}` ({z['count']} tocchi)")
    else:
        lines.append("*Supporti:* nessuno significativo")

    lines.append("")

    if res_list:
        lines.append("*Resistenze:*")
        for z in res_list:
            lines.append(f"  `{_fmt_price(z['price'])}` ({z['count']} tocchi)")
    else:
        lines.append("*Resistenze:* nessuna significativa")

    lines.extend(["", hint])
    return "\n".join(lines)


def send_daily_brief(conn, config: dict):
    """
    Genera e invia il Daily Brief per BTC/ETH/PAXG.
    Chiamato dal cron job delle 08:00 UTC.
    """
    bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
    chat_id    = config.get("TELEGRAM_CHAT_ID", "")
    ntfy_topic = config.get("NTFY_TOPIC", "")
    limit      = config.get("BOOTSTRAP_TARGET_CANDLES", 300)

    assets_in_watchlist = [a for a in ZONE_ASSETS if a in config.get("WATCHLIST", [])]
    if not assets_in_watchlist:
        assets_in_watchlist = ZONE_ASSETS

    full_message_parts = []

    for asset in assets_in_watchlist:
        df_h1 = db.get_candles_df(conn, asset, config["TIMEFRAMES"]["H1"], limit=limit)
        df_h4 = db.get_candles_df(conn, asset, config["TIMEFRAMES"]["H4"], limit=limit)

        if len(df_h1) < 25 or len(df_h4) < 25:
            logger.warning("Daily Brief: dati insufficienti per %s", asset)
            continue

        if "ema_50" not in df_h4.columns:
            from core import indicators
            indicators.add_emas(df_h4, [21, 50, 100, 200])
            indicators.add_atr(df_h4, 14)

        try:
            msg = _build_brief_message(asset, df_h1, df_h4)
            full_message_parts.append(msg)
            logger.info("Daily Brief generato per %s", asset)
        except Exception as e:
            logger.error("Errore Daily Brief per %s: %s", asset, e)

    if not full_message_parts:
        logger.warning("Daily Brief: nessun messaggio generato")
        return

    full_message = "\n\n---\n\n".join(full_message_parts)

    if bot_token and chat_id:
        sent = telegram_bot.send_message(bot_token, chat_id, full_message)
        logger.info("Daily Brief Telegram: %s", sent)

    if ntfy_topic:
        title = f"Daily Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}"
        plain = full_message.replace("*", "").replace("`", "")
        ntfy_bot.send_message(ntfy_topic, title, plain)
        logger.info("Daily Brief ntfy inviato")
