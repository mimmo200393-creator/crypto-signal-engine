"""
notifications/v41p1_telegram.py
Notifiche Telegram e ntfy per Institutional Scanner V4.1 Phase 1.
Formato dedicato che mostra la Money Flow Map con Priority Score.
"""

from notifications.telegram_bot import send_message
from notifications import ntfy_bot


def _fmt(v) -> str:
    if v is None:
        return "N/A"
    if v > 1000:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def _priority_emoji(label: str) -> str:
    return {
        "CRITICAL": "🔴",
        "HIGH":     "🟠",
        "MEDIUM":   "🟡",
        "LOW":      "⚪",
    }.get(label, "⚪")


def format_v41p1_signal_alert(signal: dict) -> str:
    direction = signal["direction"]
    emoji = "🟢" if direction == "BUY" else "🔴"
    asset_display = signal["asset"].replace("_", " ")
    quality = signal["quality_score"]
    label = signal.get("quality_label", "MEDIUM")
    label_emoji = {"HIGH": "⭐", "MEDIUM": "▫️", "LOW": "🔹"}.get(label, "▫️")

    triggers = signal.get("trigger_types", [])
    triggers_str = " + ".join(triggers) if triggers else "N/A"

    # Money Flow Map
    na_label = signal.get("nearest_above_label") or "N/A"
    na_price = signal.get("nearest_above_price")
    na_prio  = signal.get("nearest_above_priority") or ""
    na_score = signal.get("nearest_above_score")
    na_dist  = signal.get("distance_to_nearest_above_pct")

    nb_label = signal.get("nearest_below_label") or "N/A"
    nb_price = signal.get("nearest_below_price")
    nb_prio  = signal.get("nearest_below_priority") or ""
    nb_score = signal.get("nearest_below_score")
    nb_dist  = signal.get("distance_to_nearest_below_pct")

    src_label = signal.get("liquidity_source") or "N/A"
    src_prio  = signal.get("liquidity_source_priority") or ""
    src_score = signal.get("liquidity_source_score")

    tgt_label = signal.get("liquidity_target") or "N/A"
    tgt_price = signal.get("liquidity_target_price")
    tgt_prio  = signal.get("liquidity_target_priority") or ""
    tgt_score = signal.get("liquidity_target_score")

    em_points = signal.get("expected_move_points")
    em_str = f"{em_points:.1f}pt" if em_points is not None else "N/A"

    def prio_str(label, score):
        if not label:
            return ""
        pe = _priority_emoji(label)
        sc = f"{score:.2f}" if score is not None else "?"
        return f"{pe} {label} {sc}"

    def dist_str(d, sign="+"):
        if d is None:
            return ""
        return f" ({sign}{d*100:.2f}%)"

    lines = [
        f"{emoji} *INSTITUTIONAL SCANNER V4.1 — Phase 1*",
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
        "💧 *Money Flow Map*",
        f"Nearest Above: {na_label} @ `{_fmt(na_price)}`{dist_str(na_dist,'+')} {prio_str(na_prio, na_score)}",
        f"Nearest Below: {nb_label} @ `{_fmt(nb_price)}`{dist_str(nb_dist,'-')} {prio_str(nb_prio, nb_score)}",
        "",
        f"Source: {src_label} {prio_str(src_prio, src_score)}",
        f"Target: {tgt_label} @ `{_fmt(tgt_price)}` {prio_str(tgt_prio, tgt_score)}",
        f"Expected Move: *{em_str}*",
        "",
        f"EMA H4: {signal.get('ema_h4', 'N/A')}",
        f"EMA H1: {signal.get('ema_h1', 'N/A')}",
        f"Dow Theory H4: {signal.get('dow_theory_h4', 'N/A')}",
        f"Momentum: {signal.get('momentum', 'N/A')}",
        f"Sessione: {signal.get('session', 'N/A')}",
    ]
    return "\n".join(lines)


def send_v41p1_signal_alert(bot_token: str, chat_id: str, signal: dict) -> bool:
    text = format_v41p1_signal_alert(signal)
    return send_message(bot_token, chat_id, text)


def format_v41p1_signal_alert_plain(signal: dict) -> tuple:
    """Formato plain-text per ntfy. Ritorna (title, body)."""
    direction = signal["direction"]
    asset_display = signal["asset"].replace("_", " ")
    quality = signal["quality_score"]
    label = signal.get("quality_label", "MEDIUM")
    triggers = signal.get("trigger_types", [])
    triggers_str = " + ".join(triggers) if triggers else "N/A"

    em_points = signal.get("expected_move_points")
    em_str = f"{em_points:.1f}pt" if em_points is not None else "N/A"

    na_label = signal.get("nearest_above_label") or "N/A"
    na_price = signal.get("nearest_above_price")
    na_prio  = signal.get("nearest_above_priority") or ""

    nb_label = signal.get("nearest_below_label") or "N/A"
    nb_price = signal.get("nearest_below_price")
    nb_prio  = signal.get("nearest_below_priority") or ""

    tgt_label = signal.get("liquidity_target") or "N/A"
    tgt_prio  = signal.get("liquidity_target_priority") or ""

    title = f"V4.1P1 {asset_display} {direction} | Q {quality}/12 ({label}) | EM {em_str}"

    body = (
        f"Entry: {_fmt(signal['entry'])}\n"
        f"Stop Loss: {_fmt(signal['stop_loss'])}\n"
        f"TP1 (1R): {_fmt(signal.get('tp1'))}\n"
        f"TP2 (2R): {_fmt(signal.get('tp2'))}\n"
        f"R/R: {signal.get('rr', 0):.2f}\n"
        f"Trigger: {triggers_str}\n"
        f"\n"
        f"Money Flow Map:\n"
        f"  Above: {na_label} @ {_fmt(na_price)} [{na_prio}]\n"
        f"  Below: {nb_label} @ {_fmt(nb_price)} [{nb_prio}]\n"
        f"  Target: {tgt_label} [{tgt_prio}]\n"
        f"  Expected Move: {em_str}\n"
        f"\n"
        f"Sessione: {signal.get('session', 'N/A')}"
    )
    return title, body


def send_v41p1_signal_alert_ntfy(ntfy_topic: str, signal: dict) -> bool:
    title, body = format_v41p1_signal_alert_plain(signal)
    return ntfy_bot.send_message(ntfy_topic, title, body)


# ============================================================
# Watchlist Alert (riusa formato V4.1 con Priority Score aggiunto)
# ============================================================

def format_v41p1_watchlist_alert(asset: str, level: dict) -> str:
    asset_display = asset.replace("_", " ")
    direction = "SELL" if level["kind"] == "high" else "BUY"
    emoji = "🟢" if direction == "BUY" else "🔴"
    pe = _priority_emoji(level.get("priority_label", ""))

    lines = [
        f"👀 *WATCHLIST — V4.1 Phase 1*",
        "",
        f"Asset: *{asset_display}*",
        "",
        f"Livello: *{level['label']}*",
        f"Prezzo: `{_fmt(level['price'])}`",
        f"Distanza: *{level['distance_pct']*100:.2f}%*",
        f"Priority: {pe} *{level.get('priority_label','N/A')}* "
        f"({level.get('priority_score', 0):.2f})",
        f"Tocchi storici (30gg): {level.get('historical_touches', 0)}",
        "",
        f"Scenario potenziale: {emoji} *{direction}*",
        "",
        "_Alert preparatorio: nessuna conferma trigger ancora presente._",
    ]
    return "\n".join(lines)


def send_v41p1_watchlist_alert(bot_token: str, chat_id: str,
                                asset: str, level: dict) -> bool:
    text = format_v41p1_watchlist_alert(asset, level)
    return send_message(bot_token, chat_id, text)
