"""Technical indicators (spec 5.1): EMA, ATR, ADX, Donchian, volume SMA.

Purpose:  pure, vectorised indicator functions on pandas Series.
Inputs:   float Series indexed by candle open time (ascending, no gaps).
Outputs:  Series aligned to the input index; NaN during warm-up (never back-filled).
Conventions:
  - Value at row t uses ONLY rows <= t (no look-ahead). Tests enforce this.
  - Wilder smoothing = EWM with alpha=1/n, seeded by the simple mean of the first n values
    (matches the classic Wilder definition used by most charting platforms).
  - donchian_high(n) at row t = max(high[t-n .. t-1]) — the PREVIOUS n candles,
    so "close > donchian_high" is a genuine breakout of the prior range.
Failure:  ValueError on non-positive period. Garbage-in (NaN) propagates as NaN.
Tests:    tests/unit/test_indicators.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _check(n: int) -> None:
    if n <= 0:
        raise ValueError("period must be positive")


def ema(s: pd.Series, n: int) -> pd.Series:
    _check(n)
    out = s.ewm(span=n, adjust=False, min_periods=n).mean()
    return out


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing seeded with SMA of the first n valid values."""
    _check(n)
    vals = s.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    valid = np.flatnonzero(~np.isnan(vals))
    if len(valid) < n:
        return pd.Series(out, index=s.index)
    start = valid[0]
    seed_end = start + n - 1
    if np.isnan(vals[start:seed_end + 1]).any():
        return pd.Series(out, index=s.index)
    out[seed_end] = vals[start:seed_end + 1].mean()
    for i in range(seed_end + 1, len(vals)):
        out[i] = out[i - 1] + (vals[i] - out[i - 1]) / n
    return pd.Series(out, index=s.index)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return wilder(true_range(high, low, close), n)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_dm.iloc[0] = minus_dm.iloc[0] = np.nan
    tr = true_range(high, low, close)
    tr.iloc[0] = np.nan
    atr_ = wilder(tr, n)
    plus_di = 100 * wilder(plus_dm, n) / atr_
    minus_di = 100 * wilder(minus_dm, n) / atr_
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return wilder(dx, n)


def donchian_high(high: pd.Series, n: int = 20) -> pd.Series:
    _check(n)
    return high.shift(1).rolling(n, min_periods=n).max()


def donchian_low(low: pd.Series, n: int = 20) -> pd.Series:
    _check(n)
    return low.shift(1).rolling(n, min_periods=n).min()


def sma(s: pd.Series, n: int) -> pd.Series:
    _check(n)
    return s.rolling(n, min_periods=n).mean()
