"""Research set v4 — sentiment (Crypto Fear & Greed) hypotheses on DAILY bars.
Declared 2026-11-19, before any v4 result was computed. Low turnover by design (fees killed v1–v3).

Timing / no look-ahead:
  daily bar D = [D 00:00, D+1 00:00) UTC, built only from complete 4H days.
  F&G value stamped D is used at bar D's CLOSE (= D+1 00:00), i.e. one full day after its stamp date
  begins; orders execute at bar D+1 open. Missing sentiment => no entry (never forward-invented).

S1  extreme_fear_buy    : enter when F&G <= 20; exit when F&G >= 60 or after 60 days. Disaster stop 3x ATR.
S2  fear_reversal       : F&G <= 25 within the last 7 days AND close > highest close of the prior 10 days;
                          exit when F&G >= 70 or close < SMA50. Stop 3x ATR.
S3  trend_without_euphoria: close > SMA100 and F&G < 75; exit when close < SMA100 or F&G >= 80. Stop 3x ATR.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators.core import atr, sma
from strategies.trend_breakout import resample_daily


def _with_fng(h4: pd.DataFrame, fng: pd.DataFrame) -> pd.DataFrame:
    d = resample_daily(h4)
    d["atr"] = atr(d["high"], d["low"], d["close"], 14)
    f = fng.rename(columns={"day_ts": "open_time", "value": "fng"})[["open_time", "fng"]]
    return d.merge(f, on="open_time", how="left")


def _common(fr):
    common = set.intersection(*(set(f["open_time"]) for f in fr.values()))
    return {s: f[f["open_time"].isin(common)].sort_values("open_time").reset_index(drop=True) for s, f in fr.items()}


def extreme_fear_buy(h4: dict, fng: pd.DataFrame, buy_le=20, exit_ge=60):
    out = {}
    for s, df in h4.items():
        d = _with_fng(df, fng)
        d["signal"] = (d["fng"] <= buy_le) & (d["volume"] > 0)
        d["exit_signal"] = d["fng"] >= exit_ge
        out[s] = d
    return _common(out)


def fear_reversal(h4: dict, fng: pd.DataFrame, fear_le=25, lookback=7, hh=10, exit_ge=70, sma_n=50):
    out = {}
    for s, df in h4.items():
        d = _with_fng(df, fng)
        recent_fear = (d["fng"] <= fear_le).astype(float).rolling(lookback, min_periods=1).max() > 0
        prior_high = d["close"].shift(1).rolling(hh, min_periods=hh).max()
        d["signal"] = recent_fear & (d["close"] > prior_high) & d["fng"].notna() & (d["volume"] > 0)
        d["exit_signal"] = (d["fng"] >= exit_ge) | (d["close"] < sma(d["close"], sma_n))
        out[s] = d
    return _common(out)


def trend_without_euphoria(h4: dict, fng: pd.DataFrame, sma_n=100, max_fng=75, exit_fng=80):
    out = {}
    for s, df in h4.items():
        d = _with_fng(df, fng)
        m = sma(d["close"], sma_n)
        d["signal"] = (d["close"] > m) & (d["fng"] < max_fng) & (d["volume"] > 0)
        d["exit_signal"] = (d["close"] < m) | (d["fng"] >= exit_fng)
        out[s] = d
    return _common(out)


def robustness(trades: pd.DataFrame, n_boot: int = 20000, seed: int = 0) -> dict:
    """Checks that a positive result is not one lucky trade or one lucky year."""
    if trades.empty:
        return {"n": 0}
    r = trades["r"].to_numpy()
    rng = np.random.default_rng(seed)
    boot = rng.choice(r, (n_boot, len(r))).mean(axis=1)
    srt = np.sort(r)
    yrs = pd.to_datetime(trades["entry_time"], unit="s").dt.year
    per_year = trades.groupby(yrs)["r"].mean()
    return {
        "n": int(len(r)),
        "expR": round(float(r.mean()), 3),
        "p_boot_le0": round(float((boot <= 0).mean()), 3),
        "expR_wo_top1": round(float(srt[:-1].mean()), 3) if len(r) > 1 else None,
        "expR_wo_top3": round(float(srt[:-3].mean()), 3) if len(r) > 3 else None,
        "years_positive": f"{int((per_year > 0).sum())}/{len(per_year)}",
        "per_year_expR": per_year.round(3).to_dict(),
        "per_symbol_expR": trades.groupby("symbol")["r"].mean().round(3).to_dict(),
    }
