"""Backfill/store tests with a fake exchange that holds a finite synthetic history."""
from data.collectors.historical import backfill, update
from database.candle_store import CandleStore

STEP = 900
NOW = 1_700_000_000 - (1_700_000_000 % STEP) + 120   # 2 min into an open candle
HIST_START = NOW - 120 - 5000 * STEP                    # exchange has 5000 closed bars + 1 open


def c(t):
    return {"Time": t, "Open": 100, "High": 101, "Low": 99, "Close": 100.5, "Volume": "1.5"}


class FakeExchange:
    def __init__(self, max_bars=None, holes=()):
        self.calls, self.max_bars, self.holes = [], max_bars, set(holes)

    def ohlc(self, symbol, tf, start, end):
        self.calls.append((start, end))
        t0 = max(start - start % STEP, HIST_START)
        out = [c(t) for t in range(t0, min(end, NOW - 120) + 1, STEP) if t not in self.holes]
        if self.max_bars:
            out = out[-self.max_bars:]   # server truncates, keeping the newest
        return out


def test_full_backfill_stops_at_history_start(tmp_path):
    s, ex = CandleStore(tmp_path / "c.db"), FakeExchange()
    r = backfill(ex, s, "BTCIDR", "15", start_s=NOW - 10 * 365 * 86400, now_s=NOW, chunk_bars=1000)
    lo, hi, n = s.bounds("BTCIDR", "15")
    assert r.reached_history_start
    assert n == 5000 and lo == HIST_START and hi == NOW - 120 - STEP   # open candle excluded


def test_respects_lookback(tmp_path):
    s = CandleStore(tmp_path / "c.db")
    backfill(FakeExchange(), s, "BTCIDR", "15", start_s=NOW - 100 * STEP, now_s=NOW)
    _, _, n = s.bounds("BTCIDR", "15")
    assert 99 <= n <= 101


def test_truncated_server_chunks_do_not_skip_data(tmp_path):
    s = CandleStore(tmp_path / "c.db")
    backfill(FakeExchange(max_bars=300), s, "BTCIDR", "15", start_s=NOW - 2000 * STEP, now_s=NOW, chunk_bars=1000)
    lo, hi, n = s.bounds("BTCIDR", "15")
    assert n >= 1999   # no holes despite server returning only 300 bars per call


def test_idempotent_and_update(tmp_path):
    s, ex = CandleStore(tmp_path / "c.db"), FakeExchange()
    backfill(ex, s, "ETHIDR", "15", start_s=NOW - 500 * STEP, now_s=NOW)
    _, _, n1 = s.bounds("ETHIDR", "15")
    backfill(ex, s, "ETHIDR", "15", start_s=NOW - 500 * STEP, now_s=NOW)
    assert s.bounds("ETHIDR", "15")[2] == n1
    later = NOW + 10 * STEP
    update(ex, s, "ETHIDR", "15", now_s=later)
    # the candle that was still open at NOW is closed by `later` -> exactly one new row, no duplicates
    assert s.bounds("ETHIDR", "15")[2] == n1 + 1


def test_real_holes_are_preserved_not_filled(tmp_path):
    hole = NOW - 120 - 50 * STEP
    s = CandleStore(tmp_path / "c.db")
    backfill(FakeExchange(holes={hole}), s, "BTCIDR", "15", start_s=NOW - 200 * STEP, now_s=NOW)
    times = [r["Time"] for r in s.load("BTCIDR", "15")]
    assert hole not in times


def test_store_roundtrip_keeps_exact_strings(tmp_path):
    s = CandleStore(tmp_path / "c.db")
    s.upsert("X", "15", [{"Time": 900, "Open": "1146133000", "High": "1147196000", "Low": "1146000000",
                          "Close": "1147000000", "Volume": "0.00012345"}])
    row = s.load("X", "15")[0]
    assert row["Volume"] == "0.00012345" and row["Open"] == "1146133000"
