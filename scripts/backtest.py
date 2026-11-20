"""Phase 6: backtest strategy variants on INDODAX history (portfolio, max 1 concurrent position).

Run:     python -m scripts.backtest                      # set v1 (original 7 variants), BTC+ETH
         python -m scripts.backtest --set v2             # set v2 (4H/1D, long holds), BTC+ETH
         python -m scripts.backtest --set v2 --universe research   # v2 on research_markets (4H data)
Output:  stdout table + reports/backtest_<set>_<universe>_<UTC>.json + trades CSVs.
Reading: CANDIDATE = >=200 trades, PF>1.2, expR>0 in BOTH halves (spec 12.4). Variant sets are
         declared BEFORE looking at results; nothing is tuned on this data. Every run prints the
         cumulative number of variants tested so far (multiple-testing awareness).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtesting.engine import BTConfig, metrics, run  # noqa: E402
from database.candle_store import CandleStore  # noqa: E402
from scripts.scan_signals import load, params_from_cfg  # noqa: E402
from strategies.trend_breakout import Costs, generate, resample_daily  # noqa: E402

TAKER = Costs(entry_fee_pct=0.2, exit_fee_pct=0.2, spread_pct=0.1, slippage_pct=0.05)
MAKER = Costs(entry_fee_pct=0.1, exit_fee_pct=0.2, spread_pct=0.1, slippage_pct=0.05)
# Alts: conservative ASSUMPTION (wider spread, more slippage) until measured per pair.
ALT_TAKER = replace(TAKER, spread_pct=0.3, slippage_pct=0.1)
ALT_MAKER = replace(MAKER, spread_pct=0.3, slippage_pct=0.1)
CORE = {"BTCIDR", "ETHIDR"}

# name, sig tf, regime tf, rr_mode, floor|target, entry, exit, trail_atr, max_hold_hours
SETS = {
    "v1": [
        ("A_target4_1H_taker", "60", "240", "fixed_target", 4.0, "taker", "target", 2.0, 72),
        ("A_target4_1H_maker", "60", "240", "fixed_target", 4.0, "maker", "target", 2.0, 72),
        ("B_floor2_1H_taker", "60", "240", "cost_floor", 2.0, "taker", "trail", 2.0, 72),
        ("B_floor2_1H_maker", "60", "240", "cost_floor", 2.0, "maker", "trail", 2.0, 72),
        ("B_floor1_1H_maker", "60", "240", "cost_floor", 1.0, "maker", "trail", 2.0, 72),
        ("B_floor2_15m_maker", "15", "60", "cost_floor", 2.0, "maker", "trail", 2.0, 72),
        ("B_floor1_15m_maker", "15", "60", "cost_floor", 1.0, "maker", "trail", 2.0, 72),
    ],
    # declared 2026-11-19 before any v2 result was seen
    "v2": [
        ("C1_4H_1D_trail2_maker", "240", "1D", "cost_floor", 2.0, "maker", "trail", 2.0, 720),
        ("C2_4H_1D_trail3_maker", "240", "1D", "cost_floor", 2.0, "maker", "trail", 3.0, 720),
        ("C3_4H_1D_trail3_taker", "240", "1D", "cost_floor", 2.0, "taker", "trail", 3.0, 720),
        ("C4_1H_4H_trail3_maker_long", "60", "240", "cost_floor", 2.0, "maker", "trail", 3.0, 720),
    ],
}
PREVIOUSLY_TESTED = {"v1": 0, "v2": 7}
BARS_PER_HOUR = {"15": 4, "60": 1, "240": 0.25}


def frames_for(syms, data, stf, rtf, p, costs_of):
    fr = {}
    for s in syms:
        if stf not in data[s] or (rtf != "1D" and rtf not in data[s]):
            continue
        reg = resample_daily(data[s]["240"]) if rtf == "1D" else data[s][rtf]
        fr[s] = generate(data[s][stf], reg, p, costs_of(s), stf, rtf)
    if not fr:
        return fr
    common = set.intersection(*(set(f["open_time"]) for f in fr.values()))
    return {s: f[f["open_time"].isin(common)].reset_index(drop=True) for s, f in fr.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="v1", choices=list(SETS))
    ap.add_argument("--universe", default="core", choices=["core", "research"])
    a = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    strat = yaml.safe_load((ROOT / "config" / "strategy.yaml").read_text())
    markets = cfg["research_markets"] if a.universe == "research" else cfg["markets"]
    store = CandleStore(ROOT / "data" / "historical" / "candles.db")
    data: dict = {}
    for m in markets:
        s = m["symbol"]
        data[s] = {}
        for tf in ("15", "60", "240"):
            if store.bounds(s, tf)[2] > 0:
                data[s][tf] = load(store, s, tf)
    missing = [s for s in data if "240" not in data[s]]
    if missing:
        print(f"no 4H data for {missing}: run  python -m scripts.backfill --research --tfs 240")
    syms = [s for s in data if "240" in data[s]]
    base = params_from_cfg()
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    report: dict = {"generated_at_utc": stamp, "set": a.set, "universe": a.universe, "variants": {}}
    print(f"universe {a.universe}: {syms}")

    hdr = (f"{'variant':<27} {'trades':>6} {'/yr':>4} {'win%':>5} {'PF':>5} {'expR':>6} {'net IDR':>9} "
           f"{'ret%':>6} {'maxDD%':>6} {'fees':>8} {'gross':>8} | {'H1 n/expR':>11} {'H2 n/expR':>11}")
    print(hdr); print("-" * len(hdr))
    for name, stf, rtf, mode, k, entry, exit_mode, trail, hold_h in SETS[a.set]:
        t0 = time.time()
        costs_of = (lambda s, e=entry: (MAKER if e == "maker" else TAKER) if s in CORE
                    else (ALT_MAKER if e == "maker" else ALT_TAKER))
        p = replace(base, rr_mode=mode, cost_floor_mult=k if mode == "cost_floor" else base.cost_floor_mult,
                    target_atr_mult=k if mode == "fixed_target" else base.target_atr_mult)
        frames = frames_for(syms, data, stf, rtf, p, costs_of)
        if not frames:
            print(f"{name:<27} skipped: no {stf} data for this universe"); continue
        bt = BTConfig(entry=entry, exit_mode=exit_mode, costs=costs_of("BTCIDR"),
                      costs_by_symbol={s: costs_of(s) for s in frames}, stop_atr_mult=p.stop_atr_mult,
                      trail_atr_mult=trail, target_atr_mult=p.target_atr_mult,
                      max_hold_bars=int(hold_h * BARS_PER_HOUR[stf]), maker_fee_pct=0.1)
        res = run(frames, bt)
        m = metrics(res, bt.initial_equity)
        span = res.equity.index[-1] - res.equity.index[0]
        years = max(span.days / 365.25, 1e-9)
        mid = res.equity.index[0] + span / 2
        halves = {}
        et = pd.to_datetime(res.trades["entry_time"], unit="s") if len(res.trades) else None
        for h, first in (("H1", True), ("H2", False)):
            tt = res.trades[(et < mid) if first else (et >= mid)] if len(res.trades) else res.trades
            halves[h] = {"trades": int(len(tt)), "expectancy_r": round(float(tt.r.mean()), 3) if len(tt) else None}
        gross = m["net_pnl"] + m.get("fees_idr", 0)
        m["gross_before_exchange_fees"] = gross
        m["per_symbol"] = (res.trades.groupby("symbol").agg(n=("r", "size"), expR=("r", "mean"), pnl=("pnl", "sum"))
                           .round(3).to_dict("index") if len(res.trades) else {})
        m["per_year"] = et.dt.year.value_counts().sort_index().to_dict() if et is not None else {}
        verdict = ("CANDIDATE" if m["trades"] >= 200 and (m.get("profit_factor") or 0) > 1.2
                   and all((halves[h]["expectancy_r"] or -1) > 0 for h in halves) else "REJECT")
        report["variants"][name] = {"symbols": list(frames), "first_bar": str(res.equity.index[0]),
                                    "config": {**asdict(bt), "costs": asdict(bt.costs)}, "params": asdict(p),
                                    "metrics": m, "halves": halves, "events": res.events[:50], "verdict": verdict}
        if len(res.trades):
            res.trades.to_csv(ROOT / "reports" / f"trades_{name}_{a.universe}_{stamp}.csv", index=False)
        f = lambda v, fmt: format(v, fmt) if v is not None else "-"  # noqa: E731
        print(f"{name:<27} {m['trades']:>6} {m['trades'] / years:>4.0f} {f(m.get('win_rate'), '>5.1f')} "
              f"{f(m.get('profit_factor'), '>5.2f')} {f(m.get('expectancy_r'), '>6.3f')} {m['net_pnl']:>9,.0f} "
              f"{m['return_pct']:>6.2f} {m['max_dd_pct_initial']:>6.2f} {m.get('fees_idr', 0):>8,.0f} {gross:>8,.0f} | "
              f"{halves['H1']['trades']:>4}/{f(halves['H1']['expectancy_r'], '>6.3f')} "
              f"{halves['H2']['trades']:>4}/{f(halves['H2']['expectancy_r'], '>6.3f')}  {verdict} ({time.time() - t0:.0f}s)")
        if m.get("exit_reasons"):
            print(f"{'':<27} exits {m['exit_reasons']}  from {res.equity.index[0]:%Y-%m-%d}")
    out = ROOT / "reports" / f"backtest_{a.set}_{a.universe}_{stamp}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nreport: {out}")
    print(f"variants tested so far (cumulative, incl. this run): {PREVIOUSLY_TESTED[a.set] + len(SETS[a.set])}"
          + ("" if a.universe == "core" else " (+ universe re-run)"))
    print("expR: avg result per trade in R after costs. gross = net + exchange fees (spread/slippage still in).")
    print("CANDIDATE needs >=200 trades, PF>1.2, expR>0 in BOTH halves. Backtest != live.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
