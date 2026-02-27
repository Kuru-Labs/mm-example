# Kuru Market Making Bot

A market-making bot for Kuru DEX using the refactored `kuru-sdk-py` client.

## What Changed

This repo now tracks SDK v0.1.9+ behavior:

- Decimal-native order/position math in bot state and fill handling
- Full `ConfigManager.load_all_configs(...)` bootstrap path
- Typed SDK error handling (`Kuru*Error`) for retries and recovery
- Cancel-all flow aligned with SDK semantics (no tx-hash return assumptions)
- Pluggable quoter system — implement `BaseQuoter.decide()` to plug in your own strategy

## Architecture

Core modules:

- `mm_bot/main.py`: process startup, logging, signal handling
- `mm_bot/config/config.py`: loads operational config (`bot_config.toml`) and SDK config bundle
- `mm_bot/bot/bot.py`: quoting loop, order lifecycle callbacks, cancellation/reconciliation, typed recovery
- `mm_bot/quoter/`: pluggable quoter system (see below)
- `mm_bot/position/position_tracker.py`: Decimal-native position persistence (`tracking/position_state.json`)
- `mm_bot/pricing/oracle.py`: oracle sources (`kuru` websocket or `coinbase` REST)
- `mm_bot/pnl/tracker.py`: Decimal-native PnL display

### Quoter system

The quoter system is pluggable. Each quoter handles one spread level (one bid/ask pair):

| Module | Role |
|--------|------|
| `mm_bot/quoter/base.py` | `BaseQuoter` ABC — implement `decide(ctx) -> QuoterDecision` |
| `mm_bot/quoter/context.py` | `QuoterContext` (market snapshot) and `QuoterDecision` (cancel/place instructions) |
| `mm_bot/quoter/skew_quoter.py` | Built-in `SkewQuoter` — position-skew with PropMaintain cancel logic |
| `mm_bot/quoter/registry.py` | `register_quoter()` / `get_quoter_class()` |

The bot resolves order state from its tracking dicts into a `QuoterContext` snapshot and passes it to each quoter's `decide()` method. Quoters return a `QuoterDecision` (cloids to cancel + new orders to place) without touching any bot internals.

#### Writing a custom quoter

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
        # Cancel existing orders whose price source is known
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

`QuoterContext` fields available to `decide()`:

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

`ExistingOrder.source` values: `"on_chain"`, `"callback"`, `"preregistered"`, `"unknown"`.

## Install

```bash
git clone https://github.com/kuru-labs/mm-example.git
cd mm-example
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

The bot uses:

1. `bot_config.toml` for strategy/operational settings
2. `.env` for secrets and SDK runtime settings

### Required `.env`

- `PRIVATE_KEY`
- `MARKET_ADDRESS` (required unless `strategy.market_address` is set in `bot_config.toml`)

### Common SDK `.env` settings

- `RPC_URL` (default `https://rpc.monad.xyz`)
- `RPC_WS_URL` (default `wss://rpc.monad.xyz`)
- `KURU_WS_URL` (default `wss://ws.kuru.io/`)
- `KURU_API_URL` (default `https://api.kuru.io/`)
- `KURU_RPC_LOGS_SUBSCRIPTION` (default `monadLogs`)
- `KURU_GAS_BUFFER_MULTIPLIER` (default from SDK)
- `KURU_USE_ACCESS_LIST` (`true`/`false`)
- `KURU_POST_ONLY` (`true`/`false`)
- `KURU_RPC_WS_MAX_RECONNECT_ATTEMPTS`
- `KURU_RPC_WS_RECONNECT_DELAY`
- `KURU_RPC_WS_MAX_RECONNECT_DELAY`
- `KURU_RECONCILIATION_INTERVAL`
- `KURU_RECONCILIATION_THRESHOLD`

See `.env.example` and `bot_config.example.toml`.

## Run

```bash
./run.sh
```

or:

```bash
source venv/bin/activate
PYTHONPATH=. python3 mm_bot/main.py
```

## Runtime Notes

- Position state is persisted as Decimal-safe values in `tracking/position_state.json`.
- Terminal order states now include `ORDER_TIMEOUT` and `ORDER_FAILED` handling.
- SDK typed errors are classified for retry vs skip behavior:
  - execution errors: `KuruInsufficientFundsError`, `KuruContractError`, `KuruOrderError`
  - connectivity errors: `KuruConnectionError`, `KuruWebSocketError`, `KuruTimeoutError`
  - API/auth errors: `KuruAuthorizationError`

## Troubleshooting

- Orders not placing:
  - Check margin balances and wallet gas balance
  - Confirm market address and token decimals from on-chain market config
- Frequent retries:
  - Check RPC/WebSocket health
  - Tune `KURU_RPC_WS_*` and `KURU_RECONCILIATION_*`
- No reference price:
  - Verify selected oracle source in `bot_config.toml`
  - Check connectivity to Kuru WS or Coinbase API

## License

MIT
