# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python -m mm_bot.main

# Run with a custom .env file
cp .env.example .env  # then edit
python -m mm_bot.main
```

There are no tests or linter configs in this repository.

## Architecture

This is an async market making bot for Kuru DEX on Monad. It maintains bid/ask quotes using a **PropMaintain** strategy: only cancel/replace orders whose edge has drifted below a threshold, rather than replacing all orders every cycle.

### Entry point and config

`mm_bot/main.py` → load SDK config bundle via `ConfigManager.load_all_configs(...)` in `mm_bot/config/config.py` → construct `Bot` → `await bot.start()`.

Operational strategy configuration comes from `bot_config.toml` (with `.env` fallback). `BotConfig` is a dataclass holding strategy params. SDK-level configs are loaded as a full bundle via `ConfigManager.load_all_configs(...)`.

**Critical:** Keep SDK client initialization on the full config bundle path (`KuruClient.create(**sdk_configs)`) so transaction/websocket/order-execution/cache settings all flow from environment defaults and overrides.

### Two kuru_imports files

- `mm_bot/kuru_imports.py` — the canonical shim used by all bot code. Imports from `kuru_sdk_py.*` (the installed package namespace).
- `kuru_imports.py` (root) — legacy shim using `src.*` prefix. Not used by the bot directly.

Always import SDK types via `mm_bot.kuru_imports`.

### Order tracking model

The bot tracks order state **entirely from WebSocket callbacks**, not REST API polling. The REST API lags ~2 seconds behind on-chain events and is only used for: startup cleanup, orphan detection, and shutdown.

Key tracking dicts on `Bot` (all set/cleared together in callbacks):

- `active_cloids` — set of live cloids (populated at ORDER_PLACED, cleared at CANCELLED/FILLED)
- `active_orders: Dict[str, OrderInfo]` — callback-tracked price+size per cloid; used for edge checks during REST API lag
- `order_sizes` — remaining size per cloid for partial fill math
- `cloid_to_order_id` / `order_id_to_cloid` — bidirectional maps used by orphan detector
- `preregistered_orders` — cloids registered before `place_orders()` is called, to handle immediate fills that arrive before ORDER_PLACED
- `recently_cancelled_order_ids: Dict[int, float]` — order IDs we cancelled (callback received) but REST API still shows; prevents false-positive orphan detections; pruned after 5 seconds

**Invariant:** A cloid in `active_cloids` always has a matching entry in both `active_orders` and `order_sizes`. These three are always set and cleared together.

### Pluggable quoter system

The quoter layer is split into:

- `mm_bot/quoter/base.py` — `BaseQuoter` ABC. Implement `decide(ctx: QuoterContext) -> QuoterDecision`.
- `mm_bot/quoter/context.py` — `QuoterContext` (frozen snapshot of market state) and `QuoterDecision` (cancels + new orders).
- `mm_bot/quoter/skew_quoter.py` — `SkewQuoter`, the built-in strategy (position-skew + PropMaintain).
- `mm_bot/quoter/registry.py` — `register_quoter(name, cls)` / `get_quoter_class(name)`.
- `mm_bot/quoter/quoter.py` — backward-compat shim (`SkewQuoter as Quoter`).

The bot creates quoters via `_initialize_quoters()` using the registry. For each iteration it:
1. Calls `_resolve_existing_orders(quoter, on_chain_by_cloid)` to build `ExistingOrder` objects from tracking dicts
2. Constructs a `QuoterContext` snapshot
3. Calls `quoter.decide(ctx)` to get cancels + new orders
4. Processes the `QuoterDecision` (discard cancelled cloids from `active_cloids`, batch into `place_orders()`)

This replaces the old `_generate_orders_with_prop_maintain` method. All per-quoter strategy logic now lives in the quoter's `decide()` method.

### Order generation flow (`_generate_orders`)

For each quoter, `_resolve_existing_orders` resolves the existing bid/ask from tracking dicts using this priority:

1. Found in `on_chain_by_cloid` (REST API) → source `"on_chain"`, price from API
2. Found in `preregistered_orders` → source `"preregistered"`, price `None`
3. Found in `active_orders` → source `"callback"`, price from callback
4. Found in `active_cloids` only → source `"unknown"`, price `None`

The `SkewQuoter.decide()` check chain:
1. source `"preregistered"` → hold (awaiting confirmation)
2. source `"unknown"` → hold
3. source `"on_chain"` or `"callback"` → edge check against cancel threshold; keep or cancel
4. Coupling: if one side replaced, force-replace the other (uses same reference price/skew)

### Cloid format

```
{side}-{quoter_id}-{timestamp_ms}
# e.g. bid-1.0-1771500973306, ask-15.0-1771500975944
```

For `SkewQuoter`, `quoter_id = str(Decimal(str(baseline_edge_bps)))`, preserving the original format.
For custom quoters, `quoter_id` is set in `BaseQuoter.__init__` and must be unique and stable across restarts.

Quoter-to-order matching uses `cloid_prefix_bid` / `cloid_prefix_ask` properties on `BaseQuoter`. Do not change the format without updating matching logic.

### Quoter skew formula

`SkewQuoter._get_skewed_edges()` adjusts edges based on `position / max_position` (capped ±1):
- Long position → widen bids (slow to buy more), tighten asks (eager to sell)
- Short position → tighten bids (eager to buy), widen asks (slow to sell more)

Skew magnitude is controlled by `prop_skew_entry` and `prop_skew_exit`.

### Shutdown

`bot.stop()` uses an exponential backoff loop (1s, 2s, 4s, …) that calls `cancel_all_active_orders_for_market()` and re-checks the REST API until zero orders remain. This handles the case where the REST API still shows orders that were already cancelled, which previously caused "Only Owner Allowed" contract reverts.

### Runtime output

All output goes under `tracking/`:
- `bot_run_YYYYMMDD_HHMMSS.log` — full run log
- `position_state.json` — persisted position, loaded on restart
- `position_reconciliation.csv` — per-reconcile balance/drift snapshots

For deeper context on design decisions, data flow diagrams, and common pitfalls, see `SKILL.MD`.
