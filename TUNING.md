# Parameter Tuning Guide

This guide explains what each strategy parameter does and how to tune it. All parameters live in `bot_config.toml` under `[strategy]`.

---

## Baseline edge: `quoters_bps`

```toml
quoters_bps = [7, 15]
```

Each value is the **baseline edge in basis points** for one quoter — the target distance from the oracle mid-price at which that quoter places its bid and ask (before any position skew is applied). `7` = 0.07% each side.

Each entry in the list creates one independent bid/ask pair. You can run multiple quoters at different edge levels simultaneously:

```toml
quoters_bps = [5, 20]
```

The inner quoter (5 bps) captures most of the flow. The outer quoter (20 bps) earns more edge per fill and only triggers on larger price moves.

**Starting point:** baseline edge should at least cover on-chain fee costs (~2–5 bps) plus your target profit margin. On a liquid market, 5–15 bps is common. On a thinner market, 20–50 bps may be needed to avoid adverse selection.

---

## Order size: `quantity` and `quantity_bps_per_level`

```toml
quantity = 500
```

Fixed order size per side, per quoter level, in base token units.

Alternatively, size as a fraction of `max_position`:
```toml
quantity_bps_per_level = 500   # 5% of max_position per level
```

This keeps order size proportional to your inventory limit — useful when tuning `max_position` frequently.

**Rule of thumb:** size each level so that a full fill on one side doesn't push you uncomfortably close to `max_position`. With two levels and `max_position = 10000`, sizing each at 500–1000 leaves room for multiple fills before skew kicks in hard.

---

## Inventory cap: `max_position`

```toml
max_position = 10000
```

Maximum net position (in base token units) the bot will hold. When position reaches this limit:
- `stop_bids = True` — bids are cancelled and not replaced
- `stop_asks = True` (on the short side) — asks are cancelled

This is a hard cap. The skew parameters (below) are the soft mechanism that slows accumulation before the cap is hit.

---

## Inventory skew: `prop_skew_entry` and `prop_skew_exit`

These control how aggressively the bot adjusts its quoted edge as inventory builds up.

```toml
prop_skew_entry = 0.1   # slow to build more inventory
prop_skew_exit  = 0.5   # eager to unwind
```

The skew formula scales edge up (wider, less competitive) or down (tighter, more competitive) based on `position / max_position`:

- **Long position →** bid edge widens (slow to buy more), ask edge tightens (eager to sell)
- **Short position →** ask edge widens (slow to sell more), bid edge tightens (eager to buy)

`prop_skew_entry` controls the widening on the side that would increase inventory.
`prop_skew_exit` controls the tightening on the side that would reduce inventory.

At `position = max_position` the multipliers are at full effect:
- `prop_skew_entry = 0.5` → entry edge is `baseline * 1.5` (50% wider)
- `prop_skew_exit = 0.5` → exit edge is `baseline * 0.5` (50% tighter)

**Aggressive unwind example:**
```toml
prop_skew_entry = 0.2
prop_skew_exit  = 0.8
```
Barely slows down accumulation but aggressively undercuts to unwind. Use this on assets you don't want to hold overnight.

**Passive / symmetric example:**
```toml
prop_skew_entry = 0.5
prop_skew_exit  = 0.5
```
Symmetric adjustment — equally reluctant to accumulate as to unwind.

---

## PropMaintain cancel threshold: `prop_maintain`

```toml
prop_maintain = 0.2
```

Controls when an existing order is cancelled and replaced. An order is kept if its current edge is at least `(1 - prop_maintain)` of the target edge.

`prop_maintain = 0.2` → cancel if edge has drifted below 80% of target.
`prop_maintain = 0.05` → very patient — only cancel if edge has drifted below 95% of target.
`prop_maintain = 0.5` → aggressive — cancel and replace if edge drops below 50% of target.

**Lower values** = fewer cancels/replacements = less gas, but quotes may sit stale longer.
**Higher values** = quotes track the oracle more tightly = more gas.

On a fast-moving market, use a higher value (0.3–0.5). On a slow market, 0.05–0.1 is fine.

---

## Oracle depth state: `kuru_depth_state`

```toml
kuru_depth_state = "proposed"
```

Which Monad block state to read orderbook prices from. The four states reflect Monad's block lifecycle:

| State | Latency | Finality | Use when |
|-------|---------|----------|----------|
| `proposed` | Lowest | Can reorg | Fast-moving markets, tight spreads |
| `voted` | Low | Unlikely to reorg | Good balance |
| `finalized` | Medium | Very safe | Conservative |
| `committed` | Highest | Guaranteed | Maximum safety |

For most MM strategies, `proposed` or `voted` gives the freshest reference price and is fine — reorgs on Monad are rare. Use `committed` if you're seeing inconsistent fills that suggest stale pricing.

---

## Reconciliation interval: `reconcile_interval`

```toml
reconcile_interval = 10
```

Seconds between position reconciliation checks (REST API call to detect orphaned orders). Set to `0` to disable.

**Lower values** = more frequent orphan detection, more API calls.
Recommended: `10`–`30` seconds. No need to go below 5.

---

## Multi-level vs per-quoter config

**Flat config** (all levels share the same skew params):
```toml
quoters_bps = [5, 15, 30]
quantity = 500
prop_skew_entry = 0.3
prop_skew_exit = 0.6
```

**Per-quoter config** (different params per level — more control):
```toml
[[strategy.quoters]]
type = "skew"
baseline_edge_bps = 5
quantity = 200
prop_skew_entry = 0.1
prop_skew_exit = 0.8

[[strategy.quoters]]
type = "skew"
baseline_edge_bps = 25
quantity = 1000
prop_skew_entry = 0.5
prop_skew_exit = 0.3
```

The inner level is small and quick to unwind. The outer level is large and patient — it only fills on big moves, so inventory risk is lower.

---

## Hot-reload vs restart

| Parameter | Reload behaviour |
|-----------|-----------------|
| `prop_maintain` | Hot — applies immediately |
| `reconcile_interval` | Hot — applies immediately |
| `prop_skew_entry` / `prop_skew_exit` | Reinit — brief trading pause |
| `quoters_bps` / `quoters` | Reinit — brief trading pause |
| `quantity` / `quantity_bps_per_level` | Reinit — brief trading pause |
| `max_position` | Reinit — brief trading pause |
| `oracle_source` / `kuru_symbol` / `kuru_depth_state` | Restart required |
| `market_address` | Restart required |
