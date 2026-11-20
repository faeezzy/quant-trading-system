import numpy as np
import pandas as pd

from strategies.trend_breakout import Costs, Params, generate


def frame(n, step, start=0, drift=0.0, seed=0, vol=1.0):
    rng = np.random.default_rng(seed)
    close = 1000 + np.cumsum(rng.normal(drift, 1, n))
    return pd.DataFrame({"open_time": start + np.arange(n) * step, "open": close,
                         "high": close + 1, "low": close - 1, "close": close,
                         "volume": rng.uniform(0.5, 1.5, n) * vol})


def test_regime_join_has_no_lookahead():
    sig = frame(4000, 900)
    reg = frame(1000, 3600)
    out = generate(sig, reg, Params(atr_pct_median_window=100))
    # every 15m row may only see a 1H candle whose close_time <= the 15m close_time
    hours = reg["open_time"].to_numpy() + 3600
    for _, row in out.dropna(subset=["h_ema_slow"]).sample(200, random_state=0).iterrows():
        last_closed_hour = hours[hours <= row["close_time"]].max()
        assert last_closed_hour <= row["close_time"]


def test_signal_requires_all_conditions_and_warmup():
    sig = frame(4000, 900, drift=0.3)
    reg = frame(1000, 3600, drift=1.0)
    # zero costs: synthetic ATR (~0.2% of price) is below realistic round-trip costs (~0.65%),
    # which would (correctly) reject every signal. This test checks the condition logic only.
    out = generate(sig, reg, Params(atr_pct_median_window=100), Costs(0, 0, 0, 0))
    s = out[out["signal"]]
    assert len(s) > 0
    assert s[["c_trend", "c_adx", "c_breakout", "c_volume", "c_has_market", "c_rr"]].all().all()
    assert s["warm"].all()
    assert (s["stop"] < s["entry_ref"]).all() and (s["target"] > s["entry_ref"]).all()


def test_zero_volume_bar_never_signals():
    sig = frame(4000, 900, drift=0.3)
    sig.loc[3000:, "volume"] = 0.0
    out = generate(sig, frame(1000, 3600, drift=1.0), Params(atr_pct_median_window=100), Costs(0, 0, 0, 0))
    assert not out.loc[out["open_time"] >= sig.loc[3000, "open_time"], "signal"].any()


def test_costs_can_kill_signals():
    sig, reg = frame(4000, 900, drift=0.3), frame(1000, 3600, drift=1.0)
    cheap = generate(sig, reg, Params(atr_pct_median_window=100), Costs(0, 0, 0, 0))["signal"].sum()
    pricey = generate(sig, reg, Params(atr_pct_median_window=100), Costs(2, 2, 1, 1))["signal"].sum()
    assert cheap > 0 and pricey < cheap


def test_bear_regime_blocks_longs():
    sig = frame(4000, 900, drift=0.3)
    reg = frame(1000, 3600, drift=-1.0)
    out = generate(sig, reg, Params(atr_pct_median_window=100), Costs(0, 0, 0, 0))
    assert out["signal"].sum() == 0


def test_cost_floor_mode_filters_by_stop_distance():
    from dataclasses import replace
    sig, reg = frame(4000, 900, drift=0.3), frame(1000, 3600, drift=1.0)
    p = replace(Params(atr_pct_median_window=100), rr_mode="cost_floor", cost_floor_mult=1.0)
    out = generate(sig, reg, p, Costs(0.01, 0.01, 0, 0))
    stop_pct = (out["entry_ref"] - out["stop"]) / out["entry_ref"] * 100
    assert (out.loc[out["signal"], "entry_ref"] > 0).all()
    assert (stop_pct[out["signal"]] >= 0.02 - 1e-12).all()
    strict = generate(sig, reg, replace(p, cost_floor_mult=1000.0), Costs(0.01, 0.01, 0, 0))
    assert strict["signal"].sum() == 0


def test_resample_daily_keeps_only_complete_days():
    from strategies.trend_breakout import resample_daily
    h4 = frame(6 * 5 + 3, 14400, start=0)          # 5 full days + half a day
    d = resample_daily(h4)
    assert len(d) == 5 and (d["open_time"] % 86400 == 0).all()
    first = h4.iloc[:6]
    assert d.loc[0, "high"] == first["high"].max() and d.loc[0, "close"] == first["close"].iloc[-1]
    assert d.loc[0, "volume"] == first["volume"].sum()
