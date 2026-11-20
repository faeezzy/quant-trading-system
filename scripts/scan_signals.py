"""Phase 4 diagnostic: how often does the baseline strategy fire on real data? (NOT a backtest)

Purpose:  condition funnel + signal counts per year, for 15m/1H and 1H/4H variants, under
          taker and maker cost assumptions. Answers: "do costs kill this strategy before we even
          simulate P&L?"
Inputs:   data/historical/candles.db (from scripts.backfill), config/strategy.yaml.
Outputs:  stdout tables + reports/signal_scan_<UTC>.json. No network, no orders.
Run:      python -m scripts.scan_signals
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.candle_store import CandleStore  # noqa: E402
from strategies.trend_breakout import Costs, Params, generate  # noqa: E402

VARIANTS = [("15", "60"), ("60", "240")]
COSTS = {
    # spread_pct ~ observed BTC 0.09% / ETH 0.01% on 2026-11-19; slippage is an assumption
    "taker": Costs(entry_fee_pct=0.2, exit_fee_pct=0.2, spread_pct=0.1, slippage_pct=0.05),
    "maker_entry": Costs(entry_fee_pct=0.1, exit_fee_pct=0.2, spread_pct=0.05, slippage_pct=0.03),
}
CONDS = ["warm", "c_trend", "c_adx", "c_not_abnormal_vol", "c_breakout", "c_volume", "c_has_market", "c_rr"]


def load(store: CandleStore, sym: str, tf: str) -> pd.DataFrame:
    df = pd.DataFrame(store.load(sym, tf)).rename(columns=str.lower).rename(columns={"time": "open_time"})
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


def params_from_cfg() -> Params:
    s = yaml.safe_load((ROOT / "config" / "strategy.yaml").read_text())
    i, e = s["indicators"], s["exit"]
    return Params(ema_fast=i["ema_fast"], ema_slow=i["ema_slow"], adx_period=i["adx_period"],
                  adx_min=i["adx_min"], atr_period=i["atr_period"], donchian=i["donchian_period"],
                  volume_sma=i["volume_sma"], stop_atr_mult=e["stop_atr_mult"],
                  min_net_rr=s["entry"]["min_reward_risk"], target_atr_mult=s["entry"]["target_atr_mult"])


def needed_atr_pct(p: Params, k: float) -> float | None:
    """Min ATR% for net R:R >= min_net_rr: A >= k(1+R)/(T - R*S). None = impossible."""
    d = p.target_atr_mult - p.min_net_rr * p.stop_atr_mult
    return k * (1 + p.min_net_rr) / d if d > 0 else None


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    store = CandleStore(ROOT / "data" / "historical" / "candles.db")
    base = params_from_cfg()
    targets = sorted({base.target_atr_mult, 4.0})
    report: dict = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "params": asdict(base),
                    "costs": {k: {**asdict(v), "round_trip_pct": v.round_trip_pct} for k, v in COSTS.items()},
                    "results": {}}
    print(f"params: {asdict(base)}\n")
    for m in cfg["markets"]:
        sym = m["symbol"]
        data = {tf: load(store, sym, tf) for tf in ("15", "60", "240")}
        if any(d.empty for d in data.values()):
            print(f"{sym}: no data — run scripts.backfill first"); return 1
        for sig_tf, reg_tf in VARIANTS:
          for tgt in targets:
            p = replace(base, target_atr_mult=tgt)
            for cname, costs in COSTS.items():
                out = generate(data[sig_tf], data[reg_tf], p, costs, sig_tf, reg_tf)
                w = out[out["warm"]]
                funnel, mask = {}, pd.Series(True, index=out.index)
                for c in CONDS:
                    mask &= out[c].fillna(False).astype(bool)
                    funnel[c] = int(mask.sum())
                sig = out[out["signal"]]
                years = pd.to_datetime(sig["open_time"], unit="s").dt.year.value_counts().sort_index()
                key = f"{sym}_{sig_tf}m/{reg_tf}m_T{tgt}_{cname}"
                need = needed_atr_pct(p, costs.round_trip_pct)
                report["results"][key] = {
                    "bars": int(len(w)), "signals": int(len(sig)), "funnel": funnel,
                    "per_year": {int(k): int(v) for k, v in years.items()},
                    "median_atr_pct": round(float(w["atr_pct"].median()), 4),
                    "needed_atr_pct": round(need, 3) if need else None,
                    "pct_bars_atr_enough": round(float((w["atr_pct"] >= need).mean() * 100), 2) if need else 0.0,
                    "median_net_rr_of_breakouts": round(float(out.loc[out["c_breakout"].fillna(False).astype(bool) & out["warm"], "net_rr"].median()), 3),
                }
                r = report["results"][key]
                print(f"{key:<36} cost {costs.round_trip_pct:.2f}%  ATR med {r['median_atr_pct']:.3f}% "
                      f"need >={r['needed_atr_pct']}% ({r['pct_bars_atr_enough']}% of bars)  "
                      f"breakouts {funnel['c_breakout']}  SIGNALS {r['signals']} {r['per_year']}")
    out_path = ROOT / "reports" / f"signal_scan_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    print(f"report: {out_path}")
    print("NOTE: signal counts are not profitability. P&L requires the Phase 6 backtest.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
