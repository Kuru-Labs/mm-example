from mm_bot.position.position_tracker import PositionTracker
from mm_bot.pricing.oracle import OracleService
from decimal import Decimal
from typing import Optional
from loguru import logger


class PnlTracker:
    def __init__(self, position_tracker: PositionTracker, oracle_service: OracleService, market_id: str, source_name: str = "kuru"):
        self.position_tracker = position_tracker
        self.oracle_service = oracle_service
        self.market_id = market_id
        self.source_name = source_name

    def get_pnl(self) -> Optional[Decimal]:
        """
        Get the PnL

        Returns:
            PnL
        """
        price = self.oracle_service.get_price(self.market_id, self.source_name)
        if price is None:
            return None

        position = self.position_tracker.get_current_position()
        price_dec = Decimal(str(price))
        return self.position_tracker.get_quote_position() + position * price_dec

    def monitor_pnl(self) -> None:
        # TODO: Implement PnL monitoring loop
        pass

    def print_pnl(self) -> None:
        """
        Print the PnL
        """
        pnl = self.get_pnl()
        if pnl is None:
            logger.debug("PnL unavailable (no price from oracle)")
            return
        logger.info(f"PnL: {float(pnl):.4f}")
        logger.info("=" * 80)
