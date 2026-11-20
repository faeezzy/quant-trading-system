# Live broker — design note (2026-11-20)

Status: **engineering scaffold, UNVALIDATED LIVE.** Built and tested against a fake exchange only.
This sandbox cannot reach INDODAX; the real order path is first exercised on the owner's host,
with the arm/confirm guard, at Tier 1. Covers spec 8 (runtime) and 9.3 (live engineering gate).

## Why / intent
Turn the validated paper pipeline into one that can place real spot orders on INDODAX safely.
Survival-first: correctness under timeouts and restarts matters more than getting a good price.
Go-live stays gated behind the paper gate (spec 9.2) + owner approval + Tier 1.

## Hard constraints (from spec 2, 3, 4)
- Spot only, no withdrawal permission. Client refuses to run if `canWithdraw`/`canWithdrawFiat` true.
- No native stops on INDODAX V2 → stop stays a locally-monitored synthetic stop vs live bid.
- Never resend blindly after a timeout: reconcile (query by client order id) first.
- One production instance (single-instance lock). Never average down, never widen a stop.

## Order policy (owner-approved, 2026-11-20): asymmetric
- **Entry** = marketable LIMIT with a tight slippage cap (default 0.3%). If it doesn't fill, skip —
  a missed entry costs nothing (spec 2.8).
- **Stop / exit** = MARKET (fill guaranteed; slippage accepted). Survival beats price on the way out.

## Cadence (owner deferred → our call): hybrid
- Full decision cycle hourly (matches the backtest's hourly-stop assumption; strategy logic identical).
- Lightweight stop-watch every ~10 min: fetch bid, check the synthetic stop only. Makes live *safer*
  than the backtest; configurable to strict-hourly.

## Components
1. `execution/live_order_client.py` — `IndodaxTradeClient`. Signed GET/POST/DELETE on an explicit
   (method, path) allowlist that contains **no withdrawal path**. Reuses the read client's HMAC
   signing and clock-resync. `create_order` builds Binance-shaped params in one isolated, clearly
   flagged place — **the exact POST field names must be verified against INDODAX-TradeAPI-2-Private.md
   before live** (we have no local copy; the read side already uses `origClientOrderId`, so a client
   id is supported). Permission guard refuses unsafe keys.
2. `execution/fake_exchange.py` — deterministic in-memory double with the same interface, able to
   inject timeouts, partial fills and rejects, so the broker and runner are testable with no network.
3. `execution/live_broker.py` — `LiveBroker`. Persists an `orders` lifecycle log and position state
   in SQLite. The exchange is the source of truth: `reconcile()` rebuilds local truth from
   `open_orders` + `my_trades` + `get_order(client_id)` before any decision. Deterministic client
   order ids make placement idempotent across retries. Partial fills: entry keeps the filled part and
   cancels the remainder; exit/stop must reach flat.
4. `scripts/live_trade.py` — the live runner: lock → permission guard → reconcile → stop check →
   once-per-day decision → mark/limits/report. Requires `TRADING_MODE=live` and an explicit arm step
   before it will place real orders.

## Deferred (not v1)
Alerts/Hermes reporting, multi-coin/multi-position, automatic tier changes.

## Testing reality
Everything here is covered by unit/integration tests against `FakeExchange`. The live path is NOT
proven. Before Tier 1 the owner must, on the 24/7 host: verify the POST field names against the docs,
run the arm/confirm path with one minimal real order, and confirm fills + reconciliation on real data.
