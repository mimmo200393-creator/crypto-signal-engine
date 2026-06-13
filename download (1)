"""
scoring.py
Sistema di scoring per i setup Pullback EMA Trend Strategy V1.0 (FROZEN).

Punteggi (da spec, NON modificabili senza incremento STRATEGY_VERSION):
    Trend H4 favorevole     -> +3
    Trend H1 favorevole     -> +2
    Pullback su EMA50       -> +2
    Pullback su EMA21       -> +1
    Supporto/Resistenza     -> +1
    Trigger confermato      -> +1
    -----------------------------
    Score massimo           = 10
"""

SCORE_TREND_H4 = 3
SCORE_TREND_H1 = 2
SCORE_PULLBACK_EMA50 = 2
SCORE_PULLBACK_EMA21 = 1
SCORE_SR_LEVEL = 1
SCORE_TRIGGER = 1

SCORE_MAX = 10


def compute_score(setup: dict) -> int:
    """
    Calcola lo score totale di un setup.

    `setup` deve contenere i seguenti flag booleani:
        - trend_h4_ok: bool
        - trend_h1_ok: bool
        - pullback_ema50: bool   (pullback avvenuto sull'EMA50)
        - pullback_ema21: bool   (pullback avvenuto sull'EMA21)
        - sr_level_present: bool (S/R rilevante vicino all'entry)
        - trigger_confirmed: bool

    Nota: pullback_ema50 e pullback_ema21 NON sono mutuamente esclusivi
    a livello di rilevamento (il prezzo potrebbe essere vicino a entrambe
    le EMA), ma la spec assegna punteggi distinti per ciascuna condizione
    verificata. Entrambi i flag possono quindi contribuire allo score.
    """
    score = 0

    if setup.get("trend_h4_ok"):
        score += SCORE_TREND_H4
    if setup.get("trend_h1_ok"):
        score += SCORE_TREND_H1
    if setup.get("pullback_ema50"):
        score += SCORE_PULLBACK_EMA50
    if setup.get("pullback_ema21"):
        score += SCORE_PULLBACK_EMA21
    if setup.get("sr_level_present"):
        score += SCORE_SR_LEVEL
    if setup.get("trigger_confirmed"):
        score += SCORE_TRIGGER

    return min(score, SCORE_MAX)


def classify_score(score: int, db_threshold: int, telegram_threshold: int) -> dict:
    """
    Classifica il setup in base allo score:
        score < db_threshold        -> ignorato (non salvato)
        db_threshold <= score < telegram_threshold -> solo DB
        score == telegram_threshold -> 🔥 High Quality Setup
        score == SCORE_MAX          -> ⭐ Elite Setup

    Ritorna dict con:
        - save_to_db: bool
        - send_telegram: bool
        - label: str (descrizione/emoji)
    """
    if score < db_threshold:
        return {"save_to_db": False, "send_telegram": False, "label": None}

    if score < telegram_threshold:
        return {"save_to_db": True, "send_telegram": False, "label": "DB only"}

    if score >= SCORE_MAX:
        return {"save_to_db": True, "send_telegram": True, "label": "⭐ Elite Setup"}

    return {"save_to_db": True, "send_telegram": True, "label": "🔥 High Quality Setup"}
