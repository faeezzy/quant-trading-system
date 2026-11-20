"""Research set v3 — three DIFFERENT hypotheses, declared 2026-11-19 before seeing any v3 result.

All builders return {symbol: frame} on a common bar grid with columns the backtest engine needs:
open, high, low, close, atr, signal (enter next bar), exit_signal (exit next bar open).
Every value at row t uses only data up to and including the CLOSE of row t.

D1  daily_trend      : long while daily close > SMA200; exit when close < SMA200.
                       Stop 3x daily ATR (disaster stop). Very low turnover.
D2  weekly_rotation  : each week (first complete UTC day of the ISO week) rank coins by 28-day return;
                       hold the top coin if its 28d return > 0 and close > SMA100; else cash.
                       Signal persists for the week so the engine can rotate after exiting.
D3  mr_4h_uptrend    : 4H mean reversion inside a daily uptrend: daily close > SMA200 (last COMPLETE day),
                       4H RSI(2) < 10 -> buy; exit when 4H close > SMA5 of 4H closes. Stop 2x 4H ATR.
Benchmark            : buy-and-hold each coin from the first usable bar (one entry fee, one exit fee).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators.core import atr, sma
from strategies.trend_breakout import resample_daily


def rsi(close: pd.Series, n: int = 2) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def _common(fr: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    common = set.intersection(*(set(f["open_time"]) for f in fr.values()))
    return {s: f[f["open_time"].isin(common)].sort_values("open_time").reset_index(drop=True) for s, f in fr.items()}


def _daily(h4: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out = {}
    for s, df in h4.items():
        d = resample_daily(df)
        d["atr"] = atr(d["high"], d["low"], d["close"], 14)
        out[s] = d
    return out


def daily_trend(h4: dict[str, pd.DataFrame], sma_n: int = 200) -> dict[str, pd.DataFrame]:
    fr = {}
    for s, d in _daily(h4).items():
        d = d.copy()
        m = sma(d["close"], sma_n)
        d["signal"] = (d["close"] > m) & (d["volume"] > 0)
        d["exit_signal"] = d["close"] < m
        fr[s] = d
    return _common(fr)


def weekly_rotation(h4: dict[str, pd.DataFrame], lookback: int = 28, sma_n: int = 100) -> dict[str, pd.DataFrame]:
    fr = _common(_daily(h4))
    syms = list(fr)
    t = fr[syms[0]]["open_time"]
    wk = pd.to_datetime(t, unit="s").dt.isocalendar()
    key = (wk["year"].astype(int) * 100 + wk["week"].astype(int)).to_numpy()
    first_of_week = np.r_[True, key[1:] != key[:-1]]
    mom = pd.DataFrame({s: fr[s]["close"] / fr[s]["close"].shift(lookback) - 1 for s in syms})
    ok = pd.DataFrame({s: fr[s]["close"] > sma(fr[s]["close"], sma_n) for s in syms})
    pick = []
    cur = None
    for i in range(len(t)):
        if first_of_week[i]:
            row = mom.iloc[i]
            cand = row[ok.iloc[i] & (row > 0)].dropna()
            cur = cand.idxmax() if len(cand) else None
        pick.append(cur)
    pick = pd.Series(pick)
    out = {}
    for s in syms:
        d = fr[s].copy()
        d["signal"] = (pick == s).to_numpy() & (d["volume"] > 0).to_numpy()
        d["exit_signal"] = (pick != s).to_numpy()
        out[s] = d
    return out


def mr_4h_uptrend(h4: dict[str, pd.DataFrame], rsi_n: int = 2, rsi_buy: float = 10, exit_sma: int = 5,
                  daily_sma: int = 200) -> dict[str, pd.DataFrame]:
    fr = {}
    days = _daily(h4)
    for s, df in h4.items():
        d = df.copy().sort_values("open_time").reset_index(drop=True)
        d["atr"] = atr(d["high"], d["low"], d["close"], 14)
        r = rsi(d["close"], rsi_n)
        dd = days[s].copy()
        dd["up"] = dd["close"] > sma(dd["close"], daily_sma)
        dd["close_time"] = dd["open_time"] + 86400
        d["close_time"] = d["open_time"] + 14400
        d = pd.merge_asof(d, dd[["close_time", "up"]], on="close_time", direction="backward")
        up = d["up"].astype("boolean").fillna(False).astype(bool)
        d["signal"] = up & (r < rsi_buy) & (d["volume"] > 0)
        d["exit_signal"] = d["close"] > sma(d["close"], exit_sma)
        fr[s] = d.drop(columns=["up", "close_time"])
    return _common(fr)


def buy_and_hold(frames: dict[str, pd.DataFrame], fee_pct: float = 0.2, spread_pct: float = 0.1,
                 start_time: int | None = None) -> dict[str, float]:
    """Return % per coin from start_time (or first bar) to last close, after one round trip of costs."""
    out = {}
    for s, f in frames.items():
        g = f if start_time is None else f[f["open_time"] >= start_time]
        if g.empty:
            continue
        buy = g["open"].iloc[0] * (1 + spread_pct / 200)
        sell = g["close"].iloc[-1] * (1 - spread_pct / 200)
        out[s] = round(((sell * (1 - fee_pct / 100)) / (buy * (1 + fee_pct / 100)) - 1) * 100, 2)
    return out
