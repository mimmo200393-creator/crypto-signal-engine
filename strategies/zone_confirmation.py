"""
strategies/zone_confirmation.py
Zone + Confirmation Strategy V1.0 — FROZEN

Filosofia: Non inseguire il prezzo. Aspettare pazientemente che il mercato
raggiunga aree di valore H4 e dimostri concretamente l'intenzione di reagire.

Pipeline:
    1. Identifica zone H4 significative (supporti/resistenze con >= 2 tocchi)
    2. Verifica che il prezzo sia nella zona (entro 0.5% del livello)
    3. Attende un pattern di conferma H1 (Rejection / Sweep / Pullback EMA)
    4. Valida R/R >= 2, bias H4, macro risk
    5. Calcola score 0-11 e genera segnale

Versione: V1.0 — NON modificare fino a 100 trade chiusi
"""

from typing import Optional, Tuple
from datetime import datetime, timezone

from strategies.base import BaseStrategy, Signal
from core.indicators import find_pivots, cluster_levels, nearest_level


class ZoneConfirmation(BaseStrategy):

    name = "Zone + Confirmation"
    version = "V1.0"

    ZONE_LOOKBACK_H4 = 20
    ZONE_MIN_TOUCHES = 2
    ZONE_CLUSTER_ATR = 0.5
    ZONE_PROXIMITY_PCT = 0.005

    WICK_BODY_RATIO = 2.0
    PULLBACK_ATR_FRACTION = 0.3
    VOLUME_LOOKBACK = 20

    ATR_MULTIPLIER = 1.5
    MIN_RR = 2.0

    DB_SCORE_THRESHOLD = 6
    TELEGRAM_SCORE_THRESHOLD = 7
    ELITE_SCORE_THRESHOLD = 9

    def _get_bias(self, df_h4) -> str:
        last = df_h4.iloc[-1]
        e50, e100, e200 = last["ema_50"], last["ema_100"], last["ema_200"]
        if e50 > e100 > e200:
            return "RIALZISTA"
        if e50 < e100 < e200:
            return "RIBASSISTA"
        return "NEUTRALE"

    def _get_session(self, dt: datetime) -> str:
        h = dt.hour
        if 8 <= h < 10:
            return "LONDON"
        if 13 <= h < 15:
            return "NEW_YORK"
        if 1 <= h < 4:
            return "ASIA"
        return "OFF_HOURS"

    def _get_momentum(self, df_h1, direction: str) -> str:
        if len(df_h1) < 4:
            return "UNKNOWN"
        c1 = float(df_h1.iloc[-1]["close"])
        c3 = float(df_h1.iloc[-3]["close"])
        if direction == "LONG":
            return "DOWN" if c1 < c3 else "UP"
        else:
            return "UP" if c1 > c3 else "DOWN"

    def _get_macro_risk(self, macro_event) -> str:
        if macro_event is None:
            return "LOW"
        mtr = macro_event.get("minutes_to_release", 999)
        if abs(mtr) <= 30:
            return "HIGH"
        if abs(mtr) <= 120:
            return "MEDIUM"
        return "LOW"

    def _atr_daily(self, df_h4) -> float:
        if len(df_h4) < 6:
            return 0.0
        highs = df_h4["high"].values[-6:]
        lows  = df_h4["low"].values[-6:]
        return float((highs - lows).mean())

    def _get_zones(self, df_h4, direction: str, atr_h4: float) -> list:
        lookback = df_h4.iloc[-self.ZONE_LOOKBACK_H4:].copy().reset_index(drop=True)
        pivots = find_pivots(lookback, lookback=3)

        if direction == "LONG":
            raw = pivots["pivot_lows"]
        else:
            raw = pivots["pivot_highs"]

        clusters = cluster_levels(raw, atr_h4, self.ZONE_CLUSTER_ATR)
        zones = [c for c in clusters if c["count"] >= self.ZONE_MIN_TOUCHES]

        last_h4 = df_h4.iloc[-1]
        zones.append({"price": float(last_h4["ema_200"]), "count": 3, "type": "EMA200"})

        if direction == "SHORT":
            price = float(df_h4.iloc[-1]["close"])
            if price < float(last_h4["ema_50"]):
                zones.append({"price": float(last_h4["ema_50"]), "count": 2, "type": "EMA50"})
            if price < float(last_h4["ema_100"]):
                zones.append({"price": float(last_h4["ema_100"]), "count": 2, "type": "EMA100"})

        return zones

    def _price_in_zone(self, price: float, zone_price: float) -> bool:
        if zone_price == 0:
            return False
        return abs(price - zone_price) / zone_price <= self.ZONE_PROXIMITY_PCT

    def _pattern_rejection(self, df_h1, direction: str) -> bool:
        c_rejection = df_h1.iloc[-2]
        c_trigger   = df_h1.iloc[-1]

        o = float(c_rejection["open"])
        h = float(c_rejection["high"])
        l = float(c_rejection["low"])
        c = float(c_rejection["close"])
        body = abs(c - o)

        if body == 0:
            wick_ok = True
        elif direction == "LONG":
            wick = min(o, c) - l
            wick_ok = wick >= self.WICK_BODY_RATIO * body
        else:
            wick = h - max(o, c)
            wick_ok = wick >= self.WICK_BODY_RATIO * body

        if not wick_ok:
            return False

        if direction == "LONG":
            return float(c_trigger["close"]) > float(c_rejection["high"])
        else:
            return float(c_trigger["close"]) < float(c_rejection["low"])

    def _pattern_sweep(self, df_h1, zone_price: float, direction: str) -> bool:
        c_sweep   = df_h1.iloc[-2]
        c_trigger = df_h1.iloc[-1]

        o = float(c_sweep["open"])
        h = float(c_sweep["high"])
        l = float(c_sweep["low"])
        c = float(c_sweep["close"])

        if direction == "LONG":
            sweep_ok = l < zone_price < c
        else:
            sweep_ok = c < zone_price < h

        if not sweep_ok:
            return False

        body = abs(c - o)
        if body == 0:
            wick_ok = True
        elif direction == "LONG":
            wick = min(o, c) - l
            wick_ok = wick >= self.WICK_BODY_RATIO * body
        else:
            wick = h - max(o, c)
            wick_ok = wick >= self.WICK_BODY_RATIO * body

        if not wick_ok:
            return False

        if len(df_h1) >= self.VOLUME_LOOKBACK + 2:
            avg_vol = df_h1["volume"].iloc[-(self.VOLUME_LOOKBACK + 2):-2].mean()
            vol_ok = float(c_sweep["volume"]) > avg_vol
        else:
            vol_ok = True

        if not vol_ok:
            return False

        if direction == "LONG":
            return float(c_trigger["close"]) > float(c_sweep["high"])
        else:
            return float(c_trigger["close"]) < float(c_sweep["low"])

    def _pattern_pullback_ema(self, df_h1, direction: str) -> bool:
        last = df_h1.iloc[-1]
        prev = df_h1.iloc[-2]

        ema21 = float(last["ema_21"])
        ema50 = float(last["ema_50"])
        close = float(last["close"])
        atr   = float(last["atr"])

        if direction == "LONG":
            trend_h1 = ema21 > ema50
        else:
            trend_h1 = ema21 < ema50

        if not trend_h1:
            return False

        dist_ema21 = abs(close - ema21)
        dist_ema50 = abs(close - ema50)

        if direction == "LONG":
            wick_ema21 = float(last["low"]) <= ema21
            wick_ema50 = float(last["low"]) <= ema50
        else:
            wick_ema21 = float(last["high"]) >= ema21
            wick_ema50 = float(last["high"]) >= ema50

        pullback_ok = (
            (dist_ema21 <= self.PULLBACK_ATR_FRACTION * atr) or wick_ema21 or
            (dist_ema50 <= self.PULLBACK_ATR_FRACTION * atr) or wick_ema50
        )

        if not pullback_ok:
            return False

        if direction == "LONG":
            return float(last["close"]) > float(prev["high"])
        else:
            return float(last["close"]) < float(prev["low"])

    def _score(self, zone_touches: int, bias_aligned: bool, pattern_valid: bool,
               rr: float, macro_risk: str, volume_confirmed: bool) -> float:
        score = 0.0
        if zone_touches >= 3:    score += 3
        elif zone_touches >= 2:  score += 1
        if bias_aligned:         score += 2
        if pattern_valid:        score += 2
        if rr >= 3.0:            score += 2
        elif rr >= 2.0:          score += 1
        if macro_risk == "LOW":  score += 1
        if volume_confirmed:     score += 1
        return min(score, 11.0)

    def generate_signal(self, market_data: dict) -> Optional[Signal]:
        signal, _ = self.generate_signal_with_diagnostics(market_data)
        return signal

    def generate_signal_with_diagnostics(self, market_data: dict) -> Tuple[Optional[Signal], dict]:
        df_h1     = market_data["df_h1"]
        df_h4     = market_data["df_h4"]
        asset     = market_data["asset"]
        direction = market_data["direction"]

        diag = {"conditions": {}, "raw_score": None, "rejection_reason": None}

        if len(df_h1) < 25 or len(df_h4) < 25:
            diag["rejection_reason"] = "INSUFFICIENT_DATA"
            return None, diag

        last_h1 = df_h1.iloc[-1]
        last_h4 = df_h4.iloc[-1]
        price   = float(last_h1["close"])
        atr_h1  = float(last_h1["atr"])
        atr_h4  = float(last_h4["atr"]) if "atr" in last_h4.index else atr_h1 * 4

        if atr_h1 <= 0:
            diag["rejection_reason"] = "ATR_ZERO"
            return None, diag

        bias = self._get_bias(df_h4)
        bias_aligned = (
            (direction == "LONG"  and bias == "RIALZISTA") or
            (direction == "SHORT" and bias == "RIBASSISTA")
        )
        diag["conditions"]["Bias H4 allineato"] = bias_aligned

        zones = self._get_zones(df_h4, direction, atr_h4)
        if not zones:
            diag["rejection_reason"] = "NO_ZONES_FOUND"
            return None, diag

        nearest = min(zones, key=lambda z: abs(price - z["price"]))
        zone_price   = nearest["price"]
        zone_touches = nearest.get("count", 2)

        in_zone  = self._price_in_zone(price, zone_price)
        dist_pct = abs(price - zone_price) / zone_price * 100 if zone_price > 0 else 999
        diag["conditions"][f"Prezzo nella zona ({dist_pct:.2f}% da {zone_price:.4f})"] = in_zone

        if not in_zone:
            diag["rejection_reason"] = f"PRICE_NOT_IN_ZONE ({dist_pct:.2f}% > 0.5%)"
            return None, diag

        rejection_ok = self._pattern_rejection(df_h1, direction)
        sweep_ok     = self._pattern_sweep(df_h1, zone_price, direction)
        pullback_ok  = self._pattern_pullback_ema(df_h1, direction)
        pattern_ok   = rejection_ok or sweep_ok or pullback_ok

        if rejection_ok:
            pattern_name = "REJECTION_CANDLE"
        elif sweep_ok:
            pattern_name = "LIQUIDITY_SWEEP"
        elif pullback_ok:
            pattern_name = "PULLBACK_EMA"
        else:
            pattern_name = "NONE"

        volume_confirmed = sweep_ok

        diag["conditions"]["Pattern Rejection Candle"] = bool(rejection_ok)
        diag["conditions"]["Pattern Liquidity Sweep"]  = bool(sweep_ok)
        diag["conditions"]["Pattern Pullback EMA"]     = bool(pullback_ok)

        if not pattern_ok:
            diag["rejection_reason"] = "NO_PATTERN_CONFIRMED"
            return None, diag

        if direction == "LONG":
            sl = price - self.ATR_MULTIPLIER * atr_h1
            opposite_zones = self._get_zones(df_h4, "SHORT", atr_h4)
            above = [z for z in opposite_zones if z["price"] > price]
            if not above:
                diag["rejection_reason"] = "NO_TP_ZONE"
                return None, diag
            tp = min(above, key=lambda z: z["price"])["price"]
        else:
            sl = price + self.ATR_MULTIPLIER * atr_h1
            opposite_zones = self._get_zones(df_h4, "LONG", atr_h4)
            below = [z for z in opposite_zones if z["price"] < price]
            if not below:
                diag["rejection_reason"] = "NO_TP_ZONE"
                return None, diag
            tp = max(below, key=lambda z: z["price"])["price"]

        risk   = abs(price - sl)
        reward = abs(tp - price)
        rr     = reward / risk if risk > 0 else 0.0

        diag["conditions"][f"R/R >= {self.MIN_RR}"] = rr >= self.MIN_RR

        if rr < self.MIN_RR:
            diag["rejection_reason"] = f"RR_TOO_LOW ({rr:.2f})"
            return None, diag

        macro_event = market_data.get("macro_event")
        macro_risk  = self._get_macro_risk(macro_event)

        ts_ms  = int(last_h1["timestamp"])
        ts_dt  = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        session  = self._get_session(ts_dt)
        momentum = self._get_momentum(df_h1, direction)
        atr_daily = self._atr_daily(df_h4)

        raw_score = self._score(
            zone_touches=zone_touches,
            bias_aligned=bias_aligned,
            pattern_valid=pattern_ok,
            rr=rr,
            macro_risk=macro_risk,
            volume_confirmed=volume_confirmed,
        )
        diag["raw_score"] = raw_score

        if raw_score < self.DB_SCORE_THRESHOLD:
            diag["rejection_reason"] = f"SCORE_TOO_LOW ({raw_score:.0f} < {self.DB_SCORE_THRESHOLD})"
            return None, diag

        send_telegram = raw_score >= self.TELEGRAM_SCORE_THRESHOLD

        signal = Signal(
            strategy_name=self.name,
            strategy_version=self.version,
            asset=asset,
            direction=direction,
            entry=price,
            stop_loss=sl,
            take_profit=tp,
            rr=rr,
            raw_score=raw_score,
            final_score=raw_score,
            timestamp=ts_dt,
            additional_context={
                "zone_level":       zone_price,
                "zone_touches":     zone_touches,
                "bias_h4":          bias,
                "bias_aligned":     bias_aligned,
                "pattern_name":     pattern_name,
                "volume_confirmed": volume_confirmed,
                "macro_risk":       macro_risk,
                "session":          session,
                "momentum":         momentum,
                "atr_daily":        atr_daily,
                "atr_h1":           atr_h1,
                "send_telegram":    send_telegram,
                "macro_event":      macro_event,
            },
        )
        return signal, diag
