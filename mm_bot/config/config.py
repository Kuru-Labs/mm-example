import os
from dataclasses import dataclass
from typing import List, Optional
from dotenv import load_dotenv
from mm_bot.kuru_imports import ConfigManager
from mm_bot.quoter.quoter import StrategyType


@dataclass
class BotConfig:
    """Configuration for the market making bot"""
    max_position: float
    prop_skew_entry: float
    prop_skew_exit: float
    quantity: float
    quoters_bps: List[float]
    prop_maintain: float  # Cancel threshold factor (0.2 = keep orders with edge >= 80% of target)
    strategy_type: StrategyType = StrategyType.LONG
    quantity_bps_per_level: Optional[float] = None  # If set, overrides quantity
    override_start_position: Optional[float] = None  # Manual position override
    reconcile_interval: float = 300  # Seconds between reconciliation (0=disabled)
    oracle_source: str = "coinbase"  # "kuru" for Kuru orderbook mid, "coinbase" for Coinbase API


def load_config_from_env():
    """
    Load all configurations from environment variables.

    Returns:
        tuple: (wallet_config, connection_config, market_config, bot_config)
    """
    load_dotenv()

    # Load wallet config (reads PRIVATE_KEY from env)
    wallet_config = ConfigManager.load_wallet_config()

    # Load connection config (reads RPC_URL, RPC_WS_URL, etc. from env with defaults)
    connection_config = ConfigManager.load_connection_config()

    # Load market config from chain
    market_config = ConfigManager.load_market_config(
        market_address=os.getenv("MARKET_ADDRESS", "0x065c9d28e428a0db40191a54d33d5b7c71a9c394"),
        fetch_from_chain=True,
    )

    # Load bot config
    quoters_bps_str = os.getenv("QUOTERS_BPS", "25,50,75")
    quoters_bps = [float(x.strip()) for x in quoters_bps_str.split(",")]

    strategy_type_str = os.getenv("STRATEGY_TYPE", "long").lower()
    strategy_type = StrategyType.LONG if strategy_type_str == "long" else StrategyType.SHORT

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
        strategy_type=strategy_type,
        quantity_bps_per_level=quantity_bps_per_level,
        override_start_position=override_start_position,
        reconcile_interval=float(os.getenv("RECONCILE_INTERVAL", "300")),
        oracle_source=os.getenv("ORACLE", "coinbase").lower()
    )

    return wallet_config, connection_config, market_config, bot_config
