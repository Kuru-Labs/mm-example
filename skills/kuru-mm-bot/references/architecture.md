# Architecture Reference

## Table of Contents
1. [Bot Tracking Dicts](#bot-tracking-dicts)
2. [Order Lifecycle](#order-lifecycle)
3. [Quoter Skew Formula](#quoter-skew-formula)
4. [Environment Variables](#environment-variables)
5. [Orphan Detection](#orphan-detection)
6. [Common Pitfalls](#common-pitfalls)

---

## Bot Tracking Dicts

All live on `self` in `Bot`. Understand what populates and clears each before modifying any order lifecycle code.

| Field | Type | Populated by | Cleared by | Purpose |
|-------|------|-------------|------------|---------|
| `active_cloids` | `Set[str]` | ORDER_PLACED | ORDER_CANCELLED, FULLY_FILLED | Fast membership: is this cloid still live? |
| `active_orders` | `Dict[str, OrderInfo]` | ORDER_PLACED | ORDER_CANCELLED, FULLY_FILLED | Callback-tracked price+size — used instead of REST API during lag |
| `order_sizes` | `Dict[str, float]` | ORDER_PLACED | ORDER_CANCELLED, FULLY_FILLED | Remaining size for partial fill math |
| `cloid_to_order_id` | `Dict[str, int]` | ORDER_PLACED | ORDER_CANCELLED | cloid → on-chain integer order ID |
| `order_id_to_cloid` | `Dict[int, str]` | ORDER_PLACED | ORDER_CANCELLED | Reverse map, used by orphan detector |
| `preregistered_orders` | `Dict[str, tuple[float,float]]` | Before `place_orders()` | ORDER_PLACED, FULLY_FILLED | Handles immediate fills arriving before ORDER_PLACED |
| `orphaned_order_timestamps` | `Dict[int, float]` | `_validate_against_api()` | Resolved or grace period reset | Orders on-chain with no callback |
| `recently_cancelled_order_ids` | `Dict[int, float]` | ORDER_CANCELLED | Pruned after 5s | Prevents cancel-lag false-positive orphan detections |

`OrderInfo` fields: `cloid`, `side`, `price`, `size` (remaining, updated on partial fills), `order_id`.

---

## Order Lifecycle

```
place_orders() called
  └─ preregistered_orders[cloid] = (size, timestamp)

ORDER_PLACED callback
  ├─ active_cloids.add(cloid)
  ├─ active_orders[cloid] = OrderInfo(cloid, side, price, size, order_id)
  ├─ order_sizes[cloid] = size
  ├─ cloid_to_order_id[cloid] = order_id
  ├─ order_id_to_cloid[order_id] = cloid
  └─ preregistered_orders.pop(cloid, None)

ORDER_PARTIALLY_FILLED callback
  ├─ position_tracker.update_position(side, filled_size, price)
  ├─ order_sizes[cloid] = remaining_size
  └─ active_orders[cloid].size = remaining_size   # stays active

ORDER_FULLY_FILLED callback
  ├─ position_tracker.update_position(side, filled_size, price)
  ├─ position_tracker.save_state()
  ├─ active_cloids.discard(cloid)
  ├─ del active_orders[cloid], order_sizes[cloid]
  └─ preregistered_orders.pop(cloid, None)    ← covers immediate fills

ORDER_CANCELLED callback
  ├─ active_cloids.discard(cloid)
  ├─ del active_orders[cloid]
  ├─ recently_cancelled_order_ids[order_id] = time.monotonic()
  └─ del cloid_to_order_id[cloid], order_id_to_cloid[order_id]
```

**Immediate fill handling:** an order can be fully filled before ORDER_PLACED fires. `preregistered_orders` is populated before `place_orders()` and cleaned in the FULLY_FILLED handler to cover this path.

**Position total** = `start_position + current_position`. Both `Quoter` and reconciliation use the total, not just `current_position`.

---

## Quoter Skew Formula

`Quoter.get_bid_ask_edges()` adjusts edges based on `prop = position / max_position` (capped ±1):

```python
if prop > 0:  # long → eager to sell, reluctant to buy more
    bid_edge = baseline × (1 + prop × prop_skew_entry)   # wider bid
    ask_edge = baseline × (1 - prop × prop_skew_exit)    # tighter ask
else:         # short → eager to buy, reluctant to sell more
    bid_edge = baseline × (1 - prop × prop_skew_exit)    # tighter bid
    ask_edge = baseline × (1 + prop × prop_skew_entry)   # wider ask

cancel_threshold = edge × (1 - PROP_MAINTAIN)
```

`get_cancel_edges(prop_maintain)` returns `(bid_cancel_edge, ask_cancel_edge)`.
`calculate_order_edge(price, side, ref_price)` returns the current edge in bps of an existing order.

---

## Environment Variables

| Variable | Default | Notes |
|----------|---------|-------|
| `PRIVATE_KEY` | required | No 0x prefix |
| `RPC_URL` | `https://rpc.fullnode.kuru.io/` | HTTP endpoint |
| `RPC_WS_URL` | `wss://rpc.fullnode.kuru.io/` | WebSocket endpoint |
| `MARKET_ADDRESS` | required | Kuru market contract |
| `ORACLE` | `coinbase` | `kuru` (WS mid-price) or `coinbase` (REST API) |
| `KURU_RPC_LOGS_SUBSCRIPTION` | `monadLogs` | RPC filter mode for Monad |
| `SDK_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MAX_POSITION` | `1000` | Max base asset position |
| `OVERRIDE_START_POSITION` | empty | Skips on-chain position fetch if set |
| `RECONCILE_INTERVAL` | `300` | Seconds between reconciliation (0 = disabled) |
| `PROP_SKEW_ENTRY` | `0.5` | Position skew factor when entering |
| `PROP_SKEW_EXIT` | `0.5` | Position skew factor when exiting |
| `QUANTITY` | `100` | Order size per quoter per side |
| `QUANTITY_BPS_PER_LEVEL` | empty | Overrides `QUANTITY` if set (BPS of max position) |
| `QUOTERS_BPS` | `25,50,75` | Comma-separated spread levels |
| `PROP_MAINTAIN` | `0.2` | Cancel threshold factor |
| `POSITION_UPDATE_THRESHOLD_BPS` | `500` | Drift alert threshold |
| `KURU_GAS_BUFFER_MULTIPLIER` | SDK default (1.1×) | Read by `ConfigManager.load_transaction_config()` |

To add a new parameter: add to `BotConfig` → read in `load_config_from_env()` → pass to `Quoter.__init__()` → use in `get_bid_ask_edges()` or `get_cancel_edges()`.

---

## Orphan Detection

`_validate_against_api()` compares REST API order IDs against `order_id_to_cloid.keys()`.

**Cancel-lag false positives** are filtered by `recently_cancelled_order_ids`: orders we cancelled (callback received) but still showing in the REST API are excluded. Entries pruned after 5 seconds. Uses `time.monotonic()` — not `time.time()`.

**Grace period:** new orphans get 3 seconds before triggering a state reset. Most resolve within this window as callbacks arrive shortly after.

**State reset** (timeout exceeded): `active_cloids`, `active_orders`, `order_sizes`, `preregistered_orders`, and all mapping dicts are cleared. Bot re-syncs from REST API on the next cycle.

---

## Common Pitfalls

**REST API lag (~2s):** Never use REST API results for latency-sensitive decisions. Use `active_orders` for anything that needs the current order price. The REST API is for validation and shutdown only.

**Immediate fills:** An order can be filled before ORDER_PLACED fires. Always populate `preregistered_orders` before `place_orders()`, and always clean it up in FULLY_FILLED.

**Coupling:** If you add logic that replaces one side of a quoter, check whether it should also trigger the coupling block (~line 1090 in `bot.py`) to replace the other side.

**`transaction_config`:** Always pass `ConfigManager.load_transaction_config()` to `KuruClient.create()`. Forgetting it causes silent gas under-estimation: cancels succeed but placements silently fail mid-batch (contract runs out of gas).

**Cloid prefix matching:** The quoter-to-order mapping relies on `{side}-{bps}-` prefixes. Don't add extra fields to the cloid format without updating matching logic.
