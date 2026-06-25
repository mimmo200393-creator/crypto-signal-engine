"""
strategies/edge_lab/trend_rider.py
NMC Trend Rider Balanced v1.0
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

EXPIRY_BARS_M15  = 96

SCORE_LOW     = 60
SCORE_MEDIUM  = 75
SCORE_HIGH    = 90

EMA_SHORT         = 20
EMA_LONG          = 50
ADX_PERIOD        = 14
ADX_MIN           = 20
ADX_BONUS         = 25
BODY_MIN_PCT      = 0.50
ATR_PULLBACK_MULT = 0.5
SWING_LOOKBACK    = 2
MIN_SL_ATR_MULT   = 1.5   # SL minimo = 1.5x ATR M15


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
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    plus_dm  = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm   < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[(plus_dm > minus_dm) == False]    = 0
    minus_dm[(minus_dm >= plus_dm) == False]  = 0

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr_s    = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period,  adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_s

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx


def _add_indicators_h1(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"] = _ema(df["close"], EMA_SHORT)
    df["ema50"] = _ema(df["close"], EMA_LONG)
    df["atr"]   = _atr(df, ADX_PERIOD)
    df["adx"]   = _adx(df, ADX_PERIOD)
    return df


def _add_indicators_h4(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"] = _ema(df["close"], EMA_SHORT)
    df["ema50"] = _ema(df["close"], EMA_LONG)
    return df


def _add_indicators_m15(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["atr"] = _atr(df, ADX_PERIOD)
    return df


# ============================================================
# Trend Filter H1
# ============================================================

def _get_trend_h1(df_h1: pd.DataFrame) -> Optional[str]:
    if len(df_h1) < EMA_LONG + 5:
        return None

    last  = df_h1.iloc[-1]
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    price = float(last["close"])

    if price > 0 and abs(ema20 - ema50) / price < 0.0005:
        return None

    for i in range(-3, -1):
        try:
            prev_ema20 = float(df_h1.iloc[i]["ema20"])
            prev_ema50 = float(df_h1.iloc[i]["ema50"])
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
    if len(df_h4) < EMA_LONG + 5:
        return None
    last  = df_h4.iloc[-1]
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    if ema20 > ema50: return "BULLISH"
    if ema20 < ema50: return "BEARISH"
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
    if len(df_h1) < 2:
        return False

    last  = df_h1.iloc[-1]
    ema20 = float(last["ema20"])
    atr   = float(last["atr"]) if "atr" in df_h1.columns else 0.0
    price = float(last["close"])
    dist  = abs(price - ema20)

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
    if len(df_m15) < 3:
        return None

    end   = len(df_m15) - 1
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

        if body / rng < BODY_MIN_PCT:
            continue

        if direction == "BUY":
            if c > o and c > float(prev["high"]):
                return i
        else:
            if c < o and c < float(prev["low"]):
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
    start  = max(0, trigger_idx - lookback)
    window = df_m15.iloc[start:trigger_idx]

    if len(window) < SWING_LOOKBACK * 2 + 1:
        if direction == "BUY":
            return float(window["low"].min())  if len(window) > 0 else None
        else:
            return float(window["high"].max()) if len(window) > 0 else None

    lows  = window["low"].values
    highs = window["high"].values
    n     = len(lows)

    if direction == "BUY":
        for i in range(n - SWING_LOOKBACK - 1, SWING_LOOKBACK - 1, -1):
            if all(lows[i] <= lows[i - j] for j in range(1, SWING_LOOKBACK + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, SWING_LOOKBACK + 1)):
                return float(lows[i])
        return float(lows.min())
    else:
        for i in range(n - SWING_LOOKBACK - 1, SWING_LOOKBACK - 1, -1):
            if all(highs[i] >= highs[i - j] for j in range(1, SWING_LOOKBACK + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, SWING_LOOKBACK + 1)):
                return float(highs[i])
        return float(highs.max())


# ============================================================
# Volatility Filter (solo PAXG)
# ============================================================

def _check_volatility_paxg(df_m15: pd.DataFrame) -> bool:
    if "atr" not in df_m15.columns or len(df_m15) < 21:
        return True
    current_atr = float(df_m15.iloc[-1]["atr"])
    avg_atr     = float(df_m15.iloc[-20:]["atr"].mean())
    return current_atr > avg_atr


# ============================================================
# Liquidity Context
# ============================================================

def _find_liquidity_target(liq_map: Optional[dict], direction: str) -> Optional[dict]:
    if liq_map is None:
        return None

    current_price = liq_map.get("current_price", 0)
    levels        = liq_map.get("levels", [])

    if direction == "BUY":
        candidates = [lv for lv in levels if lv["kind"] == "high" and lv["price"] > current_price]
        return min(candidates, key=lambda lv: lv["price"]) if candidates else None
    else:
        candidates = [lv for lv in levels if lv["kind"] == "low" and lv["price"] < current_price]
        return max(candidates, key=lambda lv: lv["price"]) if candidates else None


def _check_new_24h_extreme(df_m15: pd.DataFrame, direction: str) -> bool:
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
    score = 90  # base obbligatoria: trend(40) + momentum(20) + pullback(20) + trigger(10)

    if trend_h4 is not None:
        if direction == "BUY"  and trend_h4 == "BULLISH": score += 10
        elif direction == "SELL" and trend_h4 == "BEARISH": score += 10

    if adx > ADX_BONUS:
        score += 10

    if liq_target is not None:
        score += 10

    if new_extreme:
        score += 10

    score = min(score, 120)

    if score >= 90:   label = "PREMIUM"
    elif score >= 75: label = "HIGH"
    elif score >= 60: label = "MEDIUM"
    else:             label = "LOW"

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
    asset = market_ctx.get("asset", "UNKNOWN")

    diag: dict = {
        "strategy":   STRATEGY_NAME,
        "direction":  direction,
        "asset":      asset,
        "rejection":  None,
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

    # ATR M15 calcolato subito — serve per SL minimo
    atr_m15 = float(df_m15.iloc[-1]["atr"]) if "atr" in df_m15.columns else 0.0
    atr_h1  = float(df_h1.iloc[-1]["atr"])  if "atr" in df_h1.columns  else 0.0

    # ── Gate 1: Trend H1 ────────────────────────────────────
    trend_h1 = _get_trend_h1(df_h1)
    diag["conditions"]["trend_h1"] = trend_h1

    if trend_h1 is None:
        return reject("NO_H1_TREND")
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
        return reject("NO_PULLBACK")

    # ── Gate 4: Volatility (solo PAXG) ──────────────────────
    if "PAXG" in asset:
        vol_ok = _check_volatility_paxg(df_m15)
        diag["conditions"]["volatility_ok"] = vol_ok
        if not vol_ok:
            return reject("PAXG_VOLATILITY_TOO_LOW")
    else:
        diag["conditions"]["volatility_ok"] = True

    # ── Gate 5: Trigger M15 ─────────────────────────────────
    trigger_idx = _find_trigger_candle(df_m15, direction, lookback=5)
    diag["conditions"]["trigger"] = trigger_idx is not None

    if trigger_idx is None:
        return reject("NO_TRIGGER")

    trigger_candle = df_m15.iloc[trigger_idx]
    entry = float(trigger_candle["close"])
    diag["entry"] = entry

    # ── Stop Loss ────────────────────────────────────────────
    sl = _find_swing_sl(df_m15, direction, trigger_idx, lookback=15)
    if sl is None:
        return reject("NO_SWING_SL")

    if direction == "BUY"  and sl >= entry:
        return reject(f"SL_INVALID_BUY (sl={sl:.4f} >= entry={entry:.4f})")
    if direction == "SELL" and sl <= entry:
        return reject(f"SL_INVALID_SELL (sl={sl:.4f} <= entry={entry:.4f})")

    risk = abs(entry - sl)
    if risk <= 0:
        return reject("RISK_ZERO")

    # ── SL minimo: 1.5x ATR M15 ─────────────────────────────
    if atr_m15 > 0 and risk < MIN_SL_ATR_MULT * atr_m15:
        return reject(
            f"SL_TOO_CLOSE (risk={risk:.4f} < {MIN_SL_ATR_MULT}x ATR={atr_m15:.4f})"
        )

    diag["sl"] = sl

    # ── Take Profit ──────────────────────────────────────────
    if direction == "BUY":
        tp1 = entry + risk
    else:
        tp1 = entry - risk

    liq_map    = market_ctx.get("liquidity")
    liq_target = _find_liquidity_target(liq_map, direction)
    tp2_raw    = float(liq_target["price"]) if liq_target else None

    # TP2 deve essere più lontano dall'entry rispetto a TP1
    # BUY:  tp2 > tp1 > entry
    # SELL: tp2 < tp1 < entry
    if tp2_raw is not None:
        if direction == "BUY" and tp2_raw > tp1:
            tp2 = tp2_raw
        elif direction == "SELL" and tp2_raw < tp1:
            tp2 = tp2_raw
        else:
            # Target di liquidità troppo vicino — fallback 2R
            tp2 = tp1 + risk if direction == "BUY" else tp1 - risk
            liq_target = None  # non valido, non conta come bonus
    else:
        tp2 = tp1 + risk if direction == "BUY" else tp1 - risk

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

    if quality_label == "LOW":
        return reject(f"QUALITY_TOO_LOW (score={quality_score})")

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
        "entry":            entry,
        "stop_loss":        sl,
        "tp1":              tp1,
        "tp2":              tp2,
        "risk":             risk,
        "rr1":              1.0,
        "rr2":              round(abs(tp2 - entry) / risk, 2) if risk > 0 else 0,
        "trend_h1":         trend_h1,
        "trend_h4":         trend_h4,
        "adx":              round(adx, 2),
        "atr_m15":          round(atr_m15, 4),
        "atr_h1":           round(atr_h1,  4),
        "pullback_valid":   pullback_ok,
        "new_24h_extreme":  new_extreme,
        "session":          session,
        "liquidity_target":       liq_target.get("label")         if liq_target else None,
        "liquidity_target_price": liq_target.get("price")         if liq_target else None,
        "liquidity_priority":     liq_target.get("priority_label") if liq_target else None,
        "quality_score":    quality_score,
        "quality_label":    quality_label,
        "final_outcome":    "OPEN",
        "expiry_bars":      EXPIRY_BARS_M15,
    }

    logger.info(
        "TRB [%s %s]: SIGNAL entry=%.4f sl=%.4f tp1=%.4f tp2=%.4f "
        "risk=%.4f score=%d (%s) adx=%.1f session=%s target=%s",
        asset, direction, entry, sl, tp1, tp2, risk,
        quality_score, quality_label, adx, session,
        liq_target.get("label") if liq_target else "N/A",
    )

    return {"signal": signal, "diagnostics": diag}
