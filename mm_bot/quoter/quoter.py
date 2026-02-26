from typing import Optional
from decimal import Decimal
import time
from loguru import logger

from mm_bot.pricing.oracle import OracleService
from mm_bot.position.position_tracker import PositionTracker
from mm_bot.kuru_imports import Order, OrderType, OrderSide


class Quoter:
    def __init__(
        self,
        oracle_service: OracleService,
        position_tracker: PositionTracker,
        source_name: str,
        market_id: str,
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
        self.market_config = market_config
        self.baseline_edge_bps = Decimal(str(baseline_edge_bps))
        self.max_position = Decimal(str(max_position))
        self.prop_skew_entry = Decimal(str(prop_skew_entry))
        self.prop_skew_exit = Decimal(str(prop_skew_exit))
        self.quantity = Decimal(str(quantity))

    def _to_decimal(self, value) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def _cap_value(self, value: Decimal, min_val: Decimal, max_val: Decimal) -> Decimal:
        """Cap a value between min_val and max_val"""
        return max(min_val, min(max_val, value))

    def _calculate_prop_of_max_position(self) -> Decimal:
        """Calculate the proportional position relative to max position, capped at [-1, 1]"""
        current_position = self.position_tracker.get_current_position()
        prop_of_max = current_position / self.max_position if self.max_position != 0 else Decimal("0")
        return self._cap_value(prop_of_max, Decimal("-1"), Decimal("1"))

    def get_bid_ask_edges(self) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """
        Calculate bid and ask edges in bps based on the strategy type and position

        Returns:
            tuple[Optional[float], Optional[float]]: (bid_edge_bps, ask_edge_bps)
        """
        prop_of_max = self._calculate_prop_of_max_position()

        if prop_of_max > 0:
            # Currently long: widen asks, tighten bids to mean-revert
            bid_edge_bps = self.baseline_edge_bps * (Decimal("1") + prop_of_max * self.prop_skew_entry)
            ask_edge_bps = self.baseline_edge_bps * (Decimal("1") - prop_of_max * self.prop_skew_exit)
        else:
            # Currently short: widen bids, tighten asks to mean-revert
            bid_edge_bps = self.baseline_edge_bps * (Decimal("1") - prop_of_max * self.prop_skew_exit)
            ask_edge_bps = self.baseline_edge_bps * (Decimal("1") + prop_of_max * self.prop_skew_entry)

        return bid_edge_bps, ask_edge_bps

    def get_cancel_edges(self, prop_maintain: float) -> tuple[Decimal, Decimal]:
        """
        Get the cancel edge thresholds (edges below which orders should be cancelled).

        Args:
            prop_maintain: Proportion to maintain (e.g. 0.2 means cancel if edge < 80% of target)

        Returns:
            tuple[float, float]: (bid_cancel_edge_bps, ask_cancel_edge_bps)
        """
        bid_edge_bps, ask_edge_bps = self.get_bid_ask_edges()

        # Cancel edges are reduced by prop_maintain factor
        maintain = self._to_decimal(prop_maintain)
        bid_cancel_edge_bps = bid_edge_bps * (Decimal("1") - maintain)
        ask_cancel_edge_bps = ask_edge_bps * (Decimal("1") - maintain)

        return bid_cancel_edge_bps, ask_cancel_edge_bps

    def calculate_order_edge(self, order_price: Decimal, order_side: OrderSide, reference_price: Decimal) -> Decimal:
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
            edge = (reference_price - order_price) / reference_price * Decimal("10000")
        else:  # SELL
            # Ask edge: how much above fair value
            edge = (order_price - reference_price) / reference_price * Decimal("10000")

        return edge

    def get_reference_price(self) -> Optional[Decimal]:
        """Get the reference price from the oracle service"""
        reference_price = self.oracle_service.get_price(self.market_id, self.source_name)
        if reference_price is None:
            return None
        return self._to_decimal(reference_price)

    def generate_orders(self, reference_price: Decimal, need_bid: bool = True, need_ask: bool = True) -> list[Order]:
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
            bid_multiplier = Decimal("1") - (bid_edge_bps / Decimal("10000"))
            # Tick rounding is delegated to SDK place_orders(price_rounding="default")
            bid_price = reference_price * bid_multiplier
            bid_cloid = f"bid-{self.baseline_edge_bps}-{timestamp}"

            orders.append(Order(
                cloid=bid_cloid,
                order_type=OrderType.LIMIT,
                side=OrderSide.BUY,
                price=bid_price,
                size=self.quantity,
                post_only=False
            ))
            logger.debug(
                f"New bid: cloid={bid_cloid} price={float(bid_price):.6f} "
                f"size={float(self.quantity)} edge={float(bid_edge_bps):.2f}bps"
            )

        if need_ask:
            # Calculate ask price
            ask_multiplier = Decimal("1") + (ask_edge_bps / Decimal("10000"))
            # Tick rounding is delegated to SDK place_orders(price_rounding="default")
            ask_price = reference_price * ask_multiplier
            ask_cloid = f"ask-{self.baseline_edge_bps}-{timestamp}"

            orders.append(Order(
                cloid=ask_cloid,
                order_type=OrderType.LIMIT,
                side=OrderSide.SELL,
                price=ask_price,
                size=self.quantity,
                post_only=False
            ))
            logger.debug(
                f"New ask: cloid={ask_cloid} price={float(ask_price):.6f} "
                f"size={float(self.quantity)} edge={float(ask_edge_bps):.2f}bps"
            )

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
