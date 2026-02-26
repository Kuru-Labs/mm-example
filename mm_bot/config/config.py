import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from mm_bot.kuru_imports import ConfigManager


@dataclass
class BotConfig:
    """Configuration for the market making bot"""
    max_position: float
    prop_skew_entry: float
    prop_skew_exit: float
    quantity: float
    quoters_bps: List[float]
    prop_maintain: float  # Cancel threshold factor (0.2 = keep orders with edge >= 80% of target)
    quantity_bps_per_level: Optional[float] = None  # If set, overrides quantity
    override_start_position: Optional[float] = None  # Manual position override
    reconcile_interval: float = 300  # Seconds between reconciliation (0=disabled)
    oracle_source: str = "coinbase"  # "kuru" for Kuru orderbook mid, "coinbase" for Coinbase API
    coinbase_symbol: Optional[str] = None  # Required when oracle_source="coinbase"
    market_address: Optional[str] = None  # Market address (restart required to change)


def load_secrets_from_env(market_address: Optional[str] = None):
    """
    Load secrets (wallet, connection, market) from .env file.

    Args:
        market_address: Optional market address (overrides .env if provided)

    Returns:
        tuple: (wallet_config, connection_config, market_config)
    """
    load_dotenv()

    # Load wallet config (reads PRIVATE_KEY from env)
    wallet_config = ConfigManager.load_wallet_config()

    # Load connection config (reads RPC_URL, RPC_WS_URL, etc. from env with defaults)
    connection_config = ConfigManager.load_connection_config()

    # Use provided market_address or fall back to .env
    if market_address is None:
        market_address = os.getenv("MARKET_ADDRESS", "0x065c9d28e428a0db40191a54d33d5b7c71a9c394")

    # Load cache config
    cache_config = ConfigManager.load_cache_config()

    # Load market config from chain
    market_config = ConfigManager.load_market_config(
        market_address=market_address,
        fetch_from_chain=True,
    )

    return wallet_config, connection_config, cache_config, market_config


def load_operational_config(toml_path: Path) -> BotConfig:
    """
    Load operational configuration from TOML file.

    Args:
        toml_path: Path to bot_config.toml

    Returns:
        BotConfig with all operational parameters

    Raises:
        ValueError: If validation fails
        FileNotFoundError: If TOML file doesn't exist
    """
    if not toml_path.exists():
        raise FileNotFoundError(f"Config file not found: {toml_path}")

    # Parse TOML
    with open(toml_path, "rb") as f:
        config_dict = tomllib.load(f)

    # Extract strategy section
    if "strategy" not in config_dict:
        raise ValueError("Config missing [strategy] section")

    strategy = config_dict["strategy"]

    # Validate required parameters
    required_params = [
        "prop_maintain",
        "reconcile_interval",
        "max_position",
        "prop_skew_entry",
        "prop_skew_exit",
        "quantity",
        "quoters_bps",
        "oracle_source",
    ]

    for param in required_params:
        if param not in strategy:
            raise ValueError(f"Missing required parameter: {param}")

    # Validate oracle_source and coinbase_symbol
    if strategy["oracle_source"] not in ["kuru", "coinbase"]:
        raise ValueError(f"Invalid oracle_source: {strategy['oracle_source']} (must be 'kuru' or 'coinbase')")

    if strategy["oracle_source"] == "coinbase":
        if "coinbase_symbol" not in strategy or not strategy["coinbase_symbol"]:
            raise ValueError("coinbase_symbol required when oracle_source='coinbase'")

    # Build BotConfig
    return BotConfig(
        prop_maintain=float(strategy["prop_maintain"]),
        reconcile_interval=float(strategy["reconcile_interval"]),
        max_position=float(strategy["max_position"]),
        prop_skew_entry=float(strategy["prop_skew_entry"]),
        prop_skew_exit=float(strategy["prop_skew_exit"]),
        quantity=float(strategy["quantity"]),
        quantity_bps_per_level=(
            float(strategy["quantity_bps_per_level"])
            if strategy.get("quantity_bps_per_level") is not None
            else None
        ),
        quoters_bps=[float(x) for x in strategy["quoters_bps"]],
        oracle_source=strategy["oracle_source"],
        coinbase_symbol=strategy.get("coinbase_symbol"),
        market_address=strategy.get("market_address"),
        override_start_position=(
            float(strategy["override_start_position"])
            if strategy.get("override_start_position") is not None
            else None
        ),
    )


def load_config_from_env():
    """
    Load all configurations from environment variables (legacy fallback).

    Returns:
        tuple: (wallet_config, connection_config, market_config, bot_config)
    """
    load_dotenv()

    # Load cache config
    cache_config = ConfigManager.load_cache_config()

    # Load secrets
    wallet_config, connection_config, market_config = load_secrets_from_env()

    # Load bot config from .env (fallback when TOML doesn't exist)
    quoters_bps_str = os.getenv("QUOTERS_BPS", "25,50,75")
    quoters_bps = [float(x.strip()) for x in quoters_bps_str.split(",")]

    # Load quantity (can be overridden by quantity_bps_per_level)
    quantity = float(os.getenv("QUANTITY", "100"))

    # Load optional quantity_bps_per_level (overrides quantity if set)
    quantity_bps_str = os.getenv("QUANTITY_BPS_PER_LEVEL")
    quantity_bps_per_level = float(quantity_bps_str) if quantity_bps_str and quantity_bps_str.strip() else None

    # Load optional override_start_position
    override_pos_str = os.getenv("OVERRIDE_START_POSITION")
    override_start_position = float(override_pos_str) if override_pos_str and override_pos_str.strip() else None

    bot_config = BotConfig(
        max_position=float(os.getenv("MAX_POSITION", "1000")),
        prop_skew_entry=float(os.getenv("PROP_SKEW_ENTRY", "0.5")),
        prop_skew_exit=float(os.getenv("PROP_SKEW_EXIT", "0.5")),
        quantity=quantity,
        quoters_bps=quoters_bps,
        prop_maintain=float(os.getenv("PROP_MAINTAIN", "0.2")),
        quantity_bps_per_level=quantity_bps_per_level,
        override_start_position=override_start_position,
        reconcile_interval=float(os.getenv("RECONCILE_INTERVAL", "300")),
        oracle_source=os.getenv("ORACLE", "coinbase").lower(),
        coinbase_symbol=os.getenv("COINBASE_SYMBOL"),
        market_address=os.getenv("MARKET_ADDRESS"),
    )

    if bot_config.oracle_source == "coinbase" and not bot_config.coinbase_symbol:
        raise ValueError(
            "COINBASE_SYMBOL is required when ORACLE=coinbase (e.g. COINBASE_SYMBOL=MON-USD)"
        )


    return wallet_config, connection_config, cache_config, market_config, bot_config

