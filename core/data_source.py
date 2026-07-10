"""
core/data_source.py
Router dei provider dati — sceglie da dove scaricare le candele
in base all'ASSET, mantenendo invariato tutto il resto del sistema.

    BTC_USDT, PAXG_USDT, ...  -> Crypto.com  (core.exchange / core.v3_exchange)
    XAU_USD                   -> Twelve Data (core.exchange_twelvedata)

I provider espongono le stesse tre funzioni con la stessa firma e lo
stesso formato di ritorno, quindi engine, strategie e Ledger non sanno
(e non devono sapere) da dove arrivano i dati.

── DUE FAMIGLIE DI PROVIDER ──────────────────────────────────────
Il sistema ha due moduli exchange per Crypto.com, identici tranne che
per la TIMEFRAME_MAP:
    core.exchange     -> usato da candles_runner.py   (H4, H1)
    core.v3_exchange  -> usato da v3_runner.py        (D1, M30, M15)
get_provider() accetta quale famiglia serve, cosi' il chiamante ottiene
il modulo giusto senza cambiare la propria logica.
Twelve Data copre tutti i timeframe con un solo modulo.

── CADENZA (obbligatoria, non opzionale) ─────────────────────────
Il free tier Twelve Data da' 800 chiamate/giorno. Lo scanner gira ogni
5 minuti (288 volte/giorno). Fetchare tutti i timeframe a ogni scan
costerebbe 288 x 5 = 1440 chiamate/giorno -> SFORA.

should_fetch() limita la FREQUENZA DI FETCH per timeframe:
    M15 ogni 15m   M30 ogni 15m   H1 ogni 30m   H4 ogni 60m   D1 ogni 4h
    = 270 chiamate/giorno (34% del budget)

ATTENZIONE — perche' NON si usa la durata del timeframe come cadenza:
la candela piu' recente in DB e' quella IN FORMAZIONE, non chiusa. Se
per H4 si aspettassero 4 ore prima di rifetchare, per 4 ore il DB
avrebbe una candela H4 con close/high/low fermi al momento in cui e'
stata aperta. Gli engine leggerebbero dati stantii. La cadenza deve
essere piu' fitta della durata del timeframe, cosi' la candela in
formazione viene aggiornata.

La cadenza e' STATELESS: derivata dall'orologio, non da un file di stato.
Su GitHub Actions ogni run parte da un checkout pulito, quindi qualunque
stato su file sarebbe sempre vuoto (e la cadenza non scatterebbe mai).
Si usa invece il minuto-del-giorno corrente: "fetcha H4 solo quando il
minuto del giorno e' multiplo di 60". Con scan ogni 5 minuti questo
produce esattamente la frequenza voluta, senza memoria fra i run.

La cadenza si applica SOLO ai provider a consumo (Twelve Data).
Crypto.com non ha limiti rilevanti: per quegli asset should_fetch()
ritorna sempre True e il comportamento resta identico a prima.

── ORARI DI MERCATO ──────────────────────────────────────────────
L'oro non e' 24/7: chiude 21:00-22:00 UTC ogni giorno e dal venerdi'
21:00 alla domenica 22:00 (orari verificati sui gap delle candele reali).
E' chiuso ~32% del tempo. Chiamare l'API a mercato chiuso spreca crediti
per ricevere sempre le stesse candele: should_fetch() li salta.
Vedi is_market_open(). Il bootstrap ignora il check (se il DB e' vuoto va
riempito comunque).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("data_source")

# Asset serviti da Twelve Data (oro spot). Tutto il resto -> Crypto.com.
TWELVEDATA_ASSETS = {"XAU_USD"}

# Intervallo minimo fra due FETCH, in minuti, per timeframe.
# Piu' fitto della durata del TF, per aggiornare la candela in formazione.
# Deve essere multiplo dell'intervallo di scan (5 min) perche' la finestra
# stateless scatti in modo affidabile.
FETCH_CADENCE_MIN = {
    "15m": 15,
    "30m": 15,
    "1h":  30,
    "4h":  60,
    "1D":  240,
}

# Tolleranza: lo scan parte ogni 5 min ma con qualche secondo di ritardo
# variabile (coda runner GitHub). La finestra di fetch e' larga SCAN_SLACK_MIN
# minuti per non mancare il boundary.
SCAN_SLACK_MIN = 5


# ============================================================
# ORARI DI MERCATO (solo per asset non-crypto)
# ============================================================
# L'oro NON e' 24/7. Orari ricavati dai dati reali (candele M15, FCS):
#   - pausa giornaliera: ultima candela 20:45, prima candela 22:00 UTC
#     -> mercato chiuso 21:00 - 22:00 UTC, tutti i giorni
#   - weekend: chiude venerdi' 21:00 UTC, riapre domenica 22:00 UTC
#
# Il mercato e' chiuso ~32% del tempo (weekend + pause). Chiamare l'API
# in quelle finestre spreca crediti per ricevere sempre le stesse candele.
#
# MARGINE: si continua a fetchare per MARKET_EDGE_MARGIN_MIN minuti DOPO
# la chiusura (la candela finale puo' arrivare in ritardo) e si riprende
# altrettanti minuti PRIMA della riapertura (il primo tick puo' anticipare).
# Meglio qualche chiamata in piu' che perdere la candela di apertura.

MARKET_CLOSE_HOUR_UTC = 21   # chiusura giornaliera
MARKET_OPEN_HOUR_UTC = 22    # riapertura giornaliera

# Margini distinti: la pausa giornaliera dura solo 60 min, quindi un margine
# di 30 min prima+dopo la annullerebbe del tutto (21:30 <= x < 21:30 = mai).
# Il weekend dura ~49h e tollera un margine ampio.
DAILY_EDGE_MARGIN_MIN = 15    # pausa giornaliera (60 min) -> chiuso 21:15-21:45
WEEKEND_EDGE_MARGIN_MIN = 30  # weekend (49h) -> margine generoso

# Asset con orari di mercato (non 24/7). Le crypto non sono qui.
MARKET_HOURS_ASSETS = {"XAU_USD"}


def is_market_open(asset: str, now: datetime | None = None) -> bool:
    """
    True se il mercato dell'asset e' (probabilmente) aperto.

    Conservativa: nel dubbio ritorna True. Il costo di un fetch inutile
    e' un credito; il costo di perdere la candela di apertura e' un buco
    nei dati che gli engine leggono.

    Per gli asset 24/7 (crypto) ritorna sempre True.
    """
    if asset not in MARKET_HOURS_ASSETS:
        return True

    now = now or datetime.now(timezone.utc)
    dow = now.weekday()          # 0=lun ... 4=ven, 5=sab, 6=dom
    minutes = now.hour * 60 + now.minute

    close_min = MARKET_CLOSE_HOUR_UTC * 60          # 1260 (21:00)
    open_min = MARKET_OPEN_HOUR_UTC * 60            # 1320 (22:00)

    # ── WEEKEND ──────────────────────────────────────────────
    wm = WEEKEND_EDGE_MARGIN_MIN

    # Sabato: sempre chiuso.
    if dow == 5:
        return False

    # Venerdi': aperto fino alla chiusura + margine (poi weekend).
    if dow == 4:
        return minutes < (close_min + wm)

    # Domenica: chiuso fino alla riapertura - margine.
    if dow == 6:
        return minutes >= (open_min - wm)

    # ── LUN-GIO: pausa giornaliera 21:00-22:00 ───────────────
    dm = DAILY_EDGE_MARGIN_MIN
    if (close_min + dm) <= minutes < (open_min - dm):
        return False

    return True


def is_metered(asset: str) -> bool:
    """True se l'asset usa un provider con budget di chiamate limitato."""
    return asset in TWELVEDATA_ASSETS


def has_volume(asset: str) -> bool:
    """
    False se il provider NON fornisce volume per quell'asset.

    XAU_USD e' OTC: nessun volume scambiato centralizzato esiste
    (verificato su Twelve Data, FCS, MetaTrader). Il provider mette
    volume=0.0, che significa DATO ASSENTE, non "volume basso".

    Chi calcola volume_ratio / volume_classification deve consultare
    questa funzione: su asset senza volume quei valori sono privi di
    significato e i voti degli engine volume-dependent vanno ignorati.
    """
    if asset in TWELVEDATA_ASSETS:
        return False
    return True


def get_provider(asset: str, family: str = "main"):
    """
    Ritorna il modulo provider giusto per l'asset.

    family:
        "main" -> il modulo usato da candles_runner (H4/H1)
        "v3"   -> il modulo usato da v3_runner      (D1/M30/M15)
    Per Twelve Data la famiglia e' irrilevante: un solo modulo copre tutti
    i timeframe.
    """
    if asset in TWELVEDATA_ASSETS:
        from core import exchange_twelvedata
        return exchange_twelvedata

    if family == "v3":
        from core import v3_exchange
        return v3_exchange

    from core import exchange
    return exchange


def should_fetch(asset: str, timeframe: str, last_candle_ts_ms: int | None = None,
                 now: datetime | None = None) -> bool:
    """
    True se e' il momento di richiedere nuove candele per (asset, timeframe).

    STATELESS: nessuna memoria fra i run (su GitHub Actions non ce ne sarebbe).
    Si guarda il minuto-del-giorno corrente e si fetcha solo quando cade in
    una finestra allineata alla cadenza del timeframe.

    Esempio H4 (cadenza 60 min, slack 5): fetcha quando il minuto del giorno
    e' 0..4, 60..64, 120..124, ... cioe' una volta ogni ora.

    Protegge il budget dei provider a consumo. Per gli altri (Crypto.com)
    ritorna sempre True: comportamento identico a prima.

    Il bootstrap (nessuna candela in DB) fetcha sempre, ignorando la cadenza.
    """
    if not is_metered(asset):
        return True
    if last_candle_ts_ms is None:
        return True  # bootstrap: il DB e' vuoto per questo tf

    now = now or datetime.now(timezone.utc)

    # Mercato chiuso: nessuna candela nuova puo' esistere. Non sprecare crediti.
    # (Il bootstrap sopra ignora questo check: se il DB e' vuoto va riempito
    #  comunque, anche di domenica, con le ultime candele disponibili.)
    if not is_market_open(asset, now):
        logger.debug("%s: mercato chiuso -> skip fetch %s", asset, timeframe)
        return False

    cadence = FETCH_CADENCE_MIN.get(timeframe)
    if cadence is None:
        return True  # timeframe sconosciuto: non bloccare
    if cadence <= SCAN_SLACK_MIN:
        return True  # cadenza <= intervallo di scan: fetcha sempre

    minute_of_day = now.hour * 60 + now.minute

    in_window = (minute_of_day % cadence) < SCAN_SLACK_MIN
    if not in_window:
        logger.debug(
            "%s %s: fuori finestra (min_of_day=%d, cadenza=%d) -> skip",
            asset, timeframe, minute_of_day, cadence
        )
    return in_window
