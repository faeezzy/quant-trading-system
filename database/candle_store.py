"""Raw OHLCV storage (SQLite, stdlib only).

Purpose:  persist raw exchange candles exactly as received (spec 4.3: raw separate from processed).
Inputs:   candle dicts {"Time","Open","High","Low","Close","Volume"} from /tradingview/history_v2.
Outputs:  table `raw_candles`, idempotent upsert keyed by (symbol, tf, open_time).
Failure:  sqlite errors propagate (caller must stop: no trading without reliable persistent state).
Notes:    prices/volume stored as TEXT to keep the exchange's exact representation.
Tests:    tests/unit/test_candle_store.py
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_candles (
    symbol     TEXT    NOT NULL,
    tf         TEXT    NOT NULL,
    open_time  INTEGER NOT NULL,      -- unix seconds, candle open
    open       TEXT    NOT NULL,
    high       TEXT    NOT NULL,
    low        TEXT    NOT NULL,
    close      TEXT    NOT NULL,
    volume     TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (symbol, tf, open_time)
) WITHOUT ROWID;
"""


class CandleStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def upsert(self, symbol: str, tf: str, candles: list[dict]) -> int:
        """Insert or replace. Returns number of rows written. Single transaction."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [(symbol, tf, int(c["Time"]), str(c["Open"]), str(c["High"]), str(c["Low"]),
                 str(c["Close"]), str(c["Volume"]), now) for c in candles]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO raw_candles VALUES (?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def bounds(self, symbol: str, tf: str) -> tuple[int | None, int | None, int]:
        with closing(self.conn.execute(
                "SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM raw_candles WHERE symbol=? AND tf=?",
                (symbol, tf))) as cur:
            lo, hi, n = cur.fetchone()
        return lo, hi, n

    def load(self, symbol: str, tf: str, start: int | None = None, end: int | None = None) -> list[dict]:
        q = "SELECT open_time, open, high, low, close, volume FROM raw_candles WHERE symbol=? AND tf=?"
        args: list = [symbol, tf]
        if start is not None:
            q += " AND open_time >= ?"; args.append(start)
        if end is not None:
            q += " AND open_time <= ?"; args.append(end)
        q += " ORDER BY open_time"
        with closing(self.conn.execute(q, args)) as cur:
            return [{"Time": t, "Open": o, "High": h, "Low": l, "Close": c, "Volume": v}
                    for t, o, h, l, c, v in cur.fetchall()]
