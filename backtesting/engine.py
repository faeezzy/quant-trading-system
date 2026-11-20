"""Bar-by-bar portfolio backtester (spec Part 12) — long-only spot, max 1 concurrent position.

Purpose:  simulate the baseline strategy with realistic, CONSERVATIVE execution and spec risk rules.
Inputs:   {symbol: DataFrame from strategies.trend_breakout.generate()} on a common bar grid,
          BTConfig (capital, risk, exits, costs, entry type).
Outputs:  BTResult(trades DataFrame, equity Series, events list).

Execution assumptions (all conservative, all documented):
  * Signal on bar t CLOSE -> order for bar t+1. Never fills on the signal bar.
  * taker entry: fill at open[t+1] * (1 + spread/2 + slippage).
  * maker entry: limit at close[t]; fills ONLY if low[t+1] < limit (price must trade THROUGH,
    touching is not enough — spec 12.2). Fill = min(limit, open). Unfilled -> cancelled.
  * Stop (synthetic, monitored): if low <= stop -> exit at min(stop, open) * (1 - spread/2 - slippage)
    (gaps through the stop are filled at the open, i.e. worse than the stop).
  * Same-bar ambiguity: if both stop and target are inside one bar, STOP is assumed first.
    After an entry fill, the stop is checked on the same bar.
  * Target (fixed_target exits only): limit sell; fills if high > target, at target * (1 - spread/2).
  * Trailing stop: updated on bar CLOSE only, never lowered: stop = max(stop, close - k*ATR).
  * Regime exit (1H EMA fast < slow) and max-holding exits: market at next bar open.
  * Fees: entry fee on notional, exit fee on proceeds (taker rate unless stated).
Risk rules (spec Part 6): risk 0.25% of equity incl. estimated round-trip cost; notional capped by
  max allocation and cash; skip if below exchange minimum order; daily loss 1% and weekly loss 2%
  block NEW entries until next day/week; drawdown >= 5% of initial equity is RECORDED
  (research mode keeps running so the whole sample is visible; live mode would halt).
Not modelled (stated, not hidden): order-book depth / partial fills (orders here are far below
  observed top-of-book depth), exchange downtime beyond zero-volume bars, tax changes over time.
Tests:    tests/unit/test_backtest.py
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from strategies.trend_breakout import Costs


@dataclass(frozen=True)
class BTConfig:
    initial_equity: float = 1_000_000
    max_allocation: float = 500_000
    risk_pct: float = 0.25
    min_order_idr: float = 10_000
    entry: str = "taker"                  # "taker" | "maker"
    exit_mode: str = "trail"              # "trail" | "target" | "none" (stop + exit_signal only)
    stop_atr_mult: float = 1.5
    trail_atr_mult: float = 2.0
    target_atr_mult: float = 4.0
    max_hold_bars: int = 72
    regime_exit: bool = True
    daily_loss_pct: float = 1.0
    weekly_loss_pct: float = 2.0
    max_dd_pct_initial: float = 5.0
    costs: Costs = field(default_factory=Costs)
    maker_fee_pct: float = 0.1
    costs_by_symbol: dict | None = None   # optional per-symbol override (e.g. wider alt spreads)


@dataclass
class BTResult:
    trades: pd.DataFrame
    equity: pd.Series
    events: list[str]


def run(frames: dict[str, pd.DataFrame], cfg: BTConfig) -> BTResult:
    syms = list(frames)
    times = frames[syms[0]]["open_time"].to_numpy()
    for s in syms[1:]:
        if not np.array_equal(frames[s]["open_time"].to_numpy(), times):
            raise ValueError("all symbols must share the same bar grid")
    F = {}
    for s in syms:
        fr = frames[s]
        F[s] = {k: fr[k].to_numpy() for k in ("open", "high", "low", "close", "atr", "signal")}
        n = len(fr)
        F[s]["h_ema_fast"] = fr["h_ema_fast"].to_numpy() if "h_ema_fast" in fr else np.ones(n)
        F[s]["h_ema_slow"] = fr["h_ema_slow"].to_numpy() if "h_ema_slow" in fr else np.zeros(n)
        F[s]["exit_signal"] = fr["exit_signal"].astype("boolean").fillna(False).to_numpy(dtype=bool) if "exit_signal" in fr \
            else np.zeros(n, dtype=bool)

    def _k(sym):
        c = (cfg.costs_by_symbol or {}).get(sym, cfg.costs)
        fee_in = (cfg.maker_fee_pct if cfg.entry == "maker" else c.entry_fee_pct) / 100
        est_rt = c.round_trip_pct / 100 if cfg.entry == "taker" else \
            (c.round_trip_pct - (c.entry_fee_pct - cfg.maker_fee_pct)) / 100
        return {"half": c.spread_pct / 200, "slip": c.slippage_pct / 100, "fee_in": fee_in,
                "fee_out": c.exit_fee_pct / 100, "est_rt": est_rt}
    K = {s: _k(s) for s in syms}

    cash = cfg.initial_equity
    pos = None          # dict
    pending = None      # dict
    exit_next = False
    trades, equity, events = [], np.empty(len(times)), []
    peak = cfg.initial_equity
    dd_breached = False
    day_key = week_key = None
    day_start = week_start = cfg.initial_equity
    block_day = block_week = None

    def close_pos(i, price, reason):
        nonlocal cash, pos
        proceeds = pos["qty"] * price
        fee = proceeds * K[pos["sym"]]["fee_out"]
        cash += proceeds - fee
        pnl = proceeds - fee - pos["cost_basis"]
        trades.append({"symbol": pos["sym"], "entry_time": times[pos["i"]], "exit_time": times[i],
                       "entry": pos["entry"], "exit": price, "qty": pos["qty"], "notional": pos["notional"],
                       "init_stop": pos["init_stop"], "risk_idr": pos["risk_idr"], "pnl": pnl,
                       "r": pnl / pos["risk_idr"], "fees": pos["fee_in"] + fee, "bars": i - pos["i"],
                       "mfe_r": pos["qty"] * (pos["mfe"] - pos["entry"]) / pos["risk_idr"],
                       "reason": reason})
        pos = None

    for i in range(len(times)):
        t = pd.Timestamp(int(times[i]), unit="s")
        mark = cash + (pos["qty"] * F[pos["sym"]]["open"][i] if pos else 0.0)
        dk, wk = t.date(), (t.isocalendar().year, t.isocalendar().week)
        if dk != day_key:
            day_key, day_start = dk, mark
        if wk != week_key:
            week_key, week_start = wk, mark

        # 1) forced exit at open (regime / max hold decided on previous close)
        if pos and exit_next:
            f, k = F[pos["sym"]], K[pos["sym"]]
            close_pos(i, f["open"][i] * (1 - k["half"] - k["slip"]), exit_next)
            exit_next = False

        # 2) pending entry
        if pending and pos is None:
            s = pending["sym"]; f = F[s]; k = K[s]
            half, slip, fee_in, est_rt = k["half"], k["slip"], k["fee_in"], k["est_rt"]
            fill = None
            if cfg.entry == "taker":
                fill = f["open"][i] * (1 + half + slip)
            elif f["low"][i] < pending["limit"]:
                fill = min(pending["limit"], f["open"][i])
            if fill is not None:
                stop = fill - cfg.stop_atr_mult * pending["atr"]
                per_unit = (fill - stop) + fill * est_rt
                eq = cash
                qty = (eq * cfg.risk_pct / 100) / per_unit if per_unit > 0 else 0
                notional = min(qty * fill, cfg.max_allocation, cash / (1 + fee_in))
                qty = notional / fill
                if notional < cfg.min_order_idr or stop <= 0:
                    events.append(f"{t} skip {s}: notional {notional:,.0f} < min order or bad stop")
                else:
                    fee = notional * fee_in
                    cash -= notional + fee
                    pos = {"sym": s, "i": i, "qty": qty, "entry": fill, "stop": stop, "init_stop": stop,
                           "target": fill + cfg.target_atr_mult * pending["atr"], "notional": notional,
                           "cost_basis": notional + fee, "fee_in": fee,
                           "risk_idr": qty * (fill - stop) + notional * est_rt, "mfe": fill}
            pending = None

        # 3) manage open position on this bar
        if pos:
            f, k = F[pos["sym"]], K[pos["sym"]]
            pos["mfe"] = max(pos["mfe"], f["high"][i])
            if f["low"][i] <= pos["stop"]:
                close_pos(i, min(pos["stop"], f["open"][i]) * (1 - k["half"] - k["slip"]), "stop")
            elif cfg.exit_mode == "target" and f["high"][i] > pos["target"]:
                close_pos(i, pos["target"] * (1 - k["half"]), "target")
        if pos:
            f = F[pos["sym"]]
            if cfg.exit_mode == "trail" and not np.isnan(f["atr"][i]):
                pos["stop"] = max(pos["stop"], f["close"][i] - cfg.trail_atr_mult * f["atr"][i])
            if f["exit_signal"][i]:
                exit_next = "signal"
            elif cfg.regime_exit and f["h_ema_fast"][i] < f["h_ema_slow"][i]:
                exit_next = "regime"
            elif i - pos["i"] >= cfg.max_hold_bars:
                exit_next = "max_hold"

        # 4) equity, limits
        eq = cash + (pos["qty"] * F[pos["sym"]]["close"][i] if pos else 0.0)
        equity[i] = eq
        peak = max(peak, eq)
        if not dd_breached and peak - eq >= cfg.max_dd_pct_initial / 100 * cfg.initial_equity:
            dd_breached = True
            events.append(f"{t} MAX DRAWDOWN {cfg.max_dd_pct_initial}% of initial breached (live bot would halt)")
        if eq <= day_start * (1 - cfg.daily_loss_pct / 100) and block_day != day_key:
            block_day = day_key; events.append(f"{t} daily loss limit hit")
        if eq <= week_start * (1 - cfg.weekly_loss_pct / 100) and block_week != week_key:
            block_week = week_key; events.append(f"{t} weekly loss limit hit")

        # 5) new signal -> order for next bar
        blocked = block_day == day_key or block_week == week_key
        if pos is None and pending is None and not blocked and i + 1 < len(times):
            for s in syms:
                if F[s]["signal"][i]:
                    pending = {"sym": s, "limit": F[s]["close"][i], "atr": F[s]["atr"][i]}
                    break

    if pos:
        k = K[pos["sym"]]
        close_pos(len(times) - 1, F[pos["sym"]]["close"][-1] * (1 - k["half"] - k["slip"]), "end_of_data")
        equity[-1] = cash
    eq_series = pd.Series(equity, index=pd.to_datetime(times, unit="s"))
    return BTResult(pd.DataFrame(trades), eq_series, events)


def metrics(res: BTResult, initial: float) -> dict:
    t, eq = res.trades, res.equity
    out = {"trades": int(len(t))}
    dd = (eq.cummax() - eq)
    out["max_dd_idr"] = round(float(dd.max()), 0)
    out["max_dd_pct_initial"] = round(float(dd.max() / initial * 100), 2)
    out["net_pnl"] = round(float(eq.iloc[-1] - initial), 0)
    out["return_pct"] = round(float((eq.iloc[-1] / initial - 1) * 100), 2)
    if len(t) == 0:
        return out
    wins, losses = t[t.pnl > 0], t[t.pnl <= 0]
    out.update({
        "win_rate": round(len(wins) / len(t) * 100, 1),
        "avg_win_r": round(float(wins.r.mean()), 2) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.r.mean()), 2) if len(losses) else 0.0,
        "expectancy_r": round(float(t.r.mean()), 3),
        "expectancy_idr": round(float(t.pnl.mean()), 0),
        "profit_factor": round(float(wins.pnl.sum() / -losses.pnl.sum()), 2) if len(losses) and losses.pnl.sum() < 0 else None,
        "fees_idr": round(float(t.fees.sum()), 0),
        "avg_bars_held": round(float(t.bars.mean()), 1),
        "exit_reasons": t.reason.value_counts().to_dict(),
    })
    daily = eq.resample("1D").last().pct_change().dropna()
    out["sharpe"] = round(float(daily.mean() / daily.std() * np.sqrt(365)), 2) if daily.std() > 0 else None
    return out
