# Kuru Market Making Bot

An async market-making bot for [Kuru DEX](https://kuru.io) on Monad. Maintains bid/ask quotes using a **PropMaintain** strategy — only cancels and replaces orders whose edge has drifted below a threshold, minimizing gas costs.

## Quick Start

```bash
git clone https://github.com/kuru-labs/mm-example.git
cd mm-example
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

```bash
cp .env.example .env
cp bot_config.example.toml bot_config.toml
```

Set your credentials in `.env`:
```
PRIVATE_KEY=your_private_key_without_0x
```

Set your market in `bot_config.toml`:
```toml
market_address = "0x..."
oracle_source = "kuru"   # or "coinbase" (requires coinbase_symbol)
```

```bash
PYTHONPATH=. python3 -m mm_bot.main
```

Logs appear under `tracking/`.

---

## Configuration

The bot uses two config files:

- **`bot_config.toml`** — strategy and operational settings (hot-reloadable)
- **`.env`** — secrets and SDK runtime settings

### Required `.env` keys

| Key | Notes |
|-----|-------|
| `PRIVATE_KEY` | No `0x` prefix |
| `MARKET_ADDRESS` | Can also be set in `bot_config.toml` as `strategy.market_address` |

### Common SDK `.env` settings

| Key | Default |
|-----|---------|
| `RPC_URL` | `https://rpc.monad.xyz` |
| `RPC_WS_URL` | `wss://rpc.monad.xyz` |
| `KURU_WS_URL` | `wss://ws.kuru.io/` |
| `KURU_API_URL` | `https://api.kuru.io/` |
| `KURU_RPC_LOGS_SUBSCRIPTION` | `monadLogs` |
| `KURU_GAS_BUFFER_MULTIPLIER` | SDK default |
| `KURU_USE_ACCESS_LIST` | `true`/`false` |
| `KURU_POST_ONLY` | `true`/`false` |

### Key `bot_config.toml` parameters

Hot-reloadable (no restart needed):

| Parameter | Description |
|-----------|-------------|
| `prop_maintain` | Cancel threshold — `0.2` means cancel if edge drops below 80% of target |
| `reconcile_interval` | Seconds between position reconciliation (`0` = disabled) |

Full reinit on change (brief trading pause):

| Parameter | Description |
|-----------|-------------|
| `quoters_bps` | Spread levels in bps — `[1, 10, 15]` creates 3 bid/ask pairs |
| `quoter_type` | Quoter strategy — built-in: `"skew"` |
| `quantity` | Order size per level per side (base token units) |
| `max_position` | Inventory cap — bot skews quotes to stay within |
| `prop_skew_entry` | How aggressively to slow down position accumulation |
| `prop_skew_exit` | How aggressively to accelerate position unwind |

See `bot_config.example.toml` for a fully annotated reference.

---

## Architecture

```
mm_bot/
├── main.py               # Entry point, logging, signal handling
├── bot/bot.py            # Quoting loop, order lifecycle, callbacks
├── quoter/               # Pluggable quoter system (see below)
├── config/
│   ├── config.py         # BotConfig dataclass, TOML + .env loading
│   └── config_watcher.py # Hot-reload (watches bot_config.toml every 5s)
├── position/             # Position tracking, persistence
├── pricing/oracle.py     # Oracle sources (Kuru WS or Coinbase REST)
└── pnl/tracker.py        # PnL display
```

The bot tracks all order state from **WebSocket callbacks**, not the REST API. The REST API lags ~2 seconds behind on-chain events and is only used for startup cleanup, orphan detection, and shutdown.

### Quoter system

Each quoter manages one bid/ask pair at one spread level. On every iteration the bot:

1. Resolves existing order state from its tracking dicts into a frozen `QuoterContext` snapshot
2. Calls `quoter.decide(ctx)` — the quoter returns cancels + new orders
3. Batches everything into a single `place_orders()` transaction

| Module | Role |
|--------|------|
| `quoter/base.py` | `BaseQuoter` ABC — implement `decide(ctx) -> QuoterDecision` |
| `quoter/context.py` | `QuoterContext` (frozen snapshot) and `QuoterDecision` |
| `quoter/skew_quoter.py` | Built-in `SkewQuoter` — position skew + PropMaintain cancel logic |
| `quoter/registry.py` | `register_quoter()` / `get_quoter_class()` |

### Writing a custom quoter

```python
from decimal import Decimal
from mm_bot.quoter.base import BaseQuoter
from mm_bot.quoter.context import QuoterContext, QuoterDecision
from mm_bot.quoter.registry import register_quoter
from mm_bot.kuru_imports import Order, OrderType, OrderSide

class MyQuoter(BaseQuoter):
    def __init__(self, edge_bps: float, quantity: Decimal):
        super().__init__(quoter_id=f"my-{edge_bps}", quantity=quantity)
        self.edge = Decimal(str(edge_bps))

    def decide(self, ctx: QuoterContext) -> QuoterDecision:
        cancels = []
        if ctx.existing_bid and ctx.existing_bid.source not in ("preregistered", "unknown"):
            cancels.append(ctx.existing_bid.cloid)
        if ctx.existing_ask and ctx.existing_ask.source not in ("preregistered", "unknown"):
            cancels.append(ctx.existing_ask.cloid)

        new_orders = []
        if not ctx.stop_bids:
            new_orders.append(Order(
                cloid=self.make_cloid("bid"), order_type=OrderType.LIMIT,
                side=OrderSide.BUY, size=self.quantity, post_only=False,
                price=self.price_from_edge(self.edge, OrderSide.BUY, ctx.reference_price),
            ))
        if not ctx.stop_asks:
            new_orders.append(Order(
                cloid=self.make_cloid("ask"), order_type=OrderType.LIMIT,
                side=OrderSide.SELL, size=self.quantity, post_only=False,
                price=self.price_from_edge(self.edge, OrderSide.SELL, ctx.reference_price),
            ))
        return QuoterDecision(cancels=cancels, new_orders=new_orders)

    @classmethod
    def from_config(cls, config_section: dict) -> "MyQuoter":
        return cls(
            edge_bps=float(config_section["baseline_edge_bps"]),
            quantity=Decimal(str(config_section["quantity"])),
        )

register_quoter("my_quoter", MyQuoter)
```

Then in `bot_config.toml`:
```toml
[[strategy.quoters]]
type = "my_quoter"
baseline_edge_bps = 10.0
quantity = 500
```

`QuoterContext` fields:

| Field | Type | Description |
|-------|------|-------------|
| `reference_price` | `Decimal` | Current fair price from oracle |
| `current_position` | `Decimal` | Net position (positive = long) |
| `max_position` | `Decimal` | Position limit from config |
| `existing_bid` | `ExistingOrder?` | Bot's current bid for this quoter |
| `existing_ask` | `ExistingOrder?` | Bot's current ask for this quoter |
| `stop_bids` | `bool` | `True` if position ≥ max_position |
| `stop_asks` | `bool` | `True` if position ≤ -max_position |
| `prop_maintain` | `float` | Cancel threshold factor from config |
| `price_precision` | `Decimal` | Market price precision |

`ExistingOrder.source`: `"on_chain"` · `"callback"` · `"preregistered"` · `"unknown"`

---

## Troubleshooting

**Orders not placing**
- Check margin balances and wallet gas balance
- Confirm market address and token decimals from on-chain market config

**Frequent retries / connectivity issues**
- Check RPC/WebSocket health
- Tune `KURU_RPC_WS_*` reconnect settings in `.env`

**No reference price**
- Verify `oracle_source` in `bot_config.toml`
- Check connectivity to Kuru WS or Coinbase API

**Position drift after restart**
- Position is persisted in `tracking/position_state.json`
- Use `override_start_position` in `bot_config.toml` to force a specific starting value

---

## License

MIT
