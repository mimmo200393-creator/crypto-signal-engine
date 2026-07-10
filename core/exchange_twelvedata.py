"""
core/exchange_twelvedata.py
Wrapper per l'API Twelve Data — usato per XAU_USD (oro spot).

Espone le STESSE tre funzioni pubbliche di core/exchange.py e
core/v3_exchange.py, con lo stesso formato di ritorno, così il resto
del sistema (engine, strategie, Ledger) non sa da dove arrivano i dati:

    bootstrap_history(...)      -> list[dict]
    fetch_new_candles_since(...) -> list[dict]
    fetch_latest_candles(...)   -> list[dict]

Formato candela (identico a exchange.py):
    {"timestamp": ms, "open", "high", "low", "close", "volume"}

── PERCHE' TWELVE DATA ───────────────────────────────────────────
PAXG_USDT (Crypto.com) non replica i movimenti dell'oro: book sottile,
estremi compressi (verificato: oro a 4174, PAXG max 4118 nello stesso
momento). La metodologia SMC cerca impronte istituzionali che su un
token ERC-20 scambiato da retail crypto non esistono.

Provider valutati:
  - Twelve Data : 800 chiamate/giorno free, XAU/USD accessibile  → SCELTO
  - FCS API     : 500 crediti/MESE (~16/giorno) → incompatibile con scan/5min
  - MetaTrader 5: richiede VPS Windows + terminale aperto → incompatibile
                  con GitHub Actions (Linux, serverless)

── VOLUME: NON ESISTE ────────────────────────────────────────────
XAU/USD e' un mercato OTC: non c'e' un exchange centralizzato che
registri il volume scambiato. Verificato su tre fonti indipendenti:
  - Twelve Data : campo "volume" ASSENTE nella risposta
  - FCS API     : "v": 0 su tutte le candele
  - MetaTrader 5: real_volume = 0 per XAUUSD (solo tick_volume)

Questo modulo restituisce volume=0.0 e espone HAS_VOLUME=False.
ATTENZIONE per chi usa i dati: volume=0 significa "DATO ASSENTE",
non "volume basso". Gli engine che calcolano volume_ratio o
volume_classification producono valori privi di significato su questo
asset e i loro voti vanno considerati non informativi (vedi
data_source.has_volume(asset)).

── RATE LIMIT ────────────────────────────────────────────────────
Free tier: 8 crediti/minuto, 800/giorno. Una chiamata a /time_series
consuma 1 credito. Il fabbisogno reale, fetchando ogni timeframe alla
sua cadenza naturale (vedi data_source.should_fetch), e' ~175/giorno:
    M15=96  M30=48  H1=24  H4=6  D1=1
cioe' il 22% del budget. Fetchare a ogni scan (288/giorno x 5 tf =
1440 chiamate) SFOREREBBE: la cadenza e' obbligatoria, non un'ottimizzazione.
"""

from __future__ import annotations

import os
import time
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger("exchange_twelvedata")

BASE_URL = "https://api.twelvedata.com"

# Il volume non esiste su oro spot (OTC). Vedi docstring.
HAS_VOLUME = False

# Mappa timeframe INTERNO -> formato Twelve Data.
# Le chiavi sono i valori realmente passati dai runner:
#   exchange.py    usa "1h", "4h"
#   v3_exchange.py usa "1D", "30m", "15m"
TIMEFRAME_MAP = {
    "15m": "15min",
    "30m": "30min",
    "1h":  "1h",
    "4h":  "4h",
    "1D":  "1day",
}

# Mappa asset interno -> simbolo Twelve Data
SYMBOL_MAP = {
    "XAU_USD": "XAU/USD",
}

# Twelve Data accetta outputsize fino a 5000; il bootstrap chiede 300.
MAX_OUTPUTSIZE = 5000


class TwelveDataError(Exception):
    pass


def _api_key() -> str:
    """Chiave da variabile d'ambiente (GitHub Secrets). Mai nel codice."""
    key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    if not key:
        raise TwelveDataError(
            "TWELVEDATA_API_KEY non impostata. "
            "Aggiungerla ai GitHub Secrets e passarla come env nel workflow."
        )
    return key


def _symbol(instrument_name: str) -> str:
    return SYMBOL_MAP.get(instrument_name, instrument_name)


def _request_timeseries(instrument_name: str, timeframe: str,
                        outputsize: int, end_date: str | None = None) -> list[dict]:
    """
    Chiamata a /time_series. Ritorna la lista grezza di candele
    (dalla piu' recente alla piu' vecchia, come restituita dall'API).
    """
    interval = TIMEFRAME_MAP.get(timeframe)
    if interval is None:
        raise TwelveDataError(f"Timeframe non supportato: {timeframe}")

    params = {
        "symbol": _symbol(instrument_name),
        "interval": interval,
        "outputsize": min(int(outputsize), MAX_OUTPUTSIZE),
        "apikey": _api_key(),
        "format": "JSON",
        "timezone": "UTC",
    }
    if end_date:
        params["end_date"] = end_date

    resp = requests.get(f"{BASE_URL}/time_series", params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    # Twelve Data segnala gli errori nel body, non nello status HTTP
    if isinstance(data, dict) and data.get("status") == "error":
        raise TwelveDataError(
            f"API error per {instrument_name} {timeframe}: "
            f"code={data.get('code')} msg={data.get('message')}"
        )

    values = data.get("values") or []
    if not values:
        logger.info("TwelveData: nessuna candela per %s %s", instrument_name, timeframe)
    return values


def _normalize_candle(raw: dict) -> dict:
    """
    Converte una candela Twelve Data nel formato interno standard.

    Campi in ingresso: datetime, open, high, low, close  (NIENTE volume)
    Il timestamp interno e' in MILLISECONDI, come Crypto.com.
    """
    dt = datetime.strptime(raw["datetime"], "%Y-%m-%d %H:%M:%S") \
        if len(raw["datetime"]) > 10 \
        else datetime.strptime(raw["datetime"], "%Y-%m-%d")
    dt = dt.replace(tzinfo=timezone.utc)

    return {
        "timestamp": int(dt.timestamp() * 1000),
        "open":  float(raw["open"]),
        "high":  float(raw["high"]),
        "low":   float(raw["low"]),
        "close": float(raw["close"]),
        # Volume assente su oro spot: 0.0 = DATO NON DISPONIBILE.
        "volume": float(raw.get("volume") or 0.0),
    }


# ============================================================
# API pubblica — stessa firma di exchange.py / v3_exchange.py
# Il parametro base_url e' ignorato (Twelve Data ha endpoint fisso),
# ma mantenuto per compatibilita' con i chiamanti esistenti.
# ============================================================

def fetch_latest_candles(base_url, instrument_name, timeframe, count, request_delay):
    raw = _request_timeseries(instrument_name, timeframe, outputsize=count)
    candles = [_normalize_candle(c) for c in raw]
    candles.sort(key=lambda x: x["timestamp"])
    time.sleep(request_delay)
    return candles


def bootstrap_history(base_url, instrument_name, timeframe, target_candles,
                      max_per_call, request_delay):
    """
    Storico iniziale. Twelve Data permette outputsize fino a 5000, quindi
    di norma UNA sola chiamata basta per le 300 candele del bootstrap
    (contro le chiamate paginate di Crypto.com).
    """
    raw = _request_timeseries(
        instrument_name, timeframe, outputsize=target_candles
    )
    candles = [_normalize_candle(c) for c in raw]
    candles.sort(key=lambda x: x["timestamp"])

    logger.info(
        "TwelveData bootstrap %s %s: %d candele (target=%d) [volume non disponibile]",
        instrument_name, timeframe, len(candles), target_candles
    )
    time.sleep(request_delay)
    return candles


def fetch_new_candles_since(base_url, instrument_name, timeframe, since_timestamp,
                            max_per_call, request_delay):
    """
    Solo le candele piu' recenti di since_timestamp (ms).

    Nota: l'oro NON e' 24/7. Chiude ~48h nel weekend e ha una pausa
    giornaliera (~75 min, verificata: 20:45->22:00 UTC). In quelle
    finestre questa funzione restituisce [] legittimamente: non e' un
    errore, il mercato e' chiuso.
    """
    raw = _request_timeseries(instrument_name, timeframe, outputsize=max_per_call)
    candles = [_normalize_candle(c) for c in raw]
    candles.sort(key=lambda x: x["timestamp"])

    new_candles = [c for c in candles if c["timestamp"] > since_timestamp]
    if not new_candles:
        logger.debug(
            "TwelveData %s %s: nessuna candela nuova (mercato chiuso o gia' aggiornato)",
            instrument_name, timeframe
        )
    time.sleep(request_delay)
    return new_candles
