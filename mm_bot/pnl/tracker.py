from mm_bot.position.position_tracker import PositionTracker
from mm_bot.pricing.oracle import OracleService
from loguru import logger


class PnlTracker:
    def __init__(self, position_tracker: PositionTracker, oracle_service: OracleService, market_id: str, source_name: str = "kuru"):
        self.position_tracker = position_tracker
        self.oracle_service = oracle_service
        self.market_id = market_id
        self.source_name = source_name

    def get_pnl(self) -> float:
        """
        Get the PnL

        Returns:
            PnL
        """
        price = self.oracle_service.get_price(self.market_id, self.source_name)

        position = self.position_tracker.get_current_position()
        return self.position_tracker.get_quote_position() + position * price

    def monitor_pnl(self) -> None:
        # TODO: Implement PnL monitoring loop
        pass

    def print_pnl(self) -> None:
        """
        Print the PnL
        """
        pnl = self.get_pnl()
        logger.info(f"PnL: {pnl:.4f}")
        logger.info("=" * 80)
