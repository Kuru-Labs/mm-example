import os
from dataclasses import dataclass
from typing import List, Optional
from dotenv import load_dotenv
from mm_bot.kuru_imports import initialize_kuru_mm_config, market_config_from_market_address
from mm_bot.quoter.quoter import StrategyType


@dataclass
class BotConfig:
    """Configuration for the market making bot"""
    max_position: float
    prop_skew_entry: float
    prop_skew_exit: float
    quantity: float
    quoters_bps: List[float]
    price_update_threshold_bps: float
    position_update_threshold_bps: float  # Position change (as BPS of max_position) that triggers update
    strategy_type: StrategyType = StrategyType.LONG
    # New parameters
    quantity_bps_per_level: Optional[float] = None  # If set, overrides quantity
    override_start_position: Optional[float] = None  # Manual position override
    reconcile_interval: float = 300  # Seconds between reconciliation (0=disabled)


def load_config_from_env():
    """
    Load all configurations from environment variables.

    Returns:
        tuple: (kuru_config, market_config, bot_config)
    """
    load_dotenv()

    # Load Kuru MM config
    kuru_config = initialize_kuru_mm_config(
        private_key=os.getenv("PRIVATE_KEY"),
        rpc_url=os.getenv("RPC_URL", "https://rpc.fullnode.kuru.io/"),
        rpc_ws_url=os.getenv("RPC_WS_URL", "wss://rpc.fullnode.kuru.io/")
    )

    # Load market config
    market_address = os.getenv("MARKET_ADDRESS", "0x065c9d28e428a0db40191a54d33d5b7c71a9c394")
    market_config = market_config_from_market_address(
        market_address=market_address,
        rpc_url=os.getenv("RPC_URL", "https://rpc.fullnode.kuru.io/")
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
        price_update_threshold_bps=float(os.getenv("PRICE_UPDATE_THRESHOLD_BPS", "10")),
        position_update_threshold_bps=float(os.getenv("POSITION_UPDATE_THRESHOLD_BPS", "500")),
        strategy_type=strategy_type,
        quantity_bps_per_level=quantity_bps_per_level,
        override_start_position=override_start_position,
        reconcile_interval=float(os.getenv("RECONCILE_INTERVAL", "300"))
    )

    return kuru_config, market_config, bot_config
