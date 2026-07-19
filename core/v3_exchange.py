"""
core/v3_exchange.py
Wrapper dedicato per il fetch delle candele D1/M30/M15 usate dall'
Institutional Scanner Framework V3.2 (PAXG_USDT, BTC_USDT).

Modulo isolato: NON modifica core/exchange.py esistente.
H4 e H1 vengono riusati dalle tabelle candles_cache esistenti
(gia' scaricate dal Signal Engine principale per questi due asset,
che fanno parte della watchlist V2.1).

Generalizzato per qualsiasi asset della V3_SCANNER_ASSETS, non solo PAXG.
"""

import time
import logging
import requests

logger = logging.getLogger("v3_exchange")

V3_TIMEFRAME_MAP = {
    "1D": "1D",
    "30m": "30m",
    "15m": "15m",
    "5m": "5m",
}


class V3ExchangeError(Exception):
    pass


def _request_candlestick(base_url, instrument_name, timeframe, count=None, end_ts=None):
    url = f"{base_url}/public/get-candlestick"
    params = {
        "instrument_name": instrument_name,
        "timeframe": V3_TIMEFRAME_MAP.get(timeframe, timeframe),
    }
    if count:
        params["count"] = count
    if end_ts:
        params["end_ts"] = int(end_ts)

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise V3ExchangeError(
            f"V3 API error per {instrument_name} {timeframe}: {data}"
        )

    result = data.get("result", {})
    return result.get("data", [])


def _normalize_candle(raw):
    return {
        "timestamp": int(raw["t"]),
        "open": float(raw["o"]),
        "high": float(raw["h"]),
        "low": float(raw["l"]),
        "close": float(raw["c"]),
        "volume": float(raw["v"]),
    }


def fetch_latest_candles(base_url, instrument_name, timeframe, count, request_delay):
    raw = _request_candlestick(base_url, instrument_name, timeframe, count=count)
    candles = [_normalize_candle(c) for c in raw]
    candles.sort(key=lambda x: x["timestamp"])
    time.sleep(request_delay)
    return candles


def bootstrap_history(base_url, instrument_name, timeframe, target_candles,
                       max_per_call, request_delay):
    """Costruisce lo storico iniziale per D1/M30/M15 di un asset V3."""
    all_candles = {}
    end_ts = None
    safety_max_calls = (target_candles // max_per_call) + 5

    for call_n in range(safety_max_calls):
        raw = _request_candlestick(
            base_url, instrument_name, timeframe,
            count=max_per_call, end_ts=end_ts
        )

        if not raw:
            logger.info(
                "V3 bootstrap: nessuna candela aggiuntiva per %s %s (chiamata %d), stop.",
                instrument_name, timeframe, call_n + 1
            )
            break

        for r in raw:
            c = _normalize_candle(r)
            all_candles[c["timestamp"]] = c

        oldest_ts = min(all_candles.keys())

        if len(all_candles) >= target_candles:
            break

        end_ts = oldest_ts - 1
        time.sleep(request_delay)

    sorted_candles = sorted(all_candles.values(), key=lambda x: x["timestamp"])

    logger.info(
        "V3 bootstrap completato per %s %s: %d candele raccolte (target=%d)",
        instrument_name, timeframe, len(sorted_candles), target_candles
    )

    return sorted_candles


def fetch_new_candles_since(base_url, instrument_name, timeframe, since_timestamp,
                             max_per_call, request_delay):
    raw = _request_candlestick(base_url, instrument_name, timeframe, count=max_per_call)
    candles = [_normalize_candle(c) for c in raw]
    candles.sort(key=lambda x: x["timestamp"])

    new_candles = [c for c in candles if c["timestamp"] > since_timestamp]

    time.sleep(request_delay)
    return new_candles
