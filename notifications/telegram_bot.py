"""
notifications/telegram_bot.py  (V2.1)
Notifiche Telegram multi-strategia.
"""

import logging
import requests

logger = logging.getLogger("telegram_bot")
TELEGRAM_API_BASE = "https://api.telegram.org"


def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID non configurati.")
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
            logger.error("Telegram API ok=False: %s", data)
            return False
        return True
    except requests.RequestException as e:
        logger.error("Errore invio Telegram: %s", e)
        return False


def format_signal_alert(signal, label: str) -> str:
    """Formato V2 multi-strategia."""
    direction_emoji = "🟢" if signal.direction == "LONG" else "🔴"
    ctx = signal.additional_context or {}

    text = (
        f"{label}\n\n"
        f"Strategy: *{signal.strategy_name} {signal.strategy_version}*\n"
        f"{direction_emoji} Asset: *{signal.asset}*\n"
        f"Direction: *{signal.direction}*\n\n"
        f"Entry:       `{signal.entry:.6f}`\n"
        f"Stop Loss:   `{signal.stop_loss:.6f}`\n"
        f"Take Profit: `{signal.take_profit:.6f}`\n"
        f"R/R: *{signal.rr:.2f}*\n\n"
        f"Raw Score:   *{signal.raw_score:.0f}/10*\n"
        f"Final Score: *{signal.final_score:.0f}/10*\n"
        f"Market Regime: {signal.market_regime or 'N/A'}"
    )

    macro_event = ctx.get("macro_event")
    if macro_event:
        mtr = macro_event.get("minutes_to_release", 0)
        if mtr >= 0:
            text += f"\n\n⚠️ Macro: {macro_event['type']} in {mtr} min"
        else:
            text += f"\n\n⚠️ Macro: {macro_event['type']} {abs(mtr)} min ago"

    return text


def _label_from_score(final_score: float) -> str:
    if final_score >= 10:
        return "⭐ ELITE SETUP"
    return "🔥 HIGH QUALITY SETUP"


def send_signal_alert(bot_token: str, chat_id: str, signal) -> bool:
    label = _label_from_score(signal.final_score)
    text = format_signal_alert(signal, label)
    return send_message(bot_token, chat_id, text)


# ============================================================
# Backward compatibility V1 (usato da test_alert)
# ============================================================

def format_alert(setup: dict, score: int, label: str) -> str:
    direction_emoji = "🟢" if setup["direzione"] == "LONG" else "🔴"

    pullback_type = []
    if setup.get("pullback_ema50"):
        pullback_type.append("EMA50")
    if setup.get("pullback_ema21"):
        pullback_type.append("EMA21")
    pullback_str = " + ".join(pullback_type) if pullback_type else "N/A"

    trend_h4_str = "✅" if setup.get("trend_h4_ok") else "❌"
    trend_h1_str = "✅" if setup.get("trend_h1_ok") else "❌"
    sr_str = "✅" if setup.get("sr_level_present") else "❌"

    text = (
        f"{label}\n\n"
        f"{direction_emoji} *{setup['asset']}* — *{setup['direzione']}*\n"
        f"Setup: {setup['setup']}\n"
        f"Score: *{score}/10*\n\n"
        f"Entry:       `{setup['entry']:.6f}`\n"
        f"Stop Loss:   `{setup['stop_loss']:.6f}`\n"
        f"Take Profit: `{setup['take_profit']:.6f}`\n"
        f"R/R: *{setup['rr']:.2f}*\n\n"
        f"Pullback: {pullback_str}\n"
        f"Trend H4: {trend_h4_str}\n"
        f"Trend H1: {trend_h1_str}\n"
        f"S/R confluence: {sr_str}"
    )

    macro_info = setup.get("macro_event")
    if macro_info:
        mtr = macro_info.get("minutes_to_release", 0)
        if mtr >= 0:
            text += f"\n\n⚠️ Macro: {macro_info['type']} in {mtr} min"
        else:
            text += f"\n\n⚠️ Macro: {macro_info['type']} {abs(mtr)} min ago"

    return text


def send_alert(bot_token: str, chat_id: str, setup: dict, score: int, label: str) -> bool:
    text = format_alert(setup, score, label)
    return send_message(bot_token, chat_id, text)
