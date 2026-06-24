"""
strategies/edge_lab/trend_rider.py
NMC Trend Rider Balanced v1.0

Strategia trend-following basata su:
    1. Trend Filter    H1: EMA20 > EMA50 (BUY) / EMA20 < EMA50 (SELL)
    2. Momentum Filter H1: ADX(14) > 20
    3. Pullback Filter H1: Low <= EMA20 (BUY) / High >= EMA20 (SELL)
                           oppure distanza da EMA20 <= 0.5 ATR H1
    4. Trigger M15:    candela bullish/bearish con Close > High prev (BUY)
                       / Close < Low prev (SELL) e Body/Range >= 0.50

Quality Score (0-100+):
    Trend H1:     40 pt (base, obbligatorio)
    Momentum:     20 pt (base, obbligatorio)
    Pullback:     20 pt (base, obbligatorio)
    Trigger:      10 pt (base, obbligatorio)
    H4 allineato: +10 pt (bonus)
    Liquidità:    +10 pt (bonus)
    Nuovo H/L 24h:+10 pt (bonus)

Classificazione:
    LOW     0-59   → nessun alert
    MEDIUM  60-74  → alert opzionale
    HIGH    75-89  → alert standard
    PREMIUM 90+    → alert prioritario

Asset: BTC_USDT, PAXG_USDT
Timeframe operativo trigger: M15
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("edge_lab.trend_rider")

STRATEGY_NAME    = "TRB"
STRATEGY_VERSION = "v1.0"

EXPIRY_BARS_M15  = 96   # 24h operative

# Quality score soglie
SCORE_LOW     = 60
SCORE_MEDIUM  = 75
SCORE_HIGH    = 90

# Parametri tecnici
EMA_SHORT    = 20
EMA_LONG     = 50
ADX_PERIOD   = 14
ADX_MIN      = 20
ADX_BONUS    = 25
BODY_MIN_PCT = 0.50
ATR_PULLBACK_MULT = 0.5
SWING_LOOKBACK = 2


# ============================================================
# Indicatori
# ============================================================

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - close).abs(),
        (low  - close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calcola ADX(period) sul DataFrame."""
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    plus_dm  = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm  < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[(plus_dm > minus_dm) == False] = 0
    minus_dm[(minus_dm >= plus_dm) == False] = 0

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr_s    = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period,  adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_s

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx


def _add_indicators_h1(df: pd.DataFrame) -> pd.DataFrame:
    """Aggiunge EMA20, EMA50, ATR14, ADX14 alle candele H1."""
    df = df.copy()
    df["ema20"] = _ema(df["close"], EMA_SHORT)
    df["ema50"] = _ema(df["close"], EMA_LONG)
    df["atr"]   = _atr(df, ADX_PERIOD)
    df["adx"]   = _adx(df, ADX_PERIOD)
    return df


def _add_indicators_h4(df: pd.DataFrame) -> pd.DataFrame:
    """Aggiunge EMA20, EMA50 alle candele H4."""
    df = df.copy()
    df["ema20"] = _ema(df["close"], EMA_SHORT)
    df["ema50"] = _ema(df["close"], EMA_LONG)
    return df


def _add_indicators_m15(df: pd.DataFrame) -> pd.DataFrame:
    """Aggiunge ATR14 alle candele M15."""
    df = df.copy()
    df["atr"] = _atr(df, ADX_PERIOD)
    return df


# ============================================================
# Trend Filter H1
# ============================================================

def _get_trend_h1(df_h1: pd.DataFrame) -> Optional[str]:
    """
    BULLISH: EMA20 > EMA50
    BEARISH: EMA20 < EMA50
    None:    incrocio recente o distanza minima
    """
    if len(df_h1) < EMA_LONG + 5:
        return None

    last = df_h1.iloc[-1]
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])

    # Distanza minima: < 0.05% del prezzo → nessun trend valido
    price = float(last["close"])
    if price > 0 and abs(ema20 - ema50) / price < 0.0005:
        return None

    # Incrocio recente (ultime 3 candele)
    for i in range(-3, -1):
        try:
            prev_ema20 = float(df_h1.iloc[i]["ema20"])
            prev_ema50 = float(df_h1.iloc[i]["ema50"])
            # Se il segno era diverso → incrocio recente
            if (ema20 - ema50) * (prev_ema20 - prev_ema50) < 0:
                return None
        except Exception:
            pass

    if ema20 > ema50:
        return "BULLISH"
    if ema20 < ema50:
        return "BEARISH"
    return None


def _get_trend_h4(df_h4: pd.DataFrame) -> Optional[str]:
    """Trend H4: solo per bonus quality, non bloccante."""
    if len(df_h4) < EMA_LONG + 5:
        return None
    last = df_h4.iloc[-1]
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    if ema20 > ema50:
        return "BULLISH"
    if ema20 < ema50:
        return "BEARISH"
    return None


# ============================================================
# Momentum Filter H1 (ADX)
# ============================================================

def _get_adx(df_h1: pd.DataFrame) -> float:
    if "adx" not in df_h1.columns or len(df_h1) < ADX_PERIOD + 5:
        return 0.0
    return float(df_h1.iloc[-1]["adx"])


# ============================================================
# Pullback Filter H1
# ============================================================

def _check_pullback(df_h1: pd.DataFrame, direction: str) -> bool:
    """
    BUY:  Low <= EMA20 oppure distanza da EMA20 <= 0.5 ATR H1
    SELL: High >= EMA20 oppure distanza da EMA20 <= 0.5 ATR H1
    """
    if len(df_h1) < 2:
        return False

    last  = df_h1.iloc[-1]
    ema20 = float(last["ema20"])
    atr   = float(last["atr"]) if "atr" in df_h1.columns else 0.0
    price = float(last["close"])

    dist = abs(price - ema20)

    if direction == "BUY":
        touch = float(last["low"]) <= ema20
        close = atr > 0 and dist <= ATR_PULLBACK_MULT * atr
        return touch or close
    else:
        touch = float(last["high"]) >= ema20
        close = atr > 0 and dist <= ATR_PULLBACK_MULT * atr
        return touch or close


# ============================================================
# Trigger M15
# ============================================================

def _find_trigger_candle(
    df_m15: pd.DataFrame,
    direction: str,
    lookback: int = 5,
) -> Optional[int]:
    """
    Cerca la candela trigger più recente nelle ultime `lookback` candele chiuse.

    BUY:  candela bullish + Close > High candela precedente + Body/Range >= 0.50
    SELL: candela bearish + Close < Low  candela precedente + Body/Range >= 0.50

    Ritorna l'indice nel DataFrame oppure None.
    """
    if len(df_m15) < 3:
        return None

    end   = len(df_m15) - 1   # ultima candela (potenzialmente aperta)
    start = max(1, end - lookback)

    for i in range(end - 1, start - 1, -1):
        candle = df_m15.iloc[i]
        prev   = df_m15.iloc[i - 1]

        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        c = float(candle["close"])

        body = abs(c - o)
        rng  = h - l

        if rng <= 0:
            continue

        body_pct = body / rng
        if body_pct < BODY_MIN_PCT:
            continue

        prev_high = float(prev["high"])
        prev_low  = float(prev["low"])

        if direction == "BUY":
            if c > o and c > prev_high:
                return i
        else:
            if c < o and c < prev_low:
                return i

    return None


# ============================================================
# Stop Loss: Swing Low/High M15
# ============================================================

def _find_swing_sl(
    df_m15: pd.DataFrame,
    direction: str,
    trigger_idx: int,
    lookback: int = 10,
) -> Optional[float]:
    """
    BUY:  ultimo Swing Low M15 (pivot low con SWING_LOOKBACK candele)
    SELL: ultimo Swing High M15
    """
    start = max(0, trigger_idx - lookback)
    window = df_m15.iloc[start:trigger_idx]

    if len(window) < SWING_LOOKBACK * 2 + 1:
        # Fallback: estremo della finestra
        if direction == "BUY":
            return float(window["low"].min()) if len(window) > 0 else None
        else:
            return float(window["high"].max()) if len(window) > 0 else None

    lows  = window["low"].values
    highs = window["high"].values
    n     = len(lows)

    if direction == "BUY":
        # Cerca ultimo pivot low
        for i in range(n - SWING_LOOKBACK - 1, SWING_LOOKBACK - 1, -1):
            if all(lows[i] <= lows[i - j] for j in range(1, SWING_LOOKBACK + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, SWING_LOOKBACK + 1)):
                return float(lows[i])
        return float(lows.min())
    else:
        # Cerca ultimo pivot high
        for i in range(n - SWING_LOOKBACK - 1, SWING_LOOKBACK - 1, -1):
            if all(highs[i] >= highs[i - j] for j in range(1, SWING_LOOKBACK + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, SWING_LOOKBACK + 1)):
                return float(highs[i])
        return float(highs.max())


# ============================================================
# Volatility Filter (solo PAXG)
# ============================================================

def _check_volatility_paxg(df_m15: pd.DataFrame) -> bool:
    """
    PAXG/XAUUSD: ATR M15 corrente > media ATR ultime 20 candele.
    BTC: sempre True (nessun filtro).
    """
    if "atr" not in df_m15.columns or len(df_m15) < 21:
        return True  # dati insufficienti → non bloccare
    current_atr = float(df_m15.iloc[-1]["atr"])
    avg_atr     = float(df_m15.iloc[-20:]["atr"].mean())
    return current_atr > avg_atr


# ============================================================
# Liquidity Context (da Money Flow Map / Liquidity Engine)
# ============================================================

def _find_liquidity_target(liq_map: Optional[dict], direction: str) -> Optional[dict]:
    """
    Cerca il primo livello di liquidità rilevante nella direzione del trade.
    Usa la stessa liq_map del Market Context Engine (già calcolata).
    """
    if liq_map is None:
        return None

    current_price = liq_map.get("current_price", 0)
    levels = liq_map.get("levels", [])

    if direction == "BUY":
        candidates = [
            lv for lv in levels
            if lv["kind"] == "high" and lv["price"] > current_price
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda lv: lv["price"])
    else:
        candidates = [
            lv for lv in levels
            if lv["kind"] == "low" and lv["price"] < current_price
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda lv: lv["price"])


def _check_new_24h_extreme(df_m15: pd.DataFrame, direction: str) -> bool:
    """
    BUY:  prezzo attuale > massimo delle ultime 96 candele M15 (24h)
    SELL: prezzo attuale < minimo delle ultime 96 candele M15
    """
    if len(df_m15) < 10:
        return False

    lookback = min(96, len(df_m15) - 1)
    window   = df_m15.iloc[-lookback - 1:-1]
    current  = df_m15.iloc[-1]

    if direction == "BUY":
        return float(current["high"]) > float(window["high"].max())
    else:
        return float(current["low"]) < float(window["low"].min())


# ============================================================
# Quality Score
# ============================================================

def _compute_quality(
    trend_h4: Optional[str],
    trend_h1: str,
    adx: float,
    pullback_ok: bool,
    trigger_idx: Optional[int],
    liq_target: Optional[dict],
    new_extreme: bool,
    direction: str,
) -> tuple[int, str]:
    """
    Calcola Quality Score e label.

    Base (obbligatori — già verificati come gate):
        Trend H1:  40 pt
        Momentum:  20 pt (ADX > 20)
        Pullback:  20 pt
        Trigger:   10 pt
    Bonus:
        H4 allineato: +10
        ADX > 25:     +10 (parte del momentum bonus)
        Liquidità:    +10
        Nuovo H/L 24h:+10
    """
    score = 0

    # Base — sempre presenti se arriviamo qui
    score += 40  # Trend H1
    score += 20  # Momentum (ADX > 20)
    score += 20  # Pullback
    score += 10  # Trigger

    # Bonus H4
    if trend_h4 is not None:
        if direction == "BUY"  and trend_h4 == "BULLISH":
            score += 10
        elif direction == "SELL" and trend_h4 == "BEARISH":
            score += 10

    # Bonus ADX > 25
    if adx > ADX_BONUS:
        score += 10

    # Bonus liquidità
    if liq_target is not None:
        score += 10

    # Bonus nuovo estremo 24h
    if new_extreme:
        score += 10

    score = min(score, 120)  # cap generoso per evitare overflow

    if score >= SCORE_HIGH:
        label = "PREMIUM" if score >= SCORE_HIGH + 15 else "HIGH"
        if score >= 90:
            label = "PREMIUM"
        else:
            label = "HIGH"
    elif score >= SCORE_LOW + 15:
        label = "MEDIUM"
    elif score >= SCORE_LOW:
        label = "MEDIUM"
    else:
        label = "LOW"

    # Ricalcolo pulito label
    if score >= 90:
        label = "PREMIUM"
    elif score >= 75:
        label = "HIGH"
    elif score >= 60:
        label = "MEDIUM"
    else:
        label = "LOW"

    return score, label


# ============================================================
# Entry point principale
# ============================================================

def generate_trb_signal(
    market_ctx: dict,
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m15: pd.DataFrame,
    direction: str,
) -> dict:
    """
    Genera un segnale NMC Trend Rider Balanced v1.0.

    Args:
        market_ctx: output Market Context Engine (per liq_map e sessione)
        df_h4:      candele H4
        df_h1:      candele H1
        df_m15:     candele M15
        direction:  "BUY" | "SELL"

    Returns:
        {"signal": dict | None, "diagnostics": dict}
    """
    asset = market_ctx.get("asset", "UNKNOWN")

    diag: dict = {
        "strategy":  STRATEGY_NAME,
        "direction": direction,
        "asset":     asset,
        "rejection": None,
        "conditions": {},
    }

    def reject(reason: str) -> dict:
        diag["rejection"] = reason
        logger.info("TRB [%s %s]: REJECT %s", asset, direction, reason)
        return {"signal": None, "diagnostics": diag}

    # ── Preparazione indicatori ──────────────────────────────
    if len(df_h1) < EMA_LONG + ADX_PERIOD + 5:
        return reject("INSUFFICIENT_H1_DATA")
    if len(df_m15) < 10:
        return reject("INSUFFICIENT_M15_DATA")

    df_h1  = _add_indicators_h1(df_h1)
    df_h4  = _add_indicators_h4(df_h4) if len(df_h4) >= EMA_LONG + 5 else df_h4
    df_m15 = _add_indicators_m15(df_m15)

    # ── Gate 1: Trend H1 ────────────────────────────────────
    trend_h1 = _get_trend_h1(df_h1)
    diag["conditions"]["trend_h1"] = trend_h1

    if trend_h1 is None:
        return reject("NO_H1_TREND (EMA20/EMA50 incrocio recente o distanza minima)")

    if direction == "BUY"  and trend_h1 != "BULLISH":
        return reject(f"TREND_NOT_ALIGNED (H1={trend_h1})")
    if direction == "SELL" and trend_h1 != "BEARISH":
        return reject(f"TREND_NOT_ALIGNED (H1={trend_h1})")

    # ── Gate 2: Momentum ADX ────────────────────────────────
    adx = _get_adx(df_h1)
    diag["conditions"]["adx"] = round(adx, 2)

    if adx < ADX_MIN:
        return reject(f"ADX_TOO_LOW ({adx:.1f} < {ADX_MIN})")

    # ── Gate 3: Pullback ────────────────────────────────────
    pullback_ok = _check_pullback(df_h1, direction)
    diag["conditions"]["pullback"] = pullback_ok

    if not pullback_ok:
        return reject("NO_PULLBACK (prezzo lontano da EMA20 H1)")

    # ── Gate 4: Volatility (solo PAXG) ──────────────────────
    if "PAXG" in asset:
        vol_ok = _check_volatility_paxg(df_m15)
        diag["conditions"]["volatility_ok"] = vol_ok
        if not vol_ok:
            return reject("PAXG_VOLATILITY_TOO_LOW (ATR M15 < media 20 candele)")
    else:
        diag["conditions"]["volatility_ok"] = True

    # ── Gate 5: Trigger M15 ─────────────────────────────────
    trigger_idx = _find_trigger_candle(df_m15, direction, lookback=5)
    diag["conditions"]["trigger"] = trigger_idx is not None

    if trigger_idx is None:
        return reject("NO_TRIGGER (nessuna candela M15 valida)")

    trigger_candle = df_m15.iloc[trigger_idx]
    entry = float(trigger_candle["close"])
    diag["entry"] = entry

    # ── Stop Loss ────────────────────────────────────────────
    sl = _find_swing_sl(df_m15, direction, trigger_idx, lookback=15)
    if sl is None:
        return reject("NO_SWING_SL")

    # Validità SL
    if direction == "BUY"  and sl >= entry:
        return reject(f"SL_INVALID_BUY (sl={sl:.4f} >= entry={entry:.4f})")
    if direction == "SELL" and sl <= entry:
        return reject(f"SL_INVALID_SELL (sl={sl:.4f} <= entry={entry:.4f})")

    risk = abs(entry - sl)
    if risk <= 0:
        return reject("RISK_ZERO")

    diag["sl"] = sl

    # ── Take Profit ──────────────────────────────────────────
    # TP1 = 1R
    if direction == "BUY":
        tp1 = entry + risk
    else:
        tp1 = entry - risk

    # TP2 = primo livello liquidità rilevante
    liq_map    = market_ctx.get("liquidity")
    liq_target = _find_liquidity_target(liq_map, direction)
    tp2        = float(liq_target["price"]) if liq_target else tp1 * 2 - entry

    diag["tp1"] = tp1
    diag["tp2"] = tp2

    # ── Contesto per analytics ───────────────────────────────
    trend_h4    = _get_trend_h4(df_h4) if len(df_h4) >= EMA_LONG + 5 else None
    new_extreme = _check_new_24h_extreme(df_m15, direction)
    sess_ctx    = market_ctx.get("session") or {}
    session     = sess_ctx.get("current_session", "UNKNOWN")

    diag["trend_h4"]    = trend_h4
    diag["new_extreme"] = new_extreme
    diag["session"]     = session

    # ── Quality Score ────────────────────────────────────────
    quality_score, quality_label = _compute_quality(
        trend_h4, trend_h1, adx, pullback_ok,
        trigger_idx, liq_target, new_extreme, direction,
    )

    diag["quality_score"] = quality_score
    diag["quality_label"] = quality_label

    # LOW → nessun segnale
    if quality_label == "LOW":
        return reject(f"QUALITY_TOO_LOW (score={quality_score})")

    # ── ATR M15 per analytics ────────────────────────────────
    atr_m15 = float(df_m15.iloc[-1]["atr"]) if "atr" in df_m15.columns else 0.0
    atr_h1  = float(df_h1.iloc[-1]["atr"])  if "atr" in df_h1.columns  else 0.0

    # ── Timestamp setup ──────────────────────────────────────
    try:
        conf_ts_ms = int(trigger_candle["timestamp"])
        conf_dt    = datetime.fromtimestamp(conf_ts_ms / 1000, tz=timezone.utc)
    except Exception:
        conf_dt = datetime.now(timezone.utc)

    # ── Costruzione segnale ───────────────────────────────────
    signal = {
        "signal_id":        str(uuid.uuid4()),
        "strategy_name":    STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "asset":            asset,
        "direction":        direction,
        "timestamp_setup":  conf_dt.isoformat(),

        # Prezzi
        "entry":     entry,
        "stop_loss": sl,
        "tp1":       tp1,
        "tp2":       tp2,
        "risk":      risk,
        "rr1":       1.0,
        "rr2":       round(abs(tp2 - entry) / risk, 2) if risk > 0 else 0,

        # Contesto
        "trend_h1":         trend_h1,
        "trend_h4":         trend_h4,
        "adx":              round(adx, 2),
        "atr_m15":          round(atr_m15, 4),
        "atr_h1":           round(atr_h1,  4),
        "pullback_valid":   pullback_ok,
        "new_24h_extreme":  new_extreme,
        "session":          session,

        # Liquidità
        "liquidity_target":       liq_target.get("label")  if liq_target else None,
        "liquidity_target_price": liq_target.get("price")  if liq_target else None,
        "liquidity_priority":     liq_target.get("priority_label") if liq_target else None,

        # Quality
        "quality_score": quality_score,
        "quality_label": quality_label,

        # Tracking
        "final_outcome": "OPEN",
        "expiry_bars":   EXPIRY_BARS_M15,
    }

    logger.info(
        "TRB [%s %s]: SIGNAL entry=%.4f sl=%.4f tp1=%.4f tp2=%.4f "
        "score=%d (%s) adx=%.1f session=%s target=%s",
        asset, direction, entry, sl, tp1, tp2,
        quality_score, quality_label, adx, session,
        liq_target.get("label") if liq_target else "N/A",
    )

    return {"signal": signal, "diagnostics": diag}
