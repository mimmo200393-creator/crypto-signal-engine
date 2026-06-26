"""
notifications/telegram_bot.py  (V2.2)
Notifiche Telegram multi-strategia.
Fix: fallback plain text se Markdown fallisce (400 Bad Request).
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

    # Tentativo 1: con Markdown
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        # Se 400 Bad Request → riprova senza parse_mode
        if resp.status_code == 400:
            logger.warning(
                "Telegram Markdown fallito (400), riprovo in plain text: %s",
                resp.text[:200],
            )
            plain_text = text.replace("*", "").replace("`", "").replace("_", " ")
            payload_plain = {
                "chat_id": chat_id,
                "text": plain_text,
                "disable_web_page_preview": True,
            }
            resp2 = requests.post(url, json=payload_plain, timeout=10)
            resp2.raise_for_status()
            data2 = resp2.json()
            if not data2.get("ok"):
                logger.error("Telegram plain text ok=False: %s", data2)
                return False
            return True
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
    asset_display = signal.asset.replace("_", " ")

    text = (
        f"{label}\n\n"
        f"Strategy: *{signal.strategy_name} {signal.strategy_version}*\n"
        f"{direction_emoji} Asset: *{asset_display}*\n"
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
# Backward compatibility V1
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


# ============================================================
# Zone + Confirmation
# ============================================================

def format_zone_signal_alert(signal, label: str) -> str:
    direction_emoji = "🟢" if signal.direction == "LONG" else "🔴"
    ctx = signal.additional_context or {}

    zone_level   = ctx.get("zone_level", 0)
    zone_touches = ctx.get("zone_touches", 0)
    bias_h4      = ctx.get("bias_h4", "N/A")
    pattern      = ctx.get("pattern_name", "N/A")
    macro_risk   = ctx.get("macro_risk", "LOW")
    session      = ctx.get("session", "N/A")
    momentum     = ctx.get("momentum", "N/A")
    atr_daily    = ctx.get("atr_daily", 0)

    def fp(v):
        if v > 1000: return f"{v:,.2f}"
        elif v > 1:  return f"{v:.4f}"
        elif v > 0.001: return f"{v:.5f}"
        return f"{v:.8f}"

    momentum_arrow = "↓" if momentum == "DOWN" else "↑"
    asset_display = signal.asset.replace("_", " ")

    text = (
        f"{label}\n\n"
        f"Strategia: *Zone + Confirmation V1.0*\n"
        f"{direction_emoji} Asset: *{asset_display}*\n"
        f"Direzione: *{signal.direction}*\n\n"
        f"Entry:       `{fp(signal.entry)}`\n"
        f"Stop Loss:   `{fp(signal.stop_loss)}`\n"
        f"Take Profit: `{fp(signal.take_profit)}`\n"
        f"R/R: *{signal.rr:.2f}*\n\n"
        f"Raw Score:   *{signal.raw_score:.0f}/11*\n"
        f"Final Score: *{signal.final_score:.0f}/11*\n\n"
        f"Bias H4: {bias_h4}\n"
        f"Zona: `{fp(zone_level)}` ({zone_touches} tocchi)\n"
        f"ATR Daily: `{fp(atr_daily)}`\n"
        f"Macro Risk: {macro_risk}\n"
        f"Sessione: {session}\n"
        f"Momentum: {momentum_arrow} {momentum}\n"
        f"Pattern: {pattern}"
    )

    macro_event = ctx.get("macro_event")
    if macro_event and macro_risk in ("MEDIUM", "HIGH"):
        mtr = macro_event.get("minutes_to_release", 0)
        if mtr >= 0:
            text += f"\n\n⚠️ Macro: {macro_event['type']} in {mtr} min"
        else:
            text += f"\n\n⚠️ Macro: {macro_event['type']} {abs(mtr)} min ago"

    return text


def send_zone_signal_alert(bot_token: str, chat_id: str, signal) -> bool:
    score = signal.final_score
    label = "⭐ ELITE SETUP" if score >= 9 else "🔥 HIGH QUALITY SETUP"
    text = format_zone_signal_alert(signal, label)
    return send_message(bot_token, chat_id, text)
    
