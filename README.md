# INDODAX Quant Trading System

Status: **Milestone 1 verified live (2026-11-19). Phase 2: private read-only client.** No orders, `trading_enabled: false`.

## Quick start (Ubuntu / WSL2)
```bash
bash scripts/env_check.sh                 # environment assessment (changes nothing)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                       # expect: 20 passed
python -m scripts.check_markets           # live public data, BTCIDR + ETHIDR
```
Exit codes: `0` all markets valid · `1` a market is DISABLED (see `!` lines) · `2` exchange unreachable.
Each run writes `reports/market_check_<UTC>.json` with the raw exchange payloads and every validation result.

## Strategy research status (2026-11-19)
17 pre-declared variants were tested on INDODAX data with conservative fills and full costs.
- Rejected: Donchian breakout (15m/1H/4H), daily SMA200 trend, 4H mean reversion, and three Fear & Greed sentiment rules.
- **PROMISING: D2 weekly momentum rotation on BTC/ETH** (`strategies/v3.py`, params lookback 28 / SMA100 / 3×ATR stop):
  - 2018-11 → 2026-11: 75 trades, PF 2.87, expR +0.57, bootstrap P(mean ≤ 0) = 0.002, positive without the top-3 trades, 6/8 years positive.
  - True out-of-sample (2018–2023, never seen when the rule was declared): expR +0.82.
  - All 30 neighbouring parameter sets are positive.
  - Weak years: 2022 and 2026.
  - Not CANDIDATE under spec 12.4, because 75 trades is fewer than 200 (low turnover by design).
  - At 0.25% risk it makes about 1.3%/yr with max DD about 3%. Sharpe is about 1.1 at any risk level.

## Paper trading (D2, UNVALIDATED LIVE)
```bash
python -m scripts.paper_trade        # run hourly via cron; idempotent, single-instance lock
crontab -e   # add:  5 * * * * cd $HOME/quant-trading-system && .venv/bin/python -m scripts.paper_trade >> logs/paper_cron.log 2>&1
```
State lives in `data/paper.db`, and one line per run goes to `reports/paper_daily.csv`. The stop is synthetic, checked against the live bid on every run.
Live trading stays OFF until at least 4–8 weeks of paper trading match the backtest (spec 12.5).

## Phase 2: private read-only
```bash
cp .env.example .env && chmod 600 .env   # fill INDODAX_API_KEY / INDODAX_API_SECRET (TAPIv2 key, Reading only)
python -m scripts.check_account --days 30
```
Exit codes: `0` OK · `2` auth/exchange error · `3` **UNSAFE: withdrawal permission enabled → revoke key**.
It checks `canTrade/canWithdraw/canWithdrawFiat`, lists non-zero balances and open orders, and computes the
**effective fee incl. tax** from real fills (`myTrades.commission`). Output goes to `reports/private/` (git-ignored).
The client only allows 5 GET paths. Withdraw, order and cancel are impossible by construction.

## What it checks (spec 3.5 / 4.3)
| Check | Source |
|---|---|
| Clock skew vs exchange | `GET /api/server_time` |
| Min order (IDR & coin), precision, maker/taker fee, maintenance/suspended | `GET /api/pairs` |
| Tick size | `GET /api/price_increments` |
| Best bid/ask, spread, crossed book | `GET /api/v2/ticker/bookTicker` |
| Order-book depth (IDR value), sort order | `GET /api/v2/depth` |
| 15m/1h/4h OHLCV: gaps, duplicates, impossible OHLC, stale, open candle | `GET /tradingview/history_v2` |

Fees and minimum order sizes come from the API on every run and are never hardcoded.

## Findings from the official docs (read 2026-11-19)
- **No native stop orders in Trade API V2.** `POST /api/v2/order` accepts only `LIMIT`/`MARKET`; `STOP_LIMIT` returns `stop_limit_not_available`. The stop-loss must therefore be a **locally monitored synthetic stop** (spec 5.5). It must never be described as exchange-held.
- Trade API V2 public has no candles or pair metadata, so this milestone uses the legacy public endpoints for those (both documented).
- Rate limits: legacy public 180/min, V2 public 300/min. The client throttles itself to about 120/min.
- `/api/pairs` returns `trade_fee_percent_maker/taker` per pair, which can feed the fee model. Taxes are not in that field; check the `commission` in `GET /api/v2/myTrades` once private read-only access is in place (Phase 2).

## Layout (Milestone 1)
```
config/        exchange.yaml risk.yaml strategy.yaml settings.yaml
core/          logging_setup.py    (JSON logs + secret redaction)
data/collectors/indodax_public.py  (GET-only client: timeout, retry/backoff, throttle, error envelopes)
data/validators/market.py          (pure validation functions)
scripts/       env_check.sh  check_markets.py
tests/unit/    20 tests, no network
```

## Safety
- `.env` is git-ignored; `.env.example` has empty placeholders. No key is needed yet.
- The public client has no order, cancel or withdraw methods, and a unit test enforces this.
