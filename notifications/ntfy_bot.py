"""
notifications/ntfy_bot.py  (V2.1)
Notifiche push via ntfy.sh - formato V2 multi-strategia.
"""

import logging
import requests

logger = logging.getLogger("ntfy_bot")
NTFY_BASE = "https://ntfy.sh"


def send_message(topic: str, title: str, message: str, priority: str = "high") -> bool:
    if not topic:
        logger.warning("NTFY_TOPIC non configurato.")
        return False
    try:
        resp = requests.post(
            f"{NTFY_BASE}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "chart_with_upwards_trend",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Errore ntfy: %s", e)
        return False


def format_signal_alert(signal) -> tuple:
    """Formato V2 - ritorna (title, body)."""
    direction_emoji = "🟢" if signal.direction == "LONG" else "🔴"
    title = f"{signal.strategy_name} | {signal.asset} {signal.direction} | Score {signal.final_score:.0f}"

    body = (
        f"{direction_emoji} {signal.asset} {signal.direction}\n"
        f"Entry: {signal.entry:.6f}\n"
        f"SL: {signal.stop_loss:.6f}\n"
        f"TP: {signal.take_profit:.6f}\n"
        f"R/R: {signal.rr:.2f}\n"
        f"Raw: {signal.raw_score:.0f} | Final: {signal.final_score:.0f}\n"
        f"Regime: {signal.market_regime or 'N/A'}"
    )

    ctx = signal.additional_context or {}
    macro_event = ctx.get("macro_event")
    if macro_event:
        mtr = macro_event.get("minutes_to_release", 0)
        body += (f"\n⚠️ {macro_event['type']} in {mtr} min"
                 if mtr >= 0
                 else f"\n⚠️ {macro_event['type']} {abs(mtr)} min ago")

    return title, body


def send_signal_alert(topic: str, signal) -> bool:
    title, body = format_signal_alert(signal)
    return send_message(topic, title, body)


# ============================================================
# Backward compatibility V1
# ============================================================

def format_alert(setup: dict, score: int, label: str) -> tuple:
    direction_emoji = "🟢" if setup["direzione"] == "LONG" else "🔴"
    title = f"{label} - {setup['asset']} {setup['direzione']}"
    body = (
        f"{direction_emoji} {setup['asset']} {setup['direzione']} | Score {score}/10\n"
        f"Entry: {setup['entry']:.6f}\n"
        f"SL: {setup['stop_loss']:.6f}\n"
        f"TP: {setup['take_profit']:.6f}\n"
        f"R/R: {setup['rr']:.2f}"
    )
    macro_info = setup.get("macro_event")
    if macro_info:
        mtr = macro_info.get("minutes_to_release", 0)
        body += (f"\n⚠️ {macro_info['type']} in {mtr} min"
                 if mtr >= 0
                 else f"\n⚠️ {macro_info['type']} {abs(mtr)} min ago")
    return title, body


def send_alert(topic: str, setup: dict, score: int, label: str) -> bool:
    title, body = format_alert(setup, score, label)
    return send_message(topic, title, body)
