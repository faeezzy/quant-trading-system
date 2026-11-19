"""Validator tests. Fixtures are synthetic, shaped after the documented response formats —
they are NOT real market data."""
from data.validators import market as v

NOW = 1_700_000_000 - (1_700_000_000 % 900) + 60  # 1 min into an open 15m candle


def candle(t, o=100, h=110, l=90, c=105, vol="1.0"):
    return {"Time": t, "Open": o, "High": h, "Low": l, "Close": c, "Volume": vol}


def series(n, end_open):
    return [candle(end_open - 900 * i) for i in reversed(range(n))]


def test_clean_series_ok_and_open_candle_flagged():
    open_t = NOW - 60
    r = v.validate_candles(series(30, open_t), "15", NOW)
    assert r.ok, r.issues
    assert r.metrics["open_candle_present"] is True
    assert r.metrics["closed"] == 29


def test_missing_candle_detected():
    s = series(30, NOW - 60)
    del s[10]
    r = v.validate_candles(s, "15", NOW)
    assert not r.ok and r.metrics["missing"] == 1


def test_duplicate_detected():
    s = series(10, NOW - 60)
    s.append(dict(s[-1]))
    r = v.validate_candles(s, "15", NOW)
    assert any("duplicate" in i for i in r.issues)


def test_impossible_ohlc_detected():
    s = series(5, NOW - 60)
    s[2] = candle(s[2]["Time"], h=95)  # high below open/close
    r = v.validate_candles(s, "15", NOW)
    assert any("impossible OHLC" in i for i in r.issues)


def test_stale_detected():
    r = v.validate_candles(series(10, NOW - 900 * 10), "15", NOW)
    assert any("stale" in i for i in r.issues)


def test_empty_candles_fail():
    assert not v.validate_candles([], "15", NOW).ok


def test_book_spread_and_depth():
    book = {"bidPrice": "1236777000", "askPrice": "1236800000"}
    depth = {"bids": [["1236777000", "0.1"], ["1236776000", "0.2"]],
             "asks": [["1236800000", "0.1"], ["1236900000", "0.2"]]}
    r = v.validate_book(book, depth, max_spread_pct=0.5)
    assert r.ok, r.issues
    assert 0 < r.metrics["spread_pct"] < 0.01


def test_crossed_book_and_wide_spread_fail():
    r = v.validate_book({"bidPrice": "101", "askPrice": "100"}, {"bids": [["101", "1"]], "asks": [["100", "1"]]}, 0.5)
    assert any("crossed" in i for i in r.issues)
    r2 = v.validate_book({"bidPrice": "100", "askPrice": "110"}, {"bids": [["100", "1"]], "asks": [["110", "1"]]}, 0.5)
    assert any("spread" in i for i in r2.issues)


def test_pair_metadata_missing_disables_market():
    assert not v.validate_pair_metadata(None, "btcidr").ok
    r = v.validate_pair_metadata({"id": "btcidr", "trade_min_base_currency": 10000}, "btcidr")
    assert not r.ok and any("trade_fee_percent_taker" in i for i in r.issues)


def test_pair_in_maintenance_disabled():
    p = {k: 1 for k in ["trade_min_base_currency", "trade_min_traded_currency", "price_precision",
                        "quantity_increment", "trade_fee_percent_taker", "trade_fee_percent_maker"]}
    p["is_maintenance"] = 1
    assert not v.validate_pair_metadata(p, "x").ok


def test_clock_skew():
    assert v.check_clock(1_000_000, 1000.5).ok
    assert not v.check_clock(1_000_000, 1010.0).ok
