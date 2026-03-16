---
name: kuru-mm-bot
description: Specialized knowledge for coding on the Kuru market making bot (mm-example repository). Use when working on any task involving order tracking, PropMaintain strategy, position management, quoter logic, bot configuration, or the order lifecycle. Provides critical architectural context that prevents common mistakes — load this before making any changes to bot.py, quoter files, or config.py.
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
| `mm_bot/bot/bot.py` | All strategy orchestration, order lifecycle |
| `mm_bot/quoter/base.py` | `BaseQuoter` ABC — implement `decide(ctx) -> QuoterDecision` |
| `mm_bot/quoter/context.py` | `QuoterContext` (frozen snapshot) and `QuoterDecision` |
| `mm_bot/quoter/skew_quoter.py` | Built-in `SkewQuoter` — position skew + PropMaintain |
| `mm_bot/quoter/registry.py` | `register_quoter()` / `get_quoter_class()` |
| `mm_bot/quoter/quoter.py` | Backward-compat shim (`SkewQuoter as Quoter`) |
| `mm_bot/config/config.py` | `BotConfig` dataclass, TOML + `.env` loading |
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
{side}-{quoter_id}-{timestamp_ms}
# e.g.  bid-1.0-1771500973306   ask-15.0-1771500975944
```
For `SkewQuoter`, `quoter_id = str(Decimal(str(baseline_edge_bps)))`. Matching uses `quoter.cloid_prefix_bid` / `quoter.cloid_prefix_ask`. Changing format requires updating matching logic in `_resolve_existing_orders()`.

## Order Tracking: Callbacks, Not REST API

The REST API lags ~2 seconds behind on-chain events. The bot tracks order state from WebSocket callbacks exclusively. `active_orders` (Dict[str, OrderInfo]) holds callback-tracked price+size and is used for edge checks when the REST API hasn't indexed a new order yet — this is the `[callback]` log path in prop-maintain.

REST API is only used for: startup cleanup, orphan detection validation, and shutdown.

## Order Generation Flow

Each iteration, `_generate_orders()` in `bot.py`:

1. Calls `_resolve_existing_orders(quoter, on_chain_by_cloid)` — scans tracking dicts, returns `ExistingOrder` objects with `source` set to `"on_chain"`, `"callback"`, `"preregistered"`, or `"unknown"`
2. Builds a frozen `QuoterContext` snapshot (price, position, existing orders, stop flags)
3. Calls `quoter.decide(ctx)` — the quoter returns `QuoterDecision(cancels, new_orders)`
4. Discards cancelled cloids from `active_cloids`, batches all orders into `place_orders()`

The old `_generate_orders_with_prop_maintain` method has been replaced by this flow. All per-quoter cancel/maintain logic now lives in the quoter's `decide()` method.

## PropMaintain Check Chain (SkewQuoter)

`SkewQuoter.decide()` evaluates each side's existing order by `source`:

1. `"preregistered"` → just sent, awaiting confirmation → hold
2. `"unknown"` → in `active_cloids` but not `active_orders` → hold
3. `"on_chain"` or `"callback"` → compute edge; keep if `edge >= baseline × (1 - prop_maintain)`, else cancel

If one side of a quoter is replaced, the **coupling block** inside `decide()` force-replaces the other side too. This ensures both bid and ask always share the same reference price.

## Shutdown

`bot.stop()` uses exponential backoff (1s, 2s, 4s, …): calls `cancel_all_active_orders_for_market()`, waits, re-checks REST API, repeats until zero orders remain. This handles cancel-lag where the API still shows already-cancelled orders, which previously caused "Only Owner Allowed" contract reverts.

---

For data structure details, full order lifecycle, skew formula, all env vars, and common pitfalls, see [references/architecture.md](references/architecture.md).
