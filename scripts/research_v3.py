"""Research v3: benchmark, breakout diagnostics, and three new hypotheses (D1–D3).

Run:  python -m scripts.research_v3 [--db path/to/candles.db]
Out:  stdout + reports/research_v3_<UTC>.json + trades CSVs. No network.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtesting.engine import BTConfig, metrics, run  # noqa: E402
from database.candle_store import CandleStore  # noqa: E402
from scripts.backtest import ALT_MAKER, ALT_TAKER, CORE, MAKER, TAKER, frames_for  # noqa: E402
from scripts.scan_signals import load, params_from_cfg  # noqa: E402
from strategies import v3  # noqa: E402

PRIOR_VARIANTS = 11
# name, builder, universe, entry, stop_atr, max_hold_bars
V3 = [
    ("D1_daily_trend", v3.daily_trend, "core", "taker", 3.0, 10_000),
    ("D1_daily_trend", v3.daily_trend, "research", "taker", 3.0, 10_000),
    ("D2_weekly_rotation", v3.weekly_rotation, "research", "taker", 3.0, 10_000),
    ("D3_mr_4h_uptrend", v3.mr_4h_uptrend, "core", "maker", 2.0, 60),
    ("D3_mr_4h_uptrend", v3.mr_4h_uptrend, "research", "maker", 2.0, 60),
]


def costs_of(entry):
    return lambda s: (MAKER if entry == "maker" else TAKER) if s in CORE else (ALT_MAKER if entry == "maker" else ALT_TAKER)


def evaluate(frames, entry, stop, hold):
    co = costs_of(entry)
    bt = BTConfig(entry=entry, exit_mode="none", stop_atr_mult=stop, max_hold_bars=hold, regime_exit=False,
                  costs=co("BTCIDR"), costs_by_symbol={s: co(s) for s in frames}, maker_fee_pct=0.1)
    res = run(frames, bt)
    m = metrics(res, bt.initial_equity)
    t = res.trades
    span = res.equity.index[-1] - res.equity.index[0]
    mid = res.equity.index[0] + span / 2
    halves = {}
    if len(t):
        et = pd.to_datetime(t["entry_time"], unit="s")
        t = t.assign(trade_ret_pct=(t["exit"] / t["entry"] - 1) * 100 - (t["fees"] / t["notional"] * 100))
        for h, sel in (("H1", et < mid), ("H2", et >= mid)):
            tt = t[sel]
            halves[h] = {"n": int(len(tt)), "expR": round(float(tt.r.mean()), 3) if len(tt) else None}
        m["avg_trade_ret_pct"] = round(float(t["trade_ret_pct"].mean()), 3)
        m["mfe_r_losers_median"] = round(float(t.loc[t.pnl <= 0, "mfe_r"].median()), 2) if (t.pnl <= 0).any() else None
        m["per_symbol"] = t.groupby("symbol")["r"].agg(["size", "mean"]).round(3).to_dict("index")
        m["time_in_market_pct"] = round(float(t["bars"].sum() / max(len(res.equity), 1) * 100), 1)
    verdict = ("CANDIDATE" if m["trades"] >= 200 and (m.get("profit_factor") or 0) > 1.2
               and halves and all((h["expR"] or -1) > 0 for h in halves.values())
               else ("PROMISING (low n)" if m["trades"] >= 20 and (m.get("profit_factor") or 0) > 1.2
                     and halves and all((h["expR"] or -1) > 0 for h in halves.values()) else "REJECT"))
    return res, m, halves, verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "historical" / "candles.db"))
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    store = CandleStore(a.db)
    uni = {"core": [m["symbol"] for m in cfg["markets"]], "research": [m["symbol"] for m in cfg["research_markets"]]}
    h4 = {s: load(store, s, "240") for s in uni["research"] if store.bounds(s, "240")[2] > 0}
    h1 = {s: load(store, s, "60") for s in uni["core"] if store.bounds(s, "60")[2] > 0}
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    rep: dict = {"generated_at_utc": stamp, "benchmark": {}, "diagnostics": {}, "v3": []}

    # --- benchmark
    print("== buy & hold (one round trip of costs) ==")
    d1 = v3.daily_trend({s: h4[s] for s in h4})
    warm = int(d1[next(iter(d1))].dropna(subset=["atr"]).loc[lambda x: x["signal"] | x["exit_signal"], "open_time"].min())
    for label, start in (("full period", None), ("from SMA200 warm-up", warm)):
        bh = v3.buy_and_hold(h4, start_time=start)
        rep["benchmark"][label] = bh
        print(f"  {label:<20} " + "  ".join(f"{s.replace('IDR', '')} {v:+.1f}%" for s, v in bh.items()))

    # --- breakout diagnostics (why did v1/v2 fail?)
    print("\n== breakout diagnostics (v1/v2 losers) ==")
    base = params_from_cfg()
    data = {s: {"60": h1[s], "240": h4[s]} for s in h1}
    for name, stf, rtf, trail in (("B_floor2_1H_maker", "60", "240", 2.0), ("C1_4H_1D_trail2_maker", "240", "1D", 2.0)):
        p = replace(base, rr_mode="cost_floor", cost_floor_mult=2.0)
        fr = frames_for(list(h1), data, stf, rtf, p, costs_of("maker"))
        bt = BTConfig(entry="maker", exit_mode="trail", trail_atr_mult=trail, costs=MAKER,
                      max_hold_bars=72 if stf == "60" else 180, maker_fee_pct=0.1)
        t = run(fr, bt).trades
        w, l = t[t.pnl > 0], t[t.pnl <= 0]
        dg = {"trades": len(t), "win_rate": round(len(w) / len(t) * 100, 1) if len(t) else None,
              "avg_win_r": round(float(w.r.mean()), 2) if len(w) else None,
              "avg_loss_r": round(float(l.r.mean()), 2) if len(l) else None,
              "losers_median_mfe_r": round(float(l.mfe_r.median()), 2) if len(l) else None,
              "losers_mfe_ge_1r_pct": round(float((l.mfe_r >= 1).mean() * 100), 1) if len(l) else None}
        rep["diagnostics"][name] = dg
        print(f"  {name:<24} {dg}")

    # --- v3
    print("\n== v3 hypotheses (declared before results) ==")
    hdr = f"{'variant':<20} {'universe':<9} {'n':>4} {'win%':>5} {'PF':>5} {'expR':>6} {'avg tr%':>7} {'inMkt%':>6} {'net IDR':>9} {'DD%':>5} | {'H1 n/expR':>11} {'H2 n/expR':>11}"
    print(hdr); print("-" * len(hdr))
    for name, builder, u, entry, stop, hold in V3:
        syms = [s for s in uni[u] if s in h4]
        frames = builder({s: h4[s] for s in syms})
        res, m, halves, verdict = evaluate(frames, entry, stop, hold)
        rep["v3"].append({"name": name, "universe": u, "symbols": syms, "metrics": m, "halves": halves, "verdict": verdict})
        if len(res.trades):
            res.trades.to_csv(ROOT / "reports" / f"trades_{name}_{u}_{stamp}.csv", index=False)
        f = lambda v, fmt: format(v, fmt) if v is not None else "-"  # noqa: E731
        h1s = halves.get("H1", {}); h2s = halves.get("H2", {})
        print(f"{name:<20} {u:<9} {m['trades']:>4} {f(m.get('win_rate'), '>5.1f')} {f(m.get('profit_factor'), '>5.2f')} "
              f"{f(m.get('expectancy_r'), '>6.3f')} {f(m.get('avg_trade_ret_pct'), '>7.2f')} {f(m.get('time_in_market_pct'), '>6.1f')} "
              f"{m['net_pnl']:>9,.0f} {m['max_dd_pct_initial']:>5.2f} | {h1s.get('n', 0):>4}/{f(h1s.get('expR'), '>6.3f')} "
              f"{h2s.get('n', 0):>4}/{f(h2s.get('expR'), '>6.3f')}  {verdict}")
    out = ROOT / "reports" / f"research_v3_{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nreport: {out}\nvariants tested so far (cumulative): {PRIOR_VARIANTS + len({v[0] for v in V3})}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
