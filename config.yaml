"""
telegram_bot.py
Invio notifiche Telegram per setup ad alta probabilita' (score >= TELEGRAM_SCORE_THRESHOLD).
"""

import logging
import requests

logger = logging.getLogger("telegram_bot")

TELEGRAM_API_BASE = "https://api.telegram.org"


def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    """
    Invia un messaggio di testo (parse_mode Markdown) alla chat configurata.
    Ritorna True se l'invio ha avuto successo, False altrimenti.
    """
    if not bot_token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID non configurati, alert non inviato.")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.error("Telegram API ha risposto ok=False: %s", data)
            return False
        return True
    except requests.RequestException as e:
        logger.error("Errore invio messaggio Telegram: %s", e)
        return False


def format_alert(setup: dict, score: int, label: str) -> str:
    """
    Formatta il messaggio di alert con tutte le informazioni richieste
    dalla spec.
    """
    direction_emoji = "🟢" if setup["direzione"] == "LONG" else "🔴"

    macro_info = setup.get("macro_event")
    if macro_info:
        macro_text = (
            f"⚠️ {macro_info['type']} in {macro_info['minutes_to_release']} min"
            if macro_info["minutes_to_release"] >= 0
            else f"⚠️ {macro_info['type']} {abs(macro_info['minutes_to_release'])} min fa"
        )
    else:
        macro_text = "Nessun evento rilevante"

    pullback_type = []
    if setup.get("pullback_ema50"):
        pullback_type.append("EMA50")
    if setup.get("pullback_ema21"):
        pullback_type.append("EMA21")
    pullback_str = " + ".join(pullback_type) if pullback_type else "N/D"

    trend_h4_str = "✅" if setup.get("trend_h4_ok") else "❌"
    trend_h1_str = "✅" if setup.get("trend_h1_ok") else "❌"
    sr_str = "✅" if setup.get("sr_level_present") else "❌"

    text = (
        f"{label}\n\n"
        f"{direction_emoji} *{setup['asset']}* — *{setup['direzione']}*\n"
        f"Setup: {setup['setup']}\n"
        f"Score: *{score}/10*\n\n"
        f"Entry: `{setup['entry']:.6f}`\n"
        f"Stop Loss: `{setup['stop_loss']:.6f}`\n"
        f"Take Profit: `{setup['take_profit']:.6f}`\n"
        f"R/R: *{setup['rr']:.2f}*\n\n"
        f"Pullback: {pullback_str}\n"
        f"Trend H4: {trend_h4_str}\n"
        f"Trend H1: {trend_h1_str}\n"
        f"S/R confluenza: {sr_str}\n\n"
        f"Macro: {macro_text}"
    )
    return text


def send_alert(bot_token: str, chat_id: str, setup: dict, score: int, label: str) -> bool:
    text = format_alert(setup, score, label)
    return send_message(bot_token, chat_id, text)
