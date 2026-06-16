"""
notifications/v4_telegram.py
Notifica Telegram dedicata a Institutional Scanner V4.0 Daily Edition.

Isolata da notifications/telegram_bot.py e da v3_telegram.py,
riusa solo la funzione di base send_message.
"""

from notifications.telegram_bot import send_message


def _fmt(v) -> str:
    if v is None:
        return "N/A"
    if v > 1000:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def format_v4_signal_alert(signal: dict) -> str:
    direction = signal["direction"]
    emoji = "🟢" if direction == "BUY" else "🔴"
    quality = signal["signal_quality"]
    label = signal.get("quality_label", "STANDARD")

    label_emoji = {"HIGH": "⭐", "STANDARD": "▫️", "LOW": "🔹"}.get(label, "▫️")

    lines = [
        f"{emoji} *INSTITUTIONAL SCANNER V4.0 — Daily Edition*",
        "",
        f"Asset: *{signal['asset']}*",
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
        f"Signal Quality: {label_emoji} *{quality:.0f}/5* ({label})",
        "",
        f"Daily Context: {signal.get('daily_context_status', 'N/A')}",
        f"H4 Structure: {signal.get('h4_structure_status', 'N/A')}",
        f"H4 Zone: {signal.get('h4_zone_status', 'N/A')}",
        f"OTE: {'✓' if signal.get('ote_present') else '✗'}",
        f"Pullback: {signal.get('pullback_type', 'N/A')}",
        f"M30 Transition: {signal.get('m30_transition_status', 'N/A')}",
        f"Sessione: {signal.get('session', 'N/A')}",
    ]
    return "\n".join(lines)


def send_v4_signal_alert(bot_token: str, chat_id: str, signal: dict) -> bool:
    text = format_v4_signal_alert(signal)
    return send_message(bot_token, chat_id, text)
