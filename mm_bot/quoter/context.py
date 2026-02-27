from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from mm_bot.kuru_imports import Order, OrderSide


@dataclass(frozen=True)
class ExistingOrder:
    """An order the bot knows about, resolved from multiple tracking sources."""
    cloid: str
    side: OrderSide
    price: Optional[Decimal]   # None if price unknown (preregistered / unknown state)
    source: str                 # "on_chain" | "callback" | "preregistered" | "unknown"


@dataclass(frozen=True)
class QuoterContext:
    """
    Snapshot of market state passed to a quoter each iteration.
    Assembled by the Bot -- quoters never touch Bot internals.

    Frozen (immutable) to prevent quoters from mutating shared state.
    New Optional fields with defaults can be added without breaking existing quoters.
    """
    # Market state
    reference_price: Decimal

    # Position state
    current_position: Decimal
    max_position: Decimal

    # This quoter's existing orders (resolved by Bot from its tracking dicts)
    existing_bid: Optional[ExistingOrder] = None
    existing_ask: Optional[ExistingOrder] = None

    # Position limit flags (computed by Bot)
    stop_bids: bool = False     # Position > max_position
    stop_asks: bool = False     # Position < -max_position

    # Strategy params from config (quoters may or may not use these)
    prop_maintain: float = 0.2

    # Market config (for price precision, etc.)
    price_precision: Optional[Decimal] = None


@dataclass
class QuoterDecision:
    """
    A quoter's output: which orders to cancel and which to place.
    """
    cancels: list[str] = field(default_factory=list)      # cloids to cancel
    new_orders: list[Order] = field(default_factory=list)  # orders to place
