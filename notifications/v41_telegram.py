"""
notifications/v41_telegram.py
Notifica Telegram dedicata a Institutional Scanner V4.1
Intraday Wave Edition.

Isolata da notifications/telegram_bot.py, v3_telegram.py, v4_telegram.py,
riusa solo la funzione di base send_message. Asset mostrato senza
underscore per evitare problemi di parsing Markdown di Telegram.
"""

from notifications.telegram_bot import send_message


def _fmt(v) -> str:
    if v is None:
        return "N/A"
    if v > 1000:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def format_v41_signal_alert(signal: dict) -> str:
    direction = signal["direction"]
    emoji = "🟢" if direction == "BUY" else "🔴"
    quality = signal["quality_score"]
    label = signal.get("quality_label", "MEDIUM")
    asset_display = signal["asset"].replace("_", " ")

    label_emoji = {"HIGH": "⭐", "MEDIUM": "▫️", "LOW": "🔹"}.get(label, "▫️")

    triggers = signal.get("trigger_types", [])
    triggers_str = " + ".join(triggers) if triggers else "N/A"

    liquidity_source = signal.get("liquidity_source") or "N/A"
    liquidity_target = signal.get("liquidity_target") or "N/A"

    lines = [
        f"{emoji} *INSTITUTIONAL SCANNER V4.1 — Intraday Wave*",
        "",
        f"Asset: *{asset_display}*",
        f"Direzione: *{direction}*",
        "",
        f"Entry: `{_fmt(signal['entry'])}`",
        f"Stop Loss: `{_fmt(signal['stop_loss'])}`",
        f"Take Profit: `{_fmt(signal.get('take_profit'))}`",
        f"R/R: *{signal.get('rr', 0):.2f}*",
        "",
        f"Trigger: *{triggers_str}*",
        f"Quality: {label_emoji} *{quality}/12* ({label})",
        "",
        f"Liquidity Source: {liquidity_source}",
        f"Liquidity Target: {liquidity_target}",
        "",
        f"EMA H4: {signal.get('ema_h4', 'N/A')}",
        f"EMA H1: {signal.get('ema_h1', 'N/A')}",
        f"Dow Theory H4: {signal.get('dow_theory_h4', 'N/A')}",
        f"Momentum: {signal.get('momentum', 'N/A')}",
        f"Zona H4: {'✓' if signal.get('in_h4_zone') else '✗'}",
        f"S/R Reaction: {'✓' if signal.get('sr_reaction') else '✗'}",
        f"OTE: {'✓' if signal.get('ote_present') else '✗'}",
        f"Sessione: {signal.get('session', 'N/A')}",
    ]
    return "\n".join(lines)


def send_v41_signal_alert(bot_token: str, chat_id: str, signal: dict) -> bool:
    text = format_v41_signal_alert(signal)
    return send_message(bot_token, chat_id, text)
