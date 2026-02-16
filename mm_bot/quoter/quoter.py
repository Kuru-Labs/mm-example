from enum import Enum
from typing import Optional
import time
from loguru import logger

from mm_bot.pricing.oracle import OracleService
from mm_bot.position.position_tracker import PositionTracker
from mm_bot.kuru_imports import Order, OrderType, OrderSide


class StrategyType(Enum):
    LONG = "long"
    SHORT = "short"


class Quoter:
    def __init__(
        self,
        oracle_service: OracleService,
        position_tracker: PositionTracker,
        source_name: str,
        market_id: str,
        strategy_type: StrategyType,
        baseline_edge_bps: float,
        max_position: float,
        prop_skew_entry: float,
        prop_skew_exit: float,
        quantity: float,
        market_config=None,
    ):
        self.oracle_service = oracle_service
        self.position_tracker = position_tracker
        self.source_name = source_name
        self.market_id = market_id
        self.strategy_type = strategy_type
        self.baseline_edge_bps = baseline_edge_bps
        self.max_position = max_position
        self.prop_skew_entry = prop_skew_entry
        self.prop_skew_exit = prop_skew_exit
        self.quantity = quantity
        self.market_config = market_config

    def _cap_value(self, value: float, min_val: float, max_val: float) -> float:
        """Cap a value between min_val and max_val"""
        return max(min_val, min(max_val, value))

    def _round_to_tick(self, price: float) -> float:
        """
        Round price to valid tick size using integer arithmetic.

        This avoids floating point precision errors by doing the rounding
        in integer space (contract format) then converting back.
        """
        if not self.market_config:
            # Fallback to simple rounding if no market config
            return round(price, 6)

        # Convert to contract integer format
        price_int = int(round(price * self.market_config.price_precision))

        # Round to nearest multiple of tick_size using ONLY integer arithmetic
        tick_size = 100  # Hardcoded for MON-USDC
        # Use integer division and modulo to avoid any float operations
        aligned_int = ((price_int + tick_size // 2) // tick_size) * tick_size

        # Convert back to human-readable float
        # Add 0.5 to compensate for SDK using int() instead of round()
        # This ensures int(aligned_price * precision) == aligned_int
        aligned_price = (aligned_int + 0.5) / self.market_config.price_precision

        logger.debug(f"_round_to_tick: input={price:.10f}, price_int={price_int}, aligned_int={aligned_int}, output={aligned_price:.10f}, verify_int={int(aligned_price * self.market_config.price_precision)}, verify_round={int(round(aligned_price * self.market_config.price_precision))}")

        # Ensure it's positive and non-zero
        min_price = (tick_size + 0.5) / self.market_config.price_precision
        return max(aligned_price, min_price)

    def _calculate_prop_of_max_position(self) -> float:
        """Calculate the proportional position relative to max position, capped at [-1, 1]"""
        current_position = self.position_tracker.get_current_position() + self.position_tracker.get_start_position()
        prop_of_max = current_position / self.max_position if self.max_position != 0 else 0
        return self._cap_value(prop_of_max, -1, 1)

    def get_bid_ask_edges(self) -> tuple[Optional[float], Optional[float]]:
        """
        Calculate bid and ask edges in bps based on the strategy type and position

        Returns:
            tuple[Optional[float], Optional[float]]: (bid_edge_bps, ask_edge_bps)
        """
        prop_of_max = self._calculate_prop_of_max_position()

        if self.strategy_type == StrategyType.LONG:
            bid_edge_bps = self.baseline_edge_bps * (1 + prop_of_max * self.prop_skew_entry)
            ask_edge_bps = self.baseline_edge_bps * (1 - prop_of_max * self.prop_skew_exit)
        else:
            bid_edge_bps = self.baseline_edge_bps * (1 - prop_of_max * self.prop_skew_exit)
            ask_edge_bps = self.baseline_edge_bps * (1 + prop_of_max * self.prop_skew_entry)

        return bid_edge_bps, ask_edge_bps

    def get_cancel_edges(self, prop_maintain: float) -> tuple[float, float]:
        """
        Get the cancel edge thresholds (edges below which orders should be cancelled).

        Args:
            prop_maintain: Proportion to maintain (e.g. 0.2 means cancel if edge < 80% of target)

        Returns:
            tuple[float, float]: (bid_cancel_edge_bps, ask_cancel_edge_bps)
        """
        bid_edge_bps, ask_edge_bps = self.get_bid_ask_edges()

        # Cancel edges are reduced by prop_maintain factor
        bid_cancel_edge_bps = bid_edge_bps * (1 - prop_maintain)
        ask_cancel_edge_bps = ask_edge_bps * (1 - prop_maintain)

        return bid_cancel_edge_bps, ask_cancel_edge_bps

    def calculate_order_edge(self, order_price: float, order_side: OrderSide, reference_price: float) -> float:
        """
        Calculate the edge (in bps) of an existing order.

        Args:
            order_price: Price of the existing order
            order_side: Side of the order (BUY or SELL)
            reference_price: Current reference/fair price

        Returns:
            float: Edge in basis points
        """
        if order_side == OrderSide.BUY:
            # Bid edge: how much below fair value
            edge = (reference_price - order_price) / reference_price * 10000
        else:  # SELL
            # Ask edge: how much above fair value
            edge = (order_price - reference_price) / reference_price * 10000

        return edge

    def get_reference_price(self) -> Optional[float]:
        """Get the reference price from the oracle service"""
        return self.oracle_service.get_price(self.market_id, self.source_name)

    def generate_orders(self, reference_price: float, need_bid: bool = True, need_ask: bool = True) -> list[Order]:
        """
        Generate orders for specified sides based on current market conditions.

        Args:
            reference_price: Current reference/fair price
            need_bid: Whether to generate a bid order
            need_ask: Whether to generate an ask order

        Returns:
            list[Order]: List containing requested orders
        """
        if reference_price is None:
            return []

        bid_edge_bps, ask_edge_bps = self.get_bid_ask_edges()

        orders = []
        timestamp = int(time.time() * 1000)

        if need_bid:
            # Calculate bid price
            bid_multiplier = 1 - (bid_edge_bps / 10000)
            bid_price = self._round_to_tick(reference_price * bid_multiplier)
            bid_cloid = f"bid-{self.baseline_edge_bps}-{timestamp}"

            orders.append(Order(
                cloid=bid_cloid,
                order_type=OrderType.LIMIT,
                side=OrderSide.BUY,
                price=bid_price,
                size=self.quantity,
                post_only=False
            ))

        if need_ask:
            # Calculate ask price
            ask_multiplier = 1 + (ask_edge_bps / 10000)
            ask_price = self._round_to_tick(reference_price * ask_multiplier)
            ask_cloid = f"ask-{self.baseline_edge_bps}-{timestamp}"

            orders.append(Order(
                cloid=ask_cloid,
                order_type=OrderType.LIMIT,
                side=OrderSide.SELL,
                price=ask_price,
                size=self.quantity,
                post_only=False
            ))

        return orders

    def get_orders(self) -> list[Order]:
        """
        Generate bid and ask orders based on the current market conditions and strategy.
        Legacy method - generates both sides always.

        Returns:
            list[Order]: List containing bid and ask orders
        """
        reference_price = self.get_reference_price()
        if reference_price is None:
            return []

        return self.generate_orders(reference_price, need_bid=True, need_ask=True)
