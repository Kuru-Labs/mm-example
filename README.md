# Kuru Market Making Bot

A market-making bot for Kuru DEX using the refactored `kuru-sdk-py` client.

## What Changed

This repo now tracks SDK v0.1.9+ behavior:

- Decimal-native order/position math in bot state and fill handling
- Full `ConfigManager.load_all_configs(...)` bootstrap path
- Typed SDK error handling (`Kuru*Error`) for retries and recovery
- Cancel-all flow aligned with SDK semantics (no tx-hash return assumptions)

## Architecture

Core modules:

- `mm_bot/main.py`: process startup, logging, signal handling
- `mm_bot/config/config.py`: loads operational config (`bot_config.toml`) and SDK config bundle
- `mm_bot/bot/bot.py`: quoting loop, order lifecycle callbacks, cancellation/reconciliation, typed recovery
- `mm_bot/quoter/quoter.py`: skewed bid/ask generation
- `mm_bot/position/position_tracker.py`: Decimal-native position persistence (`tracking/position_state.json`)
- `mm_bot/pricing/oracle.py`: oracle sources (`kuru` websocket or `coinbase` REST)
- `mm_bot/pnl/tracker.py`: Decimal-native PnL display

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
