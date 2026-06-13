"""
ntfy_bot.py
Invio notifiche push via ntfy.sh (https://ntfy.sh) - servizio gratuito,
nessun account richiesto. Basta sottoscrivere un "topic" nell'app ntfy.
"""

import logging
import requests

logger = logging.getLogger("ntfy_bot")

NTFY_BASE = "https://ntfy.sh"


def send_message(topic: str, title: str, message: str, priority: str = "high") -> bool:
    if not topic:
        logger.warning("NTFY_TOPIC non configurato, alert ntfy non inviato.")
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
        logger.error("Errore invio notifica ntfy: %s", e)
        return False


def format_alert(setup: dict, score: int, label: str) -> tuple:
    """Ritorna (title, body)."""
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
        if macro_info["minutes_to_release"] >= 0:
            macro_text = f"⚠️ {macro_info['type']} in {macro_info['minutes_to_release']} min"
        else:
            macro_text = f"⚠️ {macro_info['type']} {abs(macro_info['minutes_to_release'])} min ago"
        body += f"\nMacro: {macro_text}"

    return title, body


def send_alert(topic: str, setup: dict, score: int, label: str) -> bool:
    title, body = format_alert(setup, score, label)
    return send_message(topic, title, body)
