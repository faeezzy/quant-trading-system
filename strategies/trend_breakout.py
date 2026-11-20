"""Baseline trend-following breakout signals (spec Part 5) — research/backtest use only.

Purpose:  turn closed candles into candidate long entries with stop/target and a cost check.
Inputs:   signal-TF candles (default 15m) and regime-TF candles (default 1H) as DataFrames with
          columns open_time, open, high, low, close, volume (floats, ascending, closed candles only);
          params from config/strategy.yaml; cost assumptions (fees, spread, slippage).
Outputs:  DataFrame of signal-TF rows with indicators, per-condition booleans, `signal` flag,
          entry_ref, stop, target, net_rr. One row per closed signal candle.
No look-ahead: a 1H candle opening at T is only usable from T+1h (its close). It is joined to a
          15m candle by CLOSE time: 15m candle closing at C sees the last 1H candle that closed <= C.
Scope:    signals only. Position sizing, risk limits and execution live elsewhere (spec 8.1).
Open design question (flagged, not decided): the spec requires reward/risk >= 1.8 but defines
          no profit target for a trailing-stop system. Here a nominal target = entry + target_atr_mult*ATR
          is used ONLY for the ex-ante cost-aware R:R filter. Whether exits use it is a backtest choice.
Tests:    tests/unit/test_strategy.py
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from indicators.core import adx, atr, donchian_high, ema, sma

TF_SEC = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}


@dataclass(frozen=True)
class Params:
    ema_fast: int = 50
    ema_slow: int = 200
    adx_period: int = 14
    adx_min: float = 20.0
    atr_period: int = 14
    donchian: int = 20
    volume_sma: int = 20
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 3.0          # hypothesis — gross R:R 2.0 before costs
    min_net_rr: float = 1.8
    max_atr_pct_ratio: float = 3.0        # "abnormal volatility": ATR% > 3x its rolling median
    atr_pct_median_window: int = 500
    rr_mode: str = "fixed_target"         # "fixed_target" (spec 5.3 #5 literal) | "cost_floor" (trailing systems)
    cost_floor_mult: float = 2.0          # cost_floor: stop distance must be >= this x round-trip cost


@dataclass(frozen=True)
class Costs:
    entry_fee_pct: float = 0.2            # taker by default (conservative)
    exit_fee_pct: float = 0.2
    spread_pct: float = 0.1               # paid ~half on entry, half on exit
    slippage_pct: float = 0.05            # per side

    @property
    def round_trip_pct(self) -> float:
        return self.entry_fee_pct + self.exit_fee_pct + self.spread_pct + 2 * self.slippage_pct


def resample_daily(h4: pd.DataFrame) -> pd.DataFrame:
    """Build UTC daily candles from 4H candles. Only COMPLETE days (all 6 bars) are kept,
    so the still-forming day never leaks into the regime."""
    d = h4.copy()
    d["day"] = (d["open_time"] // 86400) * 86400
    g = d.groupby("day")
    out = pd.DataFrame({"open_time": g["open_time"].min().index, "open": g["open"].first().to_numpy(),
                        "high": g["high"].max().to_numpy(), "low": g["low"].min().to_numpy(),
                        "close": g["close"].last().to_numpy(), "volume": g["volume"].sum().to_numpy(),
                        "n": g["open_time"].count().to_numpy()})
    return out[out["n"] == 6].drop(columns="n").reset_index(drop=True)


def regime_frame(h: pd.DataFrame, p: Params, tf: str = "60") -> pd.DataFrame:
    out = pd.DataFrame({"close_time": h["open_time"] + TF_SEC[tf]})
    out["h_ema_fast"] = ema(h["close"], p.ema_fast).to_numpy()
    out["h_ema_slow"] = ema(h["close"], p.ema_slow).to_numpy()
    out["h_adx"] = adx(h["high"], h["low"], h["close"], p.adx_period).to_numpy()
    out["h_zero_vol"] = (h["volume"] <= 0).to_numpy()
    return out


def generate(sig: pd.DataFrame, reg: pd.DataFrame, p: Params = Params(), c: Costs = Costs(),
             sig_tf: str = "15", reg_tf: str = "60") -> pd.DataFrame:
    df = sig.copy().reset_index(drop=True)
    df["close_time"] = df["open_time"] + TF_SEC[sig_tf]
    df["atr"] = atr(df["high"], df["low"], df["close"], p.atr_period)
    df["dc_high"] = donchian_high(df["high"], p.donchian)
    df["vol_sma"] = sma(df["volume"], p.volume_sma)
    df["atr_pct"] = df["atr"] / df["close"] * 100
    df["atr_pct_med"] = df["atr_pct"].rolling(p.atr_pct_median_window, min_periods=p.atr_pct_median_window // 5).median()

    r = regime_frame(reg, p, reg_tf).sort_values("close_time")
    df = pd.merge_asof(df.sort_values("close_time"), r, on="close_time", direction="backward")

    # --- conditions (spec 5.2 / 5.3) ---
    df["c_trend"] = df["h_ema_fast"] > df["h_ema_slow"]
    df["c_adx"] = df["h_adx"] > p.adx_min
    df["c_not_abnormal_vol"] = df["atr_pct"] <= p.max_atr_pct_ratio * df["atr_pct_med"]
    df["c_breakout"] = df["close"] > df["dc_high"]
    df["c_volume"] = df["volume"] > df["vol_sma"]
    h_zero = df["h_zero_vol"].astype("boolean").fillna(True).astype(bool)
    df["c_has_market"] = (df["volume"] > 0) & ~h_zero

    df["entry_ref"] = df["close"]
    df["stop"] = df["close"] - p.stop_atr_mult * df["atr"]
    df["target"] = df["close"] + p.target_atr_mult * df["atr"]
    cost_idr = df["entry_ref"] * c.round_trip_pct / 100
    reward = df["target"] - df["entry_ref"] - cost_idr
    risk = df["entry_ref"] - df["stop"] + cost_idr
    df["net_rr"] = np.where(risk > 0, reward / risk, np.nan)
    if p.rr_mode == "fixed_target":
        df["c_rr"] = df["net_rr"] >= p.min_net_rr
    elif p.rr_mode == "cost_floor":
        stop_dist_pct = (df["entry_ref"] - df["stop"]) / df["entry_ref"] * 100
        df["c_rr"] = stop_dist_pct >= p.cost_floor_mult * c.round_trip_pct
    else:
        raise ValueError(f"unknown rr_mode {p.rr_mode!r}")

    conds = ["c_trend", "c_adx", "c_not_abnormal_vol", "c_breakout", "c_volume", "c_has_market", "c_rr"]
    df["warm"] = df[["atr", "dc_high", "vol_sma", "h_ema_slow", "h_adx", "atr_pct_med"]].notna().all(axis=1)
    df["signal"] = df["warm"] & df[conds].fillna(False).all(axis=1)
    return df
