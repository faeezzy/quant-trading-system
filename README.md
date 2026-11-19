# INDODAX Quant Trading System

Status: **Milestone 1: read-only market-data collector.** No API key, no orders, `trading_enabled: false`.

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
