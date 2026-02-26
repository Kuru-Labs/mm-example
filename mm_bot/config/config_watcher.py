"""
Hot-reload configuration watcher.

Monitors bot_config.toml for changes and triggers reload callbacks.
"""

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Callable, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from loguru import logger

from .config import BotConfig


class ConfigWatcher:
    """
    Watches bot_config.toml for changes and triggers reload callback.

    Change detection uses both mtime and SHA256 hash to handle:
    - Rapid edits (mtime granularity issues)
    - Touch commands (mtime change without content change)
    - Network file systems (unreliable mtime)
    """

    def __init__(self, config_path: Path, callback: Callable[[BotConfig], None]):
        """
        Initialize config watcher.

        Args:
            config_path: Path to bot_config.toml
            callback: Function to call when config changes (receives new BotConfig)
        """
        self.config_path = config_path
        self.callback = callback
        self.watch_task: Optional[asyncio.Task] = None
        self.running = False

        # Change detection state
        self.last_mtime: float = 0.0
        self.last_hash: str = ""

    async def start(self):
        """Start watching for config changes in background task."""
        if self.running:
            logger.warning("ConfigWatcher already running")
            return

        self.running = True

        # Initialize change detection state
        if self.config_path.exists():
            stat = self.config_path.stat()
            self.last_mtime = stat.st_mtime
            self.last_hash = self._compute_hash()

        self.watch_task = asyncio.create_task(self._watch_loop())
        logger.info(f"🔄 Config watcher started: {self.config_path}")

    async def stop(self):
        """Stop watching and cleanup."""
        if not self.running:
            return

        self.running = False

        if self.watch_task:
            self.watch_task.cancel()
            try:
                await self.watch_task
            except asyncio.CancelledError:
                pass

        logger.info("Config watcher stopped")

    async def _watch_loop(self):
        """Main watch loop: check file every 5 seconds."""
        while self.running:
            try:
                await asyncio.sleep(5)

                if not self.config_path.exists():
                    # File deleted - warn but keep running
                    if self.last_mtime > 0:  # Only warn once
                        logger.warning(f"Config file deleted: {self.config_path}")
                        self.last_mtime = 0
                        self.last_hash = ""
                    continue

                # Check if file changed
                if self._has_changed():
                    logger.info(f"🔄 Config file changed, reloading: {self.config_path}")
                    new_config = self._load_and_validate()

                    if new_config:
                        # Update change detection state
                        stat = self.config_path.stat()
                        self.last_mtime = stat.st_mtime
                        self.last_hash = self._compute_hash()

                        # Trigger callback
                        self.callback(new_config)
                    else:
                        logger.error("❌ Config reload failed. Keeping current config.")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in config watch loop: {e}")
                import traceback
                logger.error(traceback.format_exc())

    def _has_changed(self) -> bool:
        """
        Check if config file has changed.

        Uses both mtime and content hash for robust change detection.

        Returns:
            True if file changed, False otherwise
        """
        if not self.config_path.exists():
            return False

        # Quick check: mtime
        stat = self.config_path.stat()
        if stat.st_mtime <= self.last_mtime:
            return False  # No mtime change

        # Confirm with hash (handles touch commands, network FS issues)
        current_hash = self._compute_hash()
        return current_hash != self.last_hash

    def _compute_hash(self) -> str:
        """Compute SHA256 hash of config file."""
        try:
            return hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        except Exception as e:
            logger.error(f"Failed to compute hash for {self.config_path}: {e}")
            return ""

    def _load_and_validate(self) -> Optional[BotConfig]:
        """
        Load and validate TOML configuration.

        Returns:
            BotConfig if successful, None if error
        """
        try:
            # Parse TOML
            with open(self.config_path, "rb") as f:
                config_dict = tomllib.load(f)

            # Extract strategy section
            if "strategy" not in config_dict:
                logger.error("Config missing [strategy] section")
                return None

            strategy = config_dict["strategy"]

            # Validate all parameters
            errors = []

            # Hot-reloadable params
            if "prop_maintain" not in strategy:
                errors.append("Missing prop_maintain")
            elif not validate_prop_maintain(strategy["prop_maintain"]):
                errors.append(f"Invalid prop_maintain: {strategy['prop_maintain']} (must be 0.0 to 1.0)")

            if "reconcile_interval" not in strategy:
                errors.append("Missing reconcile_interval")
            elif not validate_reconcile_interval(strategy["reconcile_interval"]):
                errors.append(
                    f"Invalid reconcile_interval: {strategy['reconcile_interval']} (must be >= 0.0)"
                )

            # Reinit-required params
            if "max_position" not in strategy:
                errors.append("Missing max_position")
            elif not validate_max_position(strategy["max_position"]):
                errors.append(f"Invalid max_position: {strategy['max_position']} (must be > 0.0)")

            if "prop_skew_entry" not in strategy:
                errors.append("Missing prop_skew_entry")
            elif not validate_prop_skew(strategy["prop_skew_entry"]):
                errors.append(f"Invalid prop_skew_entry: {strategy['prop_skew_entry']} (must be >= 0.0)")

            if "prop_skew_exit" not in strategy:
                errors.append("Missing prop_skew_exit")
            elif not validate_prop_skew(strategy["prop_skew_exit"]):
                errors.append(f"Invalid prop_skew_exit: {strategy['prop_skew_exit']} (must be >= 0.0)")

            if "quantity" not in strategy:
                errors.append("Missing quantity")
            elif not validate_quantity(strategy["quantity"]):
                errors.append(f"Invalid quantity: {strategy['quantity']} (must be > 0.0)")

            if "quoters_bps" not in strategy:
                errors.append("Missing quoters_bps")
            elif not validate_quoters_bps(strategy["quoters_bps"]):
                errors.append(
                    f"Invalid quoters_bps: {strategy['quoters_bps']} (must be non-empty list of positive numbers)"
                )

            # Optional params
            if "oracle_source" not in strategy:
                errors.append("Missing oracle_source")
            elif strategy["oracle_source"] not in ["kuru", "coinbase"]:
                errors.append(
                    f"Invalid oracle_source: {strategy['oracle_source']} (must be 'kuru' or 'coinbase')"
                )

            # Coinbase symbol required when using coinbase oracle
            if strategy.get("oracle_source") == "coinbase":
                if "coinbase_symbol" not in strategy or not strategy["coinbase_symbol"]:
                    errors.append("coinbase_symbol required when oracle_source='coinbase'")

            # Report all validation errors
            if errors:
                for error in errors:
                    logger.error(f"Config validation error: {error}")
                return None

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

        except Exception as e:
            if isinstance(e, (tomllib.TOMLDecodeError if sys.version_info >= (3, 11) else Exception)):
                logger.error(f"Failed to parse TOML: {e}")
            else:
                logger.error(f"Unexpected error loading config: {e}")
                import traceback
                logger.error(traceback.format_exc())
            return None


# Validation functions


def validate_prop_maintain(value: float) -> bool:
    """Validate prop_maintain is between 0 and 1."""
    return 0.0 <= value <= 1.0


def validate_reconcile_interval(value: float) -> bool:
    """Validate reconcile_interval is non-negative."""
    return value >= 0.0


def validate_max_position(value: float) -> bool:
    """Validate max_position is positive."""
    return value > 0.0


def validate_prop_skew(value: float) -> bool:
    """Validate prop_skew_entry/exit is non-negative."""
    return value >= 0.0


def validate_quantity(value: float) -> bool:
    """Validate quantity is positive."""
    return value > 0.0


def validate_quoters_bps(value: list) -> bool:
    """Validate quoters_bps is non-empty list of positive numbers."""
    if not isinstance(value, list) or len(value) == 0:
        return False
    return all(isinstance(x, (int, float)) and x > 0 for x in value)
