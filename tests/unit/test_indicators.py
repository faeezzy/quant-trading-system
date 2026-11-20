import numpy as np
import pandas as pd
import pytest

from indicators.core import adx, atr, donchian_high, ema, sma, wilder


def ohlc(n=300, seed=0, drift=0.0):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(drift, 1, n))
    high = close + rng.uniform(0.1, 1, n)
    low = close - rng.uniform(0.1, 1, n)
    return pd.Series(high), pd.Series(low), pd.Series(close)


def test_ema_constant_and_warmup():
    s = pd.Series([5.0] * 60)
    e = ema(s, 50)
    assert e.iloc[:49].isna().all() and e.iloc[49:].eq(5.0).all()


def test_wilder_seed_is_sma_then_recursive():
    s = pd.Series([1.0, 2, 3, 4, 10])
    w = wilder(s, 4)
    assert np.isnan(w.iloc[2]) and w.iloc[3] == 2.5
    assert w.iloc[4] == pytest.approx(2.5 + (10 - 2.5) / 4)


def test_atr_constant_range():
    h, l, c = pd.Series([11.0] * 30), pd.Series([9.0] * 30), pd.Series([10.0] * 30)
    assert atr(h, l, c, 14).iloc[-1] == pytest.approx(2.0)


def test_donchian_uses_previous_bars_only():
    h = pd.Series([1.0, 2, 3, 4, 5, 100])
    d = donchian_high(h, 3)
    assert d.iloc[5] == 5   # excludes the current bar's 100
    assert np.isnan(d.iloc[2])


def test_adx_trend_vs_noise():
    h, l, c = ohlc(400, drift=1.5)
    assert adx(h, l, c).iloc[-1] > 40
    h2, l2, c2 = ohlc(400, seed=1, drift=0.0)
    assert adx(h2, l2, c2).iloc[-1] < adx(h, l, c).iloc[-1]


@pytest.mark.parametrize("fn", [
    lambda h, l, c: ema(c, 50), lambda h, l, c: atr(h, l, c), lambda h, l, c: adx(h, l, c),
    lambda h, l, c: donchian_high(h), lambda h, l, c: sma(c, 20)])
def test_no_lookahead(fn):
    h, l, c = ohlc(300)
    base = fn(h, l, c)
    h2, l2, c2 = h.copy(), l.copy(), c.copy()
    h2.iloc[200:] *= 3; l2.iloc[200:] *= 0.3; c2.iloc[200:] *= 2   # mutate the "future"
    pd.testing.assert_series_equal(base.iloc[:200], fn(h2, l2, c2).iloc[:200])


def test_bad_period():
    with pytest.raises(ValueError):
        ema(pd.Series([1.0]), 0)
