# Market Making Bot - kuru-mm-py Migration

A market making bot migrated from the old kuru-sdk to the new kuru-mm-py SDK. This bot implements a skew-based quoting strategy with event-driven order tracking.

## Features

- **Event-driven architecture**: WebSocket callbacks for real-time order updates
- **Skew-based quoting**: Adjusts spreads based on position relative to max position
- **Multi-level quoters**: Multiple price levels (configurable via bps)
- **Position tracking**: Automatic position updates on order fills
- **PnL tracking**: Real-time profit/loss calculation
- **Price threshold**: Only updates orders when price moves beyond threshold
- **Graceful shutdown**: Cancels all active orders on exit

## Architecture

### Components

1. **OracleService** (`src/pricing/oracle.py`)
   - Fetches reference prices from Kuru API
   - Extensible to support multiple price sources

2. **PositionTracker** (`src/position/position_tracker.py`)
   - Tracks position changes via order fill callbacks
   - Maintains current position and quote position

3. **Quoter** (`src/quoter/quoter.py`)
   - Generates bid/ask orders with skew-based pricing
   - Supports LONG and SHORT strategies
   - Adjusts spreads based on position

4. **PnlTracker** (`src/pnl/tracker.py`)
   - Calculates unrealized PnL
   - Formula: quote_position + (current_position * current_price)

5. **Bot** (`src/bot/bot.py`)
   - Main orchestrator
   - Manages order lifecycle via callbacks
   - Combines cancel + place in single transaction

6. **Config** (`src/config/config.py`)
   - Loads configuration from environment variables
   - Initializes SDK configs

## Installation

### Prerequisites

This bot requires the `kuru-mm-python` package to be available. The bot will automatically detect if it's installed as a package or available locally.

1. **Ensure kuru-mm-python is available**:
   - Either have it cloned at `../kuru-mm-python` (relative to this repo)
   - Or have `kuru-mm-py` installed in your Python environment

2. **Install dependencies**:
```bash
cd /Users/devblixt/Documents/GitHub/mm-example
pip3 install -r requirements.txt
```

Alternatively, use the installation script:
```bash
./install.sh
```

3. **Create `.env` file**:
```bash
cp .env.example .env
```

4. **Edit `.env` with your configuration**:
```bash
PRIVATE_KEY=your_private_key_here
RPC_URL=https://rpc.fullnode.kuru.io/
RPC_WS_URL=wss://rpc.fullnode.kuru.io/
MARKET_ADDRESS=0x065C9d28E428A0db40191a54d33d5b7c71a9C394
MAX_POSITION=1000
PROP_SKEW_ENTRY=0.5
PROP_SKEW_EXIT=0.5
QUANTITY=100
QUOTERS_BPS=25,50,75
PRICE_UPDATE_THRESHOLD_BPS=10
STRATEGY_TYPE=long
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PRIVATE_KEY` | Your wallet private key | Required |
| `RPC_URL` | Ethereum RPC endpoint | `https://rpc.fullnode.kuru.io/` |
| `RPC_WS_URL` | WebSocket RPC endpoint | `wss://rpc.fullnode.kuru.io/` |
| `MARKET_ADDRESS` | Market contract address | `0x065C9d28E428A0db40191a54d33d5b7c71a9C394` |
| `MAX_POSITION` | Maximum position size | `1000` |
| `PROP_SKEW_ENTRY` | Entry skew proportion (0-1) | `0.5` |
| `PROP_SKEW_EXIT` | Exit skew proportion (0-1) | `0.5` |
| `QUANTITY` | Order size per quoter | `100` |
| `QUOTERS_BPS` | Comma-separated bps levels | `25,50,75` |
| `PRICE_UPDATE_THRESHOLD_BPS` | Price change threshold to update orders | `10` |
| `STRATEGY_TYPE` | Strategy type: `long` or `short` | `long` |

### Strategy Types

- **LONG**: Wider bids when long, tighter asks to exit
- **SHORT**: Tighter bids when short, wider asks to exit

### Skew Parameters

- `PROP_SKEW_ENTRY`: How much to widen spreads when entering positions (0 = no skew, 1 = max skew)
- `PROP_SKEW_EXIT`: How much to tighten spreads when exiting positions

Example: With `baseline_edge_bps=50`, `PROP_SKEW_ENTRY=0.5`, and position at 50% of max:
- LONG strategy: bid edge = 50 * (1 + 0.5 * 0.5) = 62.5 bps, ask edge = 50 * (1 - 0.5 * 0.5) = 37.5 bps

## Usage

### Run the bot:
```bash
python src/main.py
```

### Stop the bot:
Press `Ctrl+C` for graceful shutdown (cancels all active orders)

## Order Lifecycle

1. **Order Creation**: Quoter generates orders with unique CLOIDs
2. **Order Submission**: Bot combines cancels + new orders in single transaction
3. **Order Confirmation**: Callback updates `active_cloids` set
4. **Order Fill**: Position tracker updates position on fill
5. **Order Cancellation**: Next iteration cancels old orders before placing new ones

### CLOID Format

CLOIDs are unique per quoter and timestamp:
- Format: `{side}-{baseline_edge_bps}-{timestamp_ms}`
- Example: `bid-50-1706789123456`

## Monitoring

The bot logs:
- Order placements and cancellations
- Order fills (with price and size)
- Position updates
- PnL calculations
- Transaction hashes

Example output:
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
├── src/
│   ├── main.py                   # Entry point
│   ├── bot/
│   │   └── bot.py                # Main bot logic
│   ├── quoter/
│   │   └── quoter.py             # Skew-based quoter
│   ├── position/
│   │   └── position_tracker.py   # Position tracking
│   ├── pricing/
│   │   └── oracle.py             # Price oracle
│   ├── pnl/
│   │   └── tracker.py            # PnL tracker
│   └── config/
│       └── config.py             # Configuration loader
├── .env.example                   # Environment template
├── requirements.txt               # Python dependencies
└── README.md                      # This file
```

## Migration from Old SDK

This bot was migrated from kuru-sdk (v0.2.8) to kuru-mm-py. Key changes:

### Architecture Changes
- **Polling → Event-driven**: Position tracking now uses callbacks instead of polling
- **Manual nonce → Automatic**: SDK handles nonce management
- **Separate queues → Unified API**: `place_orders()` handles both cancel and place
- **CLOID tracking**: Unique CLOIDs generated per quoter + timestamp

### SDK API Changes
- `ClientOrderExecutor` → `KuruClient`
- `OrderRequest` → `Order` dataclass
- Added `OrderType`, `OrderSide`, `OrderStatus` enums
- `await KuruClient.create()` factory pattern

### Position Tracking
- Old: `PositionManager` with 0.5s polling
- New: `PositionTracker` with fill event callbacks

### Order Placement
- Old: Separate `PlaceOrderQueue` and `CancelQueue`
- New: Single `client.place_orders([cancel_orders + new_orders])`

## Development

### Running Tests
```bash
# TODO: Add unit tests
pytest tests/
```

### Code Style
```bash
# Format code
black src/

# Lint
ruff check src/
```

## Troubleshooting

### WebSocket Connection Issues
- Check `RPC_WS_URL` is correct
- Ensure firewall allows WebSocket connections
- SDK has auto-reconnect built-in

### Position Drift
- Position tracker logs all fills
- Check logs for missed fill events
- Consider adding periodic reconciliation (future enhancement)

### Orders Not Placing
- Check margin balances (base and quote)
- Verify price precision matches market tick size
- Check for CLOID collisions (should be impossible with timestamp-based generation)

### Price Fetching Fails
- Oracle falls back to None if API fails
- Bot skips iteration if no reference price
- Check Kuru API status

## Security

- **Never commit `.env` file** - contains private key
- Use environment variables for all secrets
- Run bot in secure environment
- Monitor for unusual activity

## License

MIT

## Support

For issues or questions:
- GitHub Issues: https://github.com/anthropics/claude-code/issues
- Kuru Docs: https://docs.kuru.io
