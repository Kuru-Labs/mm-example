---
name: kuru-mm-bot
description: Specialized knowledge for coding on the Kuru market making bot (mm-example repository). Use when working on any task involving order tracking, PropMaintain strategy, position management, quoter logic, bot configuration, or the order lifecycle. Provides critical architectural context that prevents common mistakes — load this before making any changes to bot.py, quoter.py, or config.py.
---

# Kuru Market Making Bot

A PropMaintain market maker for Kuru DEX on Monad. Instead of replacing all orders every cycle, it only cancels orders whose edge has drifted below a configurable threshold — reducing gas costs significantly.

## Running the Bot

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in PRIVATE_KEY and MARKET_ADDRESS
python -m mm_bot.main
```

Logs → `tracking/bot_run_YYYYMMDD_HHMMSS.log`. Position persists across restarts via `tracking/position_state.json`.

## Key Files

| File | Role |
|------|------|
| `mm_bot/main.py` | Entry point, logging setup, signal handlers |
| `mm_bot/bot/bot.py` | All strategy logic (~1270 lines) |
| `mm_bot/quoter/quoter.py` | Bid/ask price + cancel threshold calculation |
| `mm_bot/config/config.py` | `BotConfig` dataclass, loads `.env` |
| `mm_bot/kuru_imports.py` | **Single shim for all SDK imports** — always import from here |

The root-level `kuru_imports.py` is unused dead code. Do not import from it.

## Critical Invariants

**1. Three tracking dicts are always set and cleared together:**
`active_cloids`, `active_orders`, `order_sizes` — all populated at ORDER_PLACED, all cleared at CANCELLED/FULLY_FILLED. Breaking this invariant corrupts prop-maintain and reconciliation.

**2. Always initialize `KuruClient` from full SDK config bundle:**
```python
self.client = await KuruClient.create(**sdk_configs)
```
Do not bypass bundle-based initialization with partial ad-hoc config wiring. Keep all transaction/websocket/order-execution/cache settings flowing from `ConfigManager.load_all_configs(...)`.

**3. Cloid format is load-bearing:**
```
{side}-{baseline_edge_bps}-{timestamp_ms}
# e.g.  bid-1.0-1771500973306   ask-15.0-1771500975944
```
Quoter-to-order matching uses prefix `bid-{bps}-` / `ask-{bps}-`. Changing the format requires updating the matching logic in `_generate_orders_with_prop_maintain()`.

## Order Tracking: Callbacks, Not REST API

The REST API lags ~2 seconds behind on-chain events. The bot tracks order state from WebSocket callbacks exclusively. `active_orders` (Dict[str, OrderInfo]) holds callback-tracked price+size and is used for edge checks when the REST API hasn't indexed a new order yet — this is the `[callback]` log path in prop-maintain.

REST API is only used for: startup cleanup, orphan detection validation, and shutdown.

## PropMaintain Check Chain

For each quoter + side, `_generate_orders_with_prop_maintain()` checks the existing order in priority order:

1. Found in REST API result → edge check using API price
2. In `preregistered_orders` → just sent, awaiting confirmation → hold
3. In `active_orders` → confirmed via callback, REST API not yet indexed → full edge check using callback price (logs `[callback]`)
4. In `active_cloids` but not `active_orders` → unknown state → hold

If one side of a quoter is replaced, the **coupling block** force-replaces the other side too (~line 1090 in `bot.py`). This ensures both bid and ask always share the same reference price.

## Shutdown

`bot.stop()` uses exponential backoff (1s, 2s, 4s, …): calls `cancel_all_active_orders_for_market()`, waits, re-checks REST API, repeats until zero orders remain. This handles cancel-lag where the API still shows already-cancelled orders, which previously caused "Only Owner Allowed" contract reverts.

---

For data structure details, full order lifecycle, skew formula, all env vars, and common pitfalls, see [references/architecture.md](references/architecture.md).
