import numpy as np
import pandas as pd

from strategies.v3 import buy_and_hold, daily_trend, mr_4h_uptrend, rsi, weekly_rotation


def h4(n_days=400, drift=0.0, seed=0, start=0):
    rng = np.random.default_rng(seed)
    n = n_days * 6
    c = 1000 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    return pd.DataFrame({"open_time": start + np.arange(n) * 14400, "open": c, "high": c * 1.005,
                         "low": c * 0.995, "close": c, "volume": rng.uniform(1, 2, n)})


def test_rsi_bounds():
    r = rsi(pd.Series(np.r_[np.arange(10.0), np.arange(10.0)[::-1]]), 2).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_daily_trend_no_lookahead_and_signals():
    data = {"A": h4(drift=0.001), "B": h4(seed=1, drift=-0.001)}
    fr = daily_trend(data)
    assert fr["A"]["signal"].sum() > 0
    data2 = {s: d.copy() for s, d in data.items()}
    data2["A"].loc[2000:, ["open", "high", "low", "close"]] *= 5
    fr2 = daily_trend(data2)
    cut = data["A"].loc[2000, "open_time"] // 86400 * 86400
    a, b = fr["A"][fr["A"]["open_time"] < cut], fr2["A"][fr2["A"]["open_time"] < cut]
    assert (a["signal"].to_numpy() == b["signal"].to_numpy()).all()


def test_rotation_holds_at_most_one_coin():
    data = {s: h4(seed=i, drift=0.0005 * (i - 1)) for i, s in enumerate("ABC")}
    fr = weekly_rotation(data)
    held = sum(fr[s]["signal"].astype(int) for s in fr)
    assert held.max() <= 1


def test_mr_uses_last_complete_day_only():
    data = {"A": h4(drift=0.0008)}
    fr = mr_4h_uptrend(data)
    assert {"signal", "exit_signal", "atr"} <= set(fr["A"].columns)
    assert not fr["A"]["signal"].iloc[:200 * 6].any()   # daily SMA200 not warm before ~day 200


def test_buy_and_hold_costs():
    f = pd.DataFrame({"open_time": [0, 1], "open": [100.0, 100.0], "close": [100.0, 100.0]})
    assert buy_and_hold({"X": f}, fee_pct=0.2, spread_pct=0.0)["X"] < 0
