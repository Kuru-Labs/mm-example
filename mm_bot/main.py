import asyncio
import signal
import logging
import os
import sys
from pathlib import Path
from datetime import datetime
from loguru import logger

from mm_bot.config.config import load_config_from_env, load_secrets_from_env, load_operational_config
from mm_bot.bot.bot import Bot


class InterceptHandler(logging.Handler):
    """
    Intercept standard library logging and route to loguru.
    This allows us to control SDK logs (which use standard logging).
    """
    def emit(self, record):
        # Get corresponding loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where the logged message originated
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


async def main():
    """
    Main entry point for the market making bot.
    """
    # Load .env early so SDK_LOG_LEVEL and other vars are available before setup
    from dotenv import load_dotenv
    load_dotenv()

    # Configure loguru: remove default DEBUG console handler, replace with configured level
    sdk_log_level = os.getenv("SDK_LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(
        sys.stderr,
        format="{time:HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}",
        level=sdk_log_level,
    )

    # Setup file logging
    log_dir = Path("tracking")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"bot_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    # Add file handler (keep console output too)
    logger.add(
        log_file,
        format="{time:HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}",
        level=sdk_log_level,
        rotation="50 MB",
        retention="7 days",
        compression="zip"
    )

    # Intercept standard library logging (used by SDK and other libraries)
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)

    # Set SDK log level from environment variable
    # SDK_LOG_LEVEL options: DEBUG, INFO, WARNING, ERROR (default: INFO)
    log_level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR
    }

    sdk_level = log_level_map.get(sdk_log_level, logging.INFO)

    # Set log levels for various modules
    logging.getLogger("kuru_sdk_py").setLevel(sdk_level)  # SDK modules
    logging.getLogger("kuru_sdk_py.feed").setLevel(logging.ERROR)  # SDK feed module (very noisy)
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # HTTP library (very noisy)
    logging.getLogger("websockets").setLevel(logging.WARNING)  # WebSocket library
    logging.getLogger("asyncio").setLevel(logging.WARNING)  # Asyncio internals
    logging.getLogger("web3").setLevel(logging.WARNING)  # Web3 library
    logging.getLogger("eth_rpc").setLevel(logging.WARNING)  # Ethereum RPC
    logging.getLogger("aiohttp").setLevel(logging.WARNING)  # Async HTTP client

    # Set root logger to INFO to catch any other noisy DEBUG loggers
    logging.getLogger().setLevel(logging.INFO)

    # Disable debug logs from noisy SDK modules using loguru
    logger.disable("kuru_sdk_py.feed.rpc_ws")  # Completely disable the noisy feed logs

    logger.info(f"📝 Logging to file: {log_file}")
    logger.info(f"🔧 SDK log level: {sdk_log_level}")

    # Load configuration
    logger.info("Loading configuration...")

    # Load operational config from TOML (with .env fallback)
    config_path = Path("bot_config.toml")
    if config_path.exists():
        bot_config = load_operational_config(config_path)
        logger.info(f"🔧 Loaded operational config from: {config_path}")
        # Load SDK configs from .env (use market_address from config if provided)
        sdk_configs = load_secrets_from_env(
            market_address=bot_config.market_address
        )
    else:
        logger.warning("bot_config.toml not found, falling back to .env (hot-reload disabled)")
        # Use legacy .env loader for everything
        sdk_configs, bot_config = load_config_from_env()

    market_config = sdk_configs["market_config"]

    logger.info(f"Market: {market_config.market_symbol}")
    logger.info(f"Max Position: {bot_config.max_position}")
    logger.info(f"Quantity per order: {bot_config.quantity}")
    logger.info(f"Quoters (bps): {bot_config.quoters_bps}")

    # Create bot
    bot = Bot(sdk_configs, bot_config)

    # Setup signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()

    def handle_shutdown():
        """Handle shutdown signals gracefully"""
        logger.warning("\n🛑 Shutdown signal received...")
        bot.shutdown_event.set()

    loop.add_signal_handler(signal.SIGINT, handle_shutdown)
    loop.add_signal_handler(signal.SIGTERM, handle_shutdown)

    try:
        # Start bot
        await bot.start()

    except KeyboardInterrupt:
        logger.warning("\n🛑 Keyboard interrupt received...")

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

    finally:
        # Cleanup signal handlers
        try:
            loop.remove_signal_handler(signal.SIGINT)
            loop.remove_signal_handler(signal.SIGTERM)
        except Exception:
            pass

        # Stop bot
        await bot.stop()

        logger.success("✓ Bot shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
