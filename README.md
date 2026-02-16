# Kuru Market Making Bot

A market making bot for Kuru DEX implementing a skew-based quoting strategy with event-driven order tracking.

## How It Works

The bot continuously quotes bid and ask orders around a reference price fetched from the Kuru oracle. It adjusts spreads based on the current position relative to the configured maximum — widening spreads in the entry direction and tightening them in the exit direction. This is the "skew" mechanism.

Each quoting level runs at a configured spread (in basis points). On each iteration, the bot cancels its previous orders and places fresh ones in a single transaction. Position changes are tracked via WebSocket fill callbacks rather than polling.

### Components

- **OracleService** (`mm_bot/pricing/oracle.py`) — fetches reference price from the Kuru API
- **Quoter** (`mm_bot/quoter/quoter.py`) — generates bid/ask orders with skew-based pricing per level
- **PositionTracker** (`mm_bot/position/position_tracker.py`) — updates position on order fill events
- **PnlTracker** (`mm_bot/pnl/tracker.py`) — tracks unrealized PnL
- **Bot** (`mm_bot/bot/bot.py`) — orchestrates the order lifecycle
- **Config** (`mm_bot/config/config.py`) — loads configuration from environment variables

## Prerequisites

- Python 3.10+
- The `kuru-mm-python` package (see installation below)

## Installation

**1. Clone and set up a virtual environment:**
```bash
git clone https://github.com/kuru-labs/mm-example.git
cd mm-example
python3 -m venv venv
source venv/bin/activate
```

**2. Install the `kuru-mm-python` SDK:**

If you have the SDK repo cloned locally at `../kuru-mm-python`:
```bash
pip install -e ../kuru-mm-python
```

Or install from PyPI (if available):
```bash
pip install kuru-mm-py
```

**3. Install bot dependencies:**
```bash
pip install -r requirements.txt
```

**4. Configure environment variables:**
```bash
cp .env.example .env
# Edit .env with your settings
```

## Configuration

Create a `.env` file in the project root. All required and optional variables are listed below.

### Required

| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Wallet private key (no `0x` prefix) |
| `MARKET_ADDRESS` | Kuru market contract address |

### Optional (with defaults)

| Variable | Description | Default |
|----------|-------------|---------|
| `RPC_URL` | HTTP RPC endpoint | `https://rpc.fullnode.kuru.io/` |
| `RPC_WS_URL` | WebSocket RPC endpoint | `wss://rpc.fullnode.kuru.io/` |
| `MAX_POSITION` | Maximum position size in base asset | `1000` |
| `QUANTITY` | Order size per quoter level | `100` |
| `QUOTERS_BPS` | Comma-separated spread levels in bps | `25,50,75` |
| `PRICE_UPDATE_THRESHOLD_BPS` | Minimum price move (bps) before refreshing orders | `10` |
| `POSITION_UPDATE_THRESHOLD_BPS` | Position change (bps of max) that triggers refresh | `500` |
| `PROP_SKEW_ENTRY` | Spread widening factor when entering (0–1) | `0.5` |
| `PROP_SKEW_EXIT` | Spread tightening factor when exiting (0–1) | `0.5` |
| `STRATEGY_TYPE` | `long` or `short` | `long` |
| `QUANTITY_BPS_PER_LEVEL` | If set, overrides `QUANTITY` with a position-proportional size | — |
| `OVERRIDE_START_POSITION` | Manually set initial position (skips on-chain fetch) | — |
| `RECONCILE_INTERVAL` | Seconds between position reconciliation (0 = disabled) | `300` |

### Strategy Types

- **`long`**: Widen bid spreads when long (slow to enter), tighten ask spreads (eager to exit)
- **`short`**: Tighten bid spreads when short (eager to exit), widen ask spreads (slow to enter)

### Skew Example

With `baseline_edge = 50 bps`, `PROP_SKEW_ENTRY = 0.5`, and position at 50% of max using the `long` strategy:
- Bid edge = `50 × (1 + 0.5 × 0.5)` = **62.5 bps**
- Ask edge = `50 × (1 - 0.5 × 0.5)` = **37.5 bps**

## Running the Bot

```bash
./run.sh
```

Or manually:
```bash
source venv/bin/activate
PYTHONPATH=. python3 mm_bot/main.py
```

Press `Ctrl+C` to stop — the bot will cancel all active orders before exiting.

## Monitoring

The bot logs order activity, fills, position changes, and PnL to stdout:

```
INFO: Iteration 42: Placing 6 orders, cancelling 6
INFO: Transaction hash: 0x1234...
✓ Order bid-50-1706789123456 filled! Side: buy, Price: 0.0195, Size: 100
INFO: Position update (BUY): +100 base @ 0.0195 | Total: 100
INFO: PnL: -1.95
```

## Project Structure

```
mm-example/
├── mm_bot/
│   ├── main.py                    # Entry point
│   ├── bot/
│   │   └── bot.py                 # Main bot logic
│   ├── quoter/
│   │   └── quoter.py              # Skew-based quoter
│   ├── position/
│   │   └── position_tracker.py    # Position tracking
│   ├── pricing/
│   │   └── oracle.py              # Price oracle
│   ├── pnl/
│   │   └── tracker.py             # PnL tracker
│   └── config/
│       └── config.py              # Configuration loader
├── run.sh                         # Bot runner script
├── install.sh                     # Dependency installer
├── requirements.txt               # Python dependencies
└── .env.example                   # Environment variable template
```

## Troubleshooting

**Orders not placing**
- Verify you have sufficient margin balances (base and quote) on the market
- Confirm `MARKET_ADDRESS` is correct for the asset you intend to trade

**Position drift**
- All fill events are logged — check logs for any gaps
- Adjust `RECONCILE_INTERVAL` to reconcile position against on-chain state periodically

**WebSocket disconnects**
- The SDK has built-in auto-reconnect
- Check that `RPC_WS_URL` is reachable from your network

**No reference price**
- The bot skips iterations when the oracle returns no price
- Check Kuru API availability

## Security

- Never commit your `.env` file — it contains your private key
- Use a dedicated wallet with only the funds needed for market making

## License

MIT
