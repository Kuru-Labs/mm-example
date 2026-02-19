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

`mm_bot/main.py` → `load_config_from_env()` → constructs `Bot` → `await bot.start()`.

All configuration comes from `.env` via `mm_bot/config/config.py`. `BotConfig` is a dataclass holding strategy params. SDK-level configs (wallet, connection, market, transaction) are loaded by `ConfigManager` from the SDK.

**Critical:** `KuruClient.create()` must receive `transaction_config=ConfigManager.load_transaction_config()` explicitly. Omitting it silently uses hardcoded SDK defaults and ignores `KURU_GAS_BUFFER_MULTIPLIER` from `.env`.

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

### PropMaintain logic (`_generate_orders_with_prop_maintain`)

For each quoter level, each side independently checks whether the existing order's edge is above the cancel threshold (`baseline_edge × (1 - PROP_MAINTAIN)`). The check chain:

1. Order found in REST API result → use API price for edge check
2. Order in `preregistered_orders` → just sent, awaiting confirmation → hold
3. Order in `active_orders` → confirmed via callback but REST API hasn't indexed it yet → use callback price for full edge check (logs `[callback]`)
4. Order in `active_cloids` but not `active_orders` → unknown state → hold

**Coupling:** when one side of a quoter is replaced, the other is force-replaced too (lines ~1090–1100 in `bot.py`). This keeps both sides priced off the same reference.

### Cloid format

```
{side}-{baseline_edge_bps}-{timestamp_ms}
# e.g. bid-1.0-1771500973306, ask-15.0-1771500975944
```

Quoter-to-order matching uses cloid prefix (`bid-{bps}-` / `ask-{bps}-`). Do not change this format without updating the matching logic.

### Quoter skew formula

`Quoter.get_bid_ask_edges()` adjusts edges based on `position / max_position` (capped ±1):
- Long position → widen bids (slow to buy more), tighten asks (eager to sell)
- Short position → tighten bids (eager to buy), widen asks (slow to sell more)

Skew magnitude is controlled by `PROP_SKEW_ENTRY` and `PROP_SKEW_EXIT`.

### Shutdown

`bot.stop()` uses an exponential backoff loop (1s, 2s, 4s, …) that calls `cancel_all_active_orders_for_market()` and re-checks the REST API until zero orders remain. This handles the case where the REST API still shows orders that were already cancelled, which previously caused "Only Owner Allowed" contract reverts.

### Runtime output

All output goes under `tracking/`:
- `bot_run_YYYYMMDD_HHMMSS.log` — full run log
- `position_state.json` — persisted position, loaded on restart
- `position_reconciliation.csv` — per-reconcile balance/drift snapshots

For deeper context on design decisions, data flow diagrams, and common pitfalls, see `SKILL.MD`.
