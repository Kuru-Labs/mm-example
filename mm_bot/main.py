import asyncio
import signal
from pathlib import Path
from datetime import datetime
from loguru import logger

from mm_bot.config.config import load_config_from_env
from mm_bot.bot.bot import Bot


async def main():
    """
    Main entry point for the market making bot.
    """
    # Setup file logging
    log_dir = Path("tracking")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"bot_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    # Add file handler (keep console output too)
    logger.add(
        log_file,
        format="{time:HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}",
        level="DEBUG",
        rotation="50 MB",
        retention="7 days",
        compression="zip"
    )

    logger.info(f"📝 Logging to file: {log_file}")

    # Load configuration
    logger.info("Loading configuration...")
    wallet_config, connection_config, market_config, bot_config = load_config_from_env()

    logger.info(f"Market: {market_config.market_symbol}")
    logger.info(f"Max Position: {bot_config.max_position}")
    logger.info(f"Quantity per order: {bot_config.quantity}")
    logger.info(f"Quoters (bps): {bot_config.quoters_bps}")
    logger.info(f"Strategy: {bot_config.strategy_type.value}")

    # Create bot
    bot = Bot(connection_config, wallet_config, market_config, bot_config)

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
