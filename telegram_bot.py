"""
exchange.py
Wrapper per l'API pubblica Crypto.com Exchange (v1).
Endpoint: GET /public/get-candlestick
Nessuna API key richiesta.

Note operative:
- L'endpoint restituisce al massimo MAX_CANDLES_PER_CALL candele per chiamata.
- Per costruire lo storico iniziale (BOOTSTRAP_TARGET_CANDLES) servono piu'
  chiamate paginate usando il parametro 'end_ts' (timestamp ms) per andare
  indietro nel tempo.
- Tra ogni chiamata viene applicato un delay (REQUEST_DELAY_SECONDS) per
  rispettare i rate limit pubblici.
"""

import time
import logging
import requests

logger = logging.getLogger("exchange")

# Mappa timeframe interno -> formato richiesto dall'API Crypto.com
TIMEFRAME_MAP = {
    "1h": "1h",
    "4h": "4h",
}


class ExchangeError(Exception):
    pass


def _request_candlestick(base_url, instrument_name, timeframe, count=None, end_ts=None):
    """
    Esegue una singola chiamata a public/get-candlestick.
    Ritorna la lista di candele (lista di dict con o,h,l,c,v,t) ordinata
    dalla piu' vecchia alla piu' recente, come restituita dall'API.
    """
    url = f"{base_url}/public/get-candlestick"
    params = {
        "instrument_name": instrument_name,
        "timeframe": TIMEFRAME_MAP.get(timeframe, timeframe),
    }
    if count:
        params["count"] = count
    if end_ts:
        params["end_ts"] = int(end_ts)

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise ExchangeError(
            f"API error per {instrument_name} {timeframe}: {data}"
        )

    result = data.get("result", {})
    candles = result.get("data", [])
    return candles


def _normalize_candle(raw):
    """
    Converte una candela raw dell'API nel formato interno standardizzato.
    Raw fields: o (open), h (high), l (low), c (close), v (volume), t (start time ms)
    """
    return {
        "timestamp": int(raw["t"]),
        "open": float(raw["o"]),
        "high": float(raw["h"]),
        "low": float(raw["l"]),
        "close": float(raw["c"]),
        "volume": float(raw["v"]),
    }


def fetch_latest_candles(base_url, instrument_name, timeframe, count, request_delay):
    """
    Recupera le ultime `count` candele (singola chiamata, eventualmente
    splittata se count > MAX_CANDLES_PER_CALL gestito dal chiamante).
    Usato per gli aggiornamenti incrementali durante il loop di scansione.
    """
    raw = _request_candlestick(base_url, instrument_name, timeframe, count=count)
    candles = [_normalize_candle(c) for c in raw]
    candles.sort(key=lambda x: x["timestamp"])
    time.sleep(request_delay)
    return candles


def bootstrap_history(base_url, instrument_name, timeframe, target_candles,
                       max_per_call, request_delay):
    """
    Costruisce lo storico iniziale effettuando chiamate multiple, andando
    indietro nel tempo tramite end_ts, finche' non si raggiunge il numero
    di candele target (o l'API non restituisce piu' dati).

    Ritorna una lista di candele normalizzate, ordinate dalla piu' vecchia
    alla piu' recente, senza duplicati.
    """
    all_candles = {}  # timestamp -> candle, per deduplicare
    end_ts = None
    safety_max_calls = (target_candles // max_per_call) + 5  # margine di sicurezza

    for call_n in range(safety_max_calls):
        raw = _request_candlestick(
            base_url, instrument_name, timeframe,
            count=max_per_call, end_ts=end_ts
        )

        if not raw:
            logger.info(
                "Nessuna candela aggiuntiva per %s %s (chiamata %d), stop bootstrap.",
                instrument_name, timeframe, call_n + 1
            )
            break

        for r in raw:
            c = _normalize_candle(r)
            all_candles[c["timestamp"]] = c

        oldest_ts = min(all_candles.keys())

        if len(all_candles) >= target_candles:
            break

        # prossima chiamata: chiedi candele PRECEDENTI alla piu' vecchia ottenuta
        # sottraggo 1ms per evitare di richiedere di nuovo la stessa candela
        end_ts = oldest_ts - 1

        time.sleep(request_delay)

    sorted_candles = sorted(all_candles.values(), key=lambda x: x["timestamp"])

    logger.info(
        "Bootstrap completato per %s %s: %d candele raccolte (target=%d)",
        instrument_name, timeframe, len(sorted_candles), target_candles
    )

    return sorted_candles


def fetch_new_candles_since(base_url, instrument_name, timeframe, since_timestamp,
                             max_per_call, request_delay):
    """
    Recupera solo le candele piu' recenti di `since_timestamp` (timestamp ms
    dell'ultima candela gia' presente in DB/cache).
    Usato negli aggiornamenti incrementali per limitare le chiamate.
    """
    raw = _request_candlestick(base_url, instrument_name, timeframe, count=max_per_call)
    candles = [_normalize_candle(c) for c in raw]
    candles.sort(key=lambda x: x["timestamp"])

    new_candles = [c for c in candles if c["timestamp"] > since_timestamp]

    time.sleep(request_delay)
    return new_candles
