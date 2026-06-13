-- schema.sql
-- Schema completo per Crypto Signal Engine V1.0

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    timestamp_alert DATETIME,
    timestamp_setup DATETIME NOT NULL,
    timestamp_closed DATETIME,
    asset TEXT NOT NULL,
    setup TEXT NOT NULL,
    direzione TEXT NOT NULL CHECK(direzione IN ('LONG','SHORT')),
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    rr REAL NOT NULL,
    score INTEGER NOT NULL,
    stato TEXT NOT NULL CHECK(stato IN ('ACTIVE','TP','SL','EXPIRED')),
    atr_h1 REAL,
    support_level REAL,
    resistance_level REAL,
    trigger_type TEXT,
    macro_event_active BOOLEAN DEFAULT 0,
    macro_event_type TEXT,
    macro_event_minutes_to_release INTEGER,
    mae REAL,
    mfe REAL,
    bars_open INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_asset_dir_stato ON trades(asset, direzione, stato);
CREATE INDEX IF NOT EXISTS idx_trades_stato ON trades(stato);

-- Cache delle candele OHLCV per asset/timeframe.
-- Popolata dal bootstrap iniziale e aggiornata incrementalmente.
CREATE TABLE IF NOT EXISTS candles_cache (
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    PRIMARY KEY (asset, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_candles_asset_tf_ts ON candles_cache(asset, timeframe, timestamp);
