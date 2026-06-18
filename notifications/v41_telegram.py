"""
notifications/v41_telegram.py
Notifica Telegram dedicata a Institutional Scanner V4.1
Intraday Wave Edition.

Isolata da notifications/telegram_bot.py, v3_telegram.py, v4_telegram.py,
riusa solo la funzione di base send_message. Asset mostrato senza
underscore per evitare problemi di parsing Markdown di Telegram.
"""

from notifications.telegram_bot import send_message
from notifications import ntfy_bot


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

    ote_low = signal.get("ote_entry_low")
    ote_high = signal.get("ote_entry_high")
    ote_zone_str = f"{_fmt(ote_low)} - {_fmt(ote_high)}" if ote_low is not None and ote_high is not None else "N/A"

    em_points = signal.get("expected_move_points")
    em_barrier = signal.get("expected_move_barrier") or "N/A"
    em_str = f"{em_points:.1f}pt → {em_barrier}" if em_points is not None else "N/A"

    lines = [
        f"{emoji} *INSTITUTIONAL SCANNER V4.1 — Intraday Wave*",
        "",
        f"Asset: *{asset_display}*",
        f"Direzione: *{direction}*",
        "",
        f"Entry: `{_fmt(signal['entry'])}`",
        f"Stop Loss: `{_fmt(signal['stop_loss'])}`",
        f"TP1 (1R): `{_fmt(signal.get('tp1'))}`",
        f"TP2 (2R): `{_fmt(signal.get('tp2'))}`",
        f"R/R: *{signal.get('rr', 0):.2f}*",
        "",
        f"Trigger: *{triggers_str}*",
        f"Quality: {label_emoji} *{quality}/12* ({label})",
        "",
        f"Liquidity Source: {liquidity_source}",
        f"Liquidity Target: {liquidity_target}",
        f"OTE Entry Zone: {ote_zone_str}",
        f"Expected Move: {em_str}",
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


def format_v41_signal_alert_plain(signal: dict) -> tuple:
    """
    Formato plain-text (senza Markdown) per ntfy. Ritorna (title, body).
    """
    direction = signal["direction"]
    asset_display = signal["asset"].replace("_", " ")
    quality = signal["quality_score"]
    label = signal.get("quality_label", "MEDIUM")
    triggers = signal.get("trigger_types", [])
    triggers_str = " + ".join(triggers) if triggers else "N/A"

    title = f"V4.1 {asset_display} {direction} | Quality {quality}/12 ({label})"

    ote_low = signal.get("ote_entry_low")
    ote_high = signal.get("ote_entry_high")
    ote_zone_str = f"{_fmt(ote_low)} - {_fmt(ote_high)}" if ote_low is not None and ote_high is not None else "N/A"

    body = (
        f"Entry: {_fmt(signal['entry'])}\n"
        f"Stop Loss: {_fmt(signal['stop_loss'])}\n"
        f"TP1 (1R): {_fmt(signal.get('tp1'))}\n"
        f"TP2 (2R): {_fmt(signal.get('tp2'))}\n"
        f"R/R: {signal.get('rr', 0):.2f}\n"
        f"Trigger: {triggers_str}\n"
        f"Liquidity Source: {signal.get('liquidity_source') or 'N/A'}\n"
        f"Liquidity Target: {signal.get('liquidity_target') or 'N/A'}\n"
        f"OTE Entry Zone: {ote_zone_str}\n"
        f"Sessione: {signal.get('session', 'N/A')}"
    )
    return title, body


def send_v41_signal_alert_all_channels(bot_token: str, chat_id: str, ntfy_topic: str, signal: dict) -> dict:
    """
    Invia il Trade Alert su entrambi i canali (Telegram + ntfy),
    indipendentemente l'uno dall'altro: se uno fallisce, l'altro
    viene comunque tentato. Ritorna {"telegram": bool, "ntfy": bool}.
    """
    telegram_sent = send_v41_signal_alert(bot_token, chat_id, signal)
    title, body = format_v41_signal_alert_plain(signal)
    ntfy_sent = ntfy_bot.send_message(ntfy_topic, title, body)
    return {"telegram": telegram_sent, "ntfy": ntfy_sent}


# ============================================================
# Watchlist Alert (preparatorio, non operativo)
# ============================================================

def format_v41_watchlist_alert(asset: str, proximity: dict) -> str:
    asset_display = asset.replace("_", " ")
    direction = proximity["potential_direction"]
    emoji = "🟢" if direction == "BUY" else "🔴"

    lines = [
        f"👀 *WATCHLIST — V4.1 Intraday Wave*",
        "",
        f"Asset: *{asset_display}*",
        "",
        f"Liquidity Zone: *{proximity['label']}*",
        f"Level: `{_fmt(proximity['price'])}`",
        f"Distance: *{proximity['distance_pct'] * 100:.2f}%*",
        "",
        f"Potential Direction: {emoji} *{direction}*",
        "",
        "_Alert preparatorio: nessuna conferma BOS/CHOCH ancora presente._",
    ]
    return "\n".join(lines)


def send_v41_watchlist_alert(bot_token: str, chat_id: str, asset: str, proximity: dict) -> bool:
    text = format_v41_watchlist_alert(asset, proximity)
    return send_message(bot_token, chat_id, text)


def format_v41_watchlist_alert_plain(asset: str, proximity: dict) -> tuple:
    """
    Formato plain-text (senza Markdown) per ntfy. Ritorna (title, body).
    """
    asset_display = asset.replace("_", " ")
    direction = proximity["potential_direction"]

    title = f"WATCHLIST V4.1 {asset_display} | {proximity['label']} -> {direction}"
    body = (
        f"Level: {_fmt(proximity['price'])}\n"
        f"Distance: {proximity['distance_pct'] * 100:.2f}%\n"
        f"Potential Direction: {direction}\n"
        f"Alert preparatorio: nessuna conferma BOS/CHOCH ancora presente."
    )
    return title, body


def send_v41_watchlist_alert_all_channels(bot_token: str, chat_id: str, ntfy_topic: str,
                                           asset: str, proximity: dict) -> dict:
    """
    Invia il Watchlist Alert su entrambi i canali (Telegram + ntfy),
    indipendentemente l'uno dall'altro. Ritorna {"telegram": bool, "ntfy": bool}.
    """
    telegram_sent = send_v41_watchlist_alert(bot_token, chat_id, asset, proximity)
    title, body = format_v41_watchlist_alert_plain(asset, proximity)
    ntfy_sent = ntfy_bot.send_message(ntfy_topic, title, body)
    return {"telegram": telegram_sent, "ntfy": ntfy_sent}
