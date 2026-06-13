"""
test_exchange.py
Script standalone per verificare l'endpoint pubblico Crypto.com
e determinare il valore reale di MAX_CANDLES_PER_CALL.

Esegui con: python3 test_exchange.py
"""

import requests

BASE_URL = "https://api.crypto.com/exchange/v1"


def test_count(instrument="BTC_USDT", timeframe="1h"):
    print(f"--- Test {instrument} {timeframe} ---")
    for count in [50, 100, 200, 300]:
        url = f"{BASE_URL}/public/get-candlestick"
        params = {"instrument_name": instrument, "timeframe": timeframe, "count": count}
        r = requests.get(url, params=params, timeout=15)
        try:
            data = r.json()
            n = len(data.get("result", {}).get("data", []))
            print(f"  count={count:4d} -> status={r.status_code} code={data.get('code')} candele_ricevute={n}")
        except Exception as e:
            print(f"  count={count:4d} -> status={r.status_code} ERRORE: {e}")


def test_pagination(instrument="BTC_USDT", timeframe="1h"):
    print(f"--- Test paginazione end_ts {instrument} {timeframe} ---")
    url = f"{BASE_URL}/public/get-candlestick"

    r1 = requests.get(url, params={"instrument_name": instrument, "timeframe": timeframe, "count": 50}, timeout=15)
    data1 = r1.json()
    candles1 = data1.get("result", {}).get("data", [])
    if not candles1:
        print("  Nessuna candela nella prima chiamata.")
        return

    oldest_ts = min(int(c["t"]) for c in candles1)
    print(f"  Prima chiamata: {len(candles1)} candele, oldest_ts={oldest_ts}")

    r2 = requests.get(url, params={
        "instrument_name": instrument, "timeframe": timeframe,
        "count": 50, "end_ts": oldest_ts - 1
    }, timeout=15)
    data2 = r2.json()
    candles2 = data2.get("result", {}).get("data", [])
    print(f"  Seconda chiamata (end_ts={oldest_ts-1}): {len(candles2)} candele")
    if candles2:
        newest_ts2 = max(int(c["t"]) for c in candles2)
        print(f"  newest_ts seconda chiamata = {newest_ts2} (dovrebbe essere < {oldest_ts})")


if __name__ == "__main__":
    test_count()
    print()
    test_pagination()
