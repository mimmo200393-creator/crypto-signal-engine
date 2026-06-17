"""
notifications/v3_telegram.py
Notifica Telegram dedicata all'Institutional Scanner Framework V3.2.

Formato isolato da notifications/telegram_bot.py esistente,
riusa solo la funzione di base send_message.
"""

import logging
from notifications.telegram_bot import send_message

logger = logging.getLogger("v3_telegram")


def _fmt(v) -> str:
    if v is None:
        return "N/A"
    if v > 1000:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def format_v3_signal_alert(signal: dict) -> str:
    direction = signal["direction"]
    emoji = "🟢" if direction == "BUY" else "🔴"
    quality = signal["signal_quality"]
    asset_display = signal["asset"].replace("_", " ")

    lines = [
        f"{emoji} *INSTITUTIONAL SCANNER V3.2*",
        "",
        f"Asset: *{asset_display}*",
        f"Direzione: *{direction}*",
        "",
        f"Entry: `{_fmt(signal['entry'])}`",
        f"Stop Loss: `{_fmt(signal['stop_loss'])}`",
        "",
        f"TP1: `{_fmt(signal.get('tp1'))}`",
        f"TP2: `{_fmt(signal.get('tp2'))}`",
        f"TP3: `{_fmt(signal.get('tp3'))}`",
        "",
        f"R/R: *{signal['rr']:.2f}*",
        f"Signal Quality: *{quality:.0f}/9*",
        "",
        f"Daily Context: {signal.get('daily_context_status', 'N/A')}",
        f"H4 Structure: {signal.get('h4_structure_status', 'N/A')}",
        f"H4 Zone: {signal.get('h4_zone_status', 'N/A')}",
        f"OTE: {'✓' if signal.get('ote_present') else '✗'}",
        f"Pullback: {signal.get('pullback_type', 'N/A')}",
        f"M30 Transition: {signal.get('m30_transition_status', 'N/A')}",
        f"M15 BOS: {'✓' if signal.get('m15_bos_confirmed') else '✗'}",
        f"Sessione: {signal.get('session', 'N/A')}",
    ]
    return "\n".join(lines)


def send_v3_signal_alert(bot_token: str, chat_id: str, signal: dict) -> bool:
    text = format_v3_signal_alert(signal)
    return send_message(bot_token, chat_id, text)
