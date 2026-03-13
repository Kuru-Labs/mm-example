import time
from abc import ABC, abstractmethod
from decimal import Decimal

from mm_bot.kuru_imports import OrderSide
from mm_bot.quoter.context import QuoterContext, QuoterDecision


class BaseQuoter(ABC):
    """
    Abstract base class for all quoter implementations.

    A quoter is responsible for one "level" of quoting -- typically
    one bid/ask pair at a particular spread from mid.

    Implementers must define a single method: decide(ctx) -> QuoterDecision.
    The Bot handles all infrastructure (order tracking, pre-registration,
    callback handling, balance filtering). The quoter handles strategy
    (pricing, cancel/maintain decisions, order construction).
    """

    def __init__(self, quoter_id: str, quantity: Decimal):
        """
        Args:
            quoter_id: Unique identifier for this quoter instance.
                       Used in cloid prefixes for order matching.
                       Must be stable across restarts for the same config.
                       Examples: "10.0", "always-25.0", "vwap-1"
            quantity: Order size for this quoter (base token units).
        """
        self.quoter_id = quoter_id
        self.quantity = quantity

    @property
    def cloid_prefix_bid(self) -> str:
        """Prefix for matching bid cloids back to this quoter."""
        return f"bid-{self.quoter_id}-"

    @property
    def cloid_prefix_ask(self) -> str:
        """Prefix for matching ask cloids back to this quoter."""
        return f"ask-{self.quoter_id}-"

    def owns_cloid(self, cloid: str) -> bool:
        """Check if a cloid belongs to this quoter."""
        return cloid.startswith(self.cloid_prefix_bid) or cloid.startswith(self.cloid_prefix_ask)

    def make_cloid(self, side: str) -> str:
        """Generate a new cloid for this quoter. side is 'bid' or 'ask'."""
        timestamp = int(time.time() * 1000)
        return f"{side}-{self.quoter_id}-{timestamp}"

    @abstractmethod
    def decide(self, ctx: QuoterContext) -> QuoterDecision:
        """
        Given the current market context, decide what to do.

        This is the single method that quoter implementers must define.
        It receives full context and returns cancel/place instructions.

        The Bot handles:
        - Resolving existing orders from its tracking state
        - Executing the cancels and placements
        - Pre-registration and callback tracking
        - Balance filtering

        The quoter handles:
        - Pricing logic (edges, skew, signals, etc.)
        - Cancel/maintain decisions
        - Order construction
        """
        ...

    @classmethod
    def from_config(cls, config_section: dict) -> "BaseQuoter":
        """
        Construct a quoter from a config dict (e.g., a [[strategy.quoters]] section).
        Override this in subclasses to parse type-specific config fields.
        """
        raise NotImplementedError(f"{cls.__name__} must implement from_config()")

    # --- Static helpers (quoters can use or ignore) ---

    @staticmethod
    def calculate_order_edge(
        order_price: Decimal,
        order_side: OrderSide,
        reference_price: Decimal,
    ) -> Decimal:
        """Calculate edge in bps of an existing order relative to reference price."""
        if order_side == OrderSide.BUY:
            return (reference_price - order_price) / reference_price * Decimal("10000")
        else:
            return (order_price - reference_price) / reference_price * Decimal("10000")

    @staticmethod
    def price_from_edge(
        edge_bps: Decimal,
        side: OrderSide,
        reference_price: Decimal,
    ) -> Decimal:
        """Convert an edge in bps to a price."""
        if side == OrderSide.BUY:
            return reference_price * (Decimal("1") - edge_bps / Decimal("10000"))
        else:
            return reference_price * (Decimal("1") + edge_bps / Decimal("10000"))
