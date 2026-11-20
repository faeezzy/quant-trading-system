"""Crypto Fear & Greed Index collector (alternative.me) — market sentiment, daily since 2018.

Purpose:  store the full daily sentiment history for research/backtests.
Source:   GET https://api.alternative.me/fng/?limit=0&format=json  (public, no key; attribution:
          "Crypto Fear & Greed Index by alternative.me").
          Response: {"data": [{"value": "64", "value_classification": "Greed",
                               "timestamp": "1782950400", "time_until_update": "43200"}, ...]}
Outputs:  table sentiment_fng(day_ts PK, value, classification, fetched_at) in the candle DB.
No look-ahead rule (enforced by consumers, see strategies/v4.py): the value stamped day D is only
          used from D+1 00:00 UTC onward (one full day lag), since its exact publish time is unknown.
Failure:  network/HTTP errors -> raise; malformed rows are skipped and counted, never invented.
Tests:    tests/unit/test_sentiment.py
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)
URL = "https://api.alternative.me/fng/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sentiment_fng (
    day_ts INTEGER PRIMARY KEY,
    value INTEGER NOT NULL,
    classification TEXT,
    fetched_at TEXT NOT NULL
);
"""


def parse(payload: dict) -> tuple[list[tuple[int, int, str]], int]:
    rows, bad = [], 0
    for r in payload.get("data", []) if isinstance(payload, dict) else []:
        try:
            ts, v = int(r["timestamp"]), int(r["value"])
            if not 0 <= v <= 100:
                raise ValueError
            rows.append((ts - ts % 86400, v, str(r.get("value_classification", ""))))
        except (KeyError, TypeError, ValueError):
            bad += 1
    return rows, bad


def fetch(session: requests.Session | None = None, timeout: float = 20) -> dict:
    s = session or requests.Session()
    resp = s.get(URL, params={"limit": 0, "format": "json"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def store(conn: sqlite3.Connection, rows: list[tuple[int, int, str]]) -> int:
    conn.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.executemany("INSERT OR REPLACE INTO sentiment_fng VALUES (?,?,?,?)", [(*r, now) for r in rows])
    return len(rows)


def load(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    try:
        return conn.execute("SELECT day_ts, value FROM sentiment_fng ORDER BY day_ts").fetchall()
    except sqlite3.OperationalError:
        return []
