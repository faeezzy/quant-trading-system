"""Research v4: sentiment hypotheses + stricter robustness, on the longest available history.

Run:  python -m scripts.research_v4 [--db path]
Verdict ladder (all must hold for the level):
  PROMISING : n>=40, PF>1.3, expR>0 in both halves, bootstrap P(meanR<=0)<0.05,
              expR without top-3 trades > 0, >=60% of years positive
  CANDIDATE : PROMISING and n>=200 (spec 12.4)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtesting.engine import BTConfig, metrics, run  # noqa: E402
from data.collectors import sentiment  # noqa: E402
from database.candle_store import CandleStore  # noqa: E402
from scripts.research_v3 import costs_of  # noqa: E402
from scripts.scan_signals import load  # noqa: E402
from strategies import v3, v4  # noqa: E402

PRIOR_VARIANTS = 14
V4 = [  # name, builder(h4, fng), universe, entry, stop, hold_bars (daily bars)
    (n, b, u, "taker", 3.0, h)
    for n, b, h in (("S1_extreme_fear_buy", v4.extreme_fear_buy, 60),
                    ("S2_fear_reversal", v4.fear_reversal, 10_000),
                    ("S3_trend_no_euphoria", v4.trend_without_euphoria, 10_000))
    for u in ("core", "long6", "all7")
]
RETEST = [  # long-history re-test of earlier ideas (NOT new variants; same pre-declared params)
    (n, b, u, e, st, h)
    for n, b, e, st, h in (("D1_daily_trend", v3.daily_trend, "taker", 3.0, 10_000),
                           ("D2_weekly_rotation", v3.weekly_rotation, "taker", 3.0, 10_000),
                           ("D3_mr_4h_uptrend", v3.mr_4h_uptrend, "maker", 2.0, 60))
    for u in ("core", "long6")
]


def verdict(m, halves, rb):
    ok = (rb.get("n", 0) >= 40 and (m.get("profit_factor") or 0) > 1.3
          and all((h.get("expR") or -1) > 0 for h in halves.values()) and halves
          and rb["p_boot_le0"] < 0.05 and (rb.get("expR_wo_top3") or -1) > 0
          and int(rb["years_positive"].split("/")[0]) >= 0.6 * int(rb["years_positive"].split("/")[1]))
    return ("CANDIDATE" if ok and rb["n"] >= 200 else "PROMISING") if ok else "REJECT"


def evaluate(frames, entry, stop, hold):
    co = costs_of(entry)
    bt = BTConfig(entry=entry, exit_mode="none", stop_atr_mult=stop, max_hold_bars=hold, regime_exit=False,
                  costs=co("BTCIDR"), costs_by_symbol={s: co(s) for s in frames}, maker_fee_pct=0.1)
    res = run(frames, bt)
    m = metrics(res, bt.initial_equity)
    t = res.trades
    halves = {}
    if len(t):
        span = res.equity.index[-1] - res.equity.index[0]
        mid = res.equity.index[0] + span / 2
        et = pd.to_datetime(t["entry_time"], unit="s")
        for h, sel in (("H1", et < mid), ("H2", et >= mid)):
            halves[h] = {"n": int(sel.sum()), "expR": round(float(t[sel].r.mean()), 3) if sel.any() else None}
        m["avg_trade_ret_pct"] = round(float(((t["exit"] / t["entry"] - 1) * 100 - t["fees"] / t["notional"] * 100).mean()), 2)
    rb = v4.robustness(t)
    return res, m, halves, rb, verdict(m, halves, rb) if len(t) else "REJECT"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "historical" / "candles.db"))
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    store = CandleStore(a.db)
    fng = pd.DataFrame(sentiment.load(store.conn), columns=["day_ts", "value"])
    allr = [m["symbol"] for m in cfg["research_markets"]]
    uni = {"core": [m["symbol"] for m in cfg["markets"]], "long6": [s for s in allr if s != "SOLIDR"], "all7": allr}
    h4 = {s: load(store, s, "240") for s in uni["all7"] if store.bounds(s, "240")[2] > 0}
    print("data:")
    for s, d in h4.items():
        print(f"  {s:<8} 4H {pd.to_datetime(d.open_time.min(), unit='s'):%Y-%m-%d} → {pd.to_datetime(d.open_time.max(), unit='s'):%Y-%m-%d}  ({len(d)} bars)")
    if fng.empty:
        print("  NO SENTIMENT DATA — run scripts.fetch_research_data on the VM"); return 1
    print(f"  F&G     {pd.to_datetime(fng.day_ts.min(), unit='s'):%Y-%m-%d} → {pd.to_datetime(fng.day_ts.max(), unit='s'):%Y-%m-%d}  ({len(fng)} days)")
    print("\n  buy&hold full period: " + "  ".join(f"{s.replace('IDR', '')} {v:+.0f}%" for s, v in v3.buy_and_hold(h4).items()))

    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    rep = {"generated_at_utc": stamp, "results": []}
    # baseline from the spec (Donchian breakout 4H/1D, trailing) on the long history
    from dataclasses import replace as _r
    from scripts.backtest import MAKER, frames_for
    from scripts.scan_signals import params_from_cfg
    print("\nspec baseline breakout C1 (4H/1D, trail 2xATR, maker) on long history:")
    for u in ("core", "long6"):
        p = _r(params_from_cfg(), rr_mode="cost_floor", cost_floor_mult=2.0)
        fr = frames_for(uni[u], {s: {"240": h4[s]} for s in uni[u]}, "240", "1D", p, costs_of("maker"))
        co = costs_of("maker")
        r0 = run(fr, BTConfig(entry="maker", exit_mode="trail", trail_atr_mult=2.0, max_hold_bars=180,
                              costs=co("BTCIDR"), costs_by_symbol={s: co(s) for s in fr}, maker_fee_pct=0.1))
        m0, rb0 = metrics(r0, 1_000_000), v4.robustness(r0.trades)
        print(f"  {u:<6} n={m0['trades']} PF={m0.get('profit_factor')} expR={m0.get('expectancy_r')} "
              f"years+={rb0.get('years_positive')} p≤0={rb0.get('p_boot_le0')}")
        rep.setdefault("baseline", {})[u] = {"metrics": m0, "robustness": rb0}

    hdr = f"\n{'variant':<22} {'univ':<8} {'n':>4} {'win%':>5} {'PF':>5} {'expR':>6} {'p≤0':>5} {'w/o top3':>8} {'yrs+':>5} {'avgTr%':>6} {'DD%':>5} | {'H1':>11} {'H2':>11}  verdict"
    print(hdr); print("-" * len(hdr))
    for label, items, needs_fng in (("v4", V4, True), ("retest", RETEST, False)):
        for name, builder, u, entry, stop, hold in items:
            syms = [s for s in uni[u] if s in h4]
            frames = builder({s: h4[s] for s in syms}, fng) if needs_fng else builder({s: h4[s] for s in syms})
            res, m, halves, rb, vd = evaluate(frames, entry, stop, hold)
            rep["results"].append({"set": label, "name": name, "universe": u, "metrics": m, "halves": halves,
                                   "robustness": rb, "verdict": vd, "first_bar": str(res.equity.index[0])})
            if len(res.trades):
                res.trades.to_csv(ROOT / "reports" / f"trades_{name}_{u}_{stamp}.csv", index=False)
            f = lambda v, fmt: format(v, fmt) if v is not None else "-"  # noqa: E731
            h1, h2 = halves.get("H1", {}), halves.get("H2", {})
            print(f"{name:<22} {u:<8} {m['trades']:>4} {f(m.get('win_rate'), '>5.1f')} {f(m.get('profit_factor'), '>5.2f')} "
                  f"{f(m.get('expectancy_r'), '>6.3f')} {f(rb.get('p_boot_le0'), '>5.2f')} {f(rb.get('expR_wo_top3'), '>8.3f')} "
                  f"{rb.get('years_positive', '-'):>5} {f(m.get('avg_trade_ret_pct'), '>6.2f')} {m['max_dd_pct_initial']:>5.2f} | "
                  f"{h1.get('n', 0):>4}/{f(h1.get('expR'), '>6.3f')} {h2.get('n', 0):>4}/{f(h2.get('expR'), '>6.3f')}  {vd}")
    out = ROOT / "reports" / f"research_v4_{stamp}.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nreport: {out}\nvariants tested so far (cumulative): {PRIOR_VARIANTS + len({v[0] for v in V4})}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
