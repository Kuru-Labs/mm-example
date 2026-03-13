from decimal import Decimal
from typing import Optional

from loguru import logger

from mm_bot.kuru_imports import Order, OrderType, OrderSide
from mm_bot.quoter.base import BaseQuoter
from mm_bot.quoter.context import ExistingOrder, QuoterContext, QuoterDecision


class SkewQuoter(BaseQuoter):
    """
    Position-skew quoter with PropMaintain cancel logic.

    This is the original quoter strategy. It:
    - Widens/tightens edges based on position as a proportion of max_position
    - Only cancels orders whose edge has drifted below a threshold (PropMaintain)
    - Couples bid/ask: if one side is replaced, the other is force-replaced too
    """

    def __init__(
        self,
        baseline_edge_bps: float,
        quantity: Decimal,
        prop_skew_entry: float = 0.5,
        prop_skew_exit: float = 0.5,
    ):
        # quoter_id embeds baseline_edge_bps for backward-compatible cloid format
        # e.g. "10.0" produces cloids like "bid-10.0-1771500973306"
        quoter_id = str(Decimal(str(baseline_edge_bps)))
        super().__init__(quoter_id=quoter_id, quantity=quantity)
        self.baseline_edge_bps = Decimal(str(baseline_edge_bps))
        self.prop_skew_entry = Decimal(str(prop_skew_entry))
        self.prop_skew_exit = Decimal(str(prop_skew_exit))

    def _get_skewed_edges(self, ctx: QuoterContext) -> tuple[Decimal, Decimal]:
        """Calculate bid/ask edges with position skew applied."""
        if ctx.max_position != 0:
            prop_of_max = ctx.current_position / ctx.max_position
        else:
            prop_of_max = Decimal("0")
        prop_of_max = max(Decimal("-1"), min(Decimal("1"), prop_of_max))

        if prop_of_max > 0:
            # Currently long: widen bids (slow to buy more), tighten asks (eager to sell)
            bid_edge = self.baseline_edge_bps * (Decimal("1") + prop_of_max * self.prop_skew_entry)
            ask_edge = self.baseline_edge_bps * (Decimal("1") - prop_of_max * self.prop_skew_exit)
        else:
            # Currently short: tighten bids (eager to buy), widen asks (slow to sell more)
            bid_edge = self.baseline_edge_bps * (Decimal("1") - prop_of_max * self.prop_skew_exit)
            ask_edge = self.baseline_edge_bps * (Decimal("1") + prop_of_max * self.prop_skew_entry)

        return bid_edge, ask_edge

    def _evaluate_existing_order(
        self,
        existing: Optional[ExistingOrder],
        side: OrderSide,
        cancel_threshold: Decimal,
        reference_price: Decimal,
        stop_side: bool,
    ) -> tuple[bool, Optional[str]]:
        """
        Evaluate a single existing order: should it be kept, cancelled, or is it missing?

        Returns:
            (need_new_order, cancel_cloid_or_none)
        """
        if existing is None:
            return True, None  # No order exists, need a new one

        side_label = "bid" if side == OrderSide.BUY else "ask"

        if existing.source == "preregistered":
            logger.debug(f"Quoter {self.baseline_edge_bps}bps: {side_label.capitalize()} pending confirmation, holding")
            return False, None

        if existing.source == "unknown":
            logger.debug(f"Quoter {self.baseline_edge_bps}bps: {side_label.capitalize()} in unknown state, holding")
            return False, None

        # We have a price -- do the edge check
        if existing.price is None:
            return False, None  # Safety: shouldn't happen for on_chain/callback but be safe

        order_edge = self.calculate_order_edge(existing.price, side, reference_price)
        source_tag = f" [{existing.source}]" if existing.source == "callback" else ""

        if stop_side:
            logger.debug(
                f"Quoter {float(self.baseline_edge_bps):.2f}bps: "
                f"Cancelling {side_label} @ {float(existing.price):.6f} "
                f"(position limit exceeded){source_tag}"
            )
            return True, existing.cloid

        if order_edge >= cancel_threshold:
            logger.debug(
                f"Quoter {float(self.baseline_edge_bps):.2f}bps: "
                f"Keeping {side_label} @ {float(existing.price):.6f} "
                f"(edge={float(order_edge):.1f} >= cancel_threshold={float(cancel_threshold):.1f}){source_tag}"
            )
            return False, None

        logger.debug(
            f"Quoter {float(self.baseline_edge_bps):.2f}bps: "
            f"Cancelling {side_label} @ {float(existing.price):.6f} "
            f"(edge={float(order_edge):.1f} < cancel_threshold={float(cancel_threshold):.1f}){source_tag}"
        )
        return True, existing.cloid

    def decide(self, ctx: QuoterContext) -> QuoterDecision:
        cancels = []

        bid_edge, ask_edge = self._get_skewed_edges(ctx)
        maintain = Decimal(str(ctx.prop_maintain))
        bid_cancel_threshold = bid_edge * (Decimal("1") - maintain)
        ask_cancel_threshold = ask_edge * (Decimal("1") - maintain)

        # --- Evaluate existing bid ---
        need_bid, bid_cancel = self._evaluate_existing_order(
            ctx.existing_bid, OrderSide.BUY, bid_cancel_threshold,
            ctx.reference_price, ctx.stop_bids,
        )
        if bid_cancel:
            cancels.append(bid_cancel)

        # --- Evaluate existing ask ---
        need_ask, ask_cancel = self._evaluate_existing_order(
            ctx.existing_ask, OrderSide.SELL, ask_cancel_threshold,
            ctx.reference_price, ctx.stop_asks,
        )
        if ask_cancel:
            cancels.append(ask_cancel)

        # --- Coupling: if one side replaced, force-replace the other ---
        if need_bid and not need_ask:
            if ctx.existing_ask and ctx.existing_ask.source == "preregistered":
                logger.debug(f"Coupling: ask {ctx.existing_ask.cloid} still preregistered, skipping quoter this iteration")
                return QuoterDecision()
            need_ask = True
            if ctx.existing_ask and ctx.existing_ask.cloid not in cancels:
                cancels.append(ctx.existing_ask.cloid)
                logger.debug(f"Coupling: cancelling ask {ctx.existing_ask.cloid} because bid was replaced")
        elif need_ask and not need_bid:
            if ctx.existing_bid and ctx.existing_bid.source == "preregistered":
                logger.debug(f"Coupling: bid {ctx.existing_bid.cloid} still preregistered, skipping quoter this iteration")
                return QuoterDecision()
            need_bid = True
            if ctx.existing_bid and ctx.existing_bid.cloid not in cancels:
                cancels.append(ctx.existing_bid.cloid)
                logger.debug(f"Coupling: cancelling bid {ctx.existing_bid.cloid} because ask was replaced")

        # --- Generate new orders ---
        new_orders = []
        final_need_bid = need_bid and not ctx.stop_bids
        final_need_ask = need_ask and not ctx.stop_asks

        if final_need_bid:
            bid_price = self.price_from_edge(bid_edge, OrderSide.BUY, ctx.reference_price)
            new_orders.append(Order(
                cloid=self.make_cloid("bid"),
                order_type=OrderType.LIMIT,
                side=OrderSide.BUY,
                price=bid_price,
                size=self.quantity,
                post_only=False,
            ))
            logger.debug(
                f"New bid: cloid={new_orders[-1].cloid} price={float(bid_price):.6f} "
                f"size={float(self.quantity)} edge={float(bid_edge):.2f}bps"
            )

        if final_need_ask:
            ask_price = self.price_from_edge(ask_edge, OrderSide.SELL, ctx.reference_price)
            new_orders.append(Order(
                cloid=self.make_cloid("ask"),
                order_type=OrderType.LIMIT,
                side=OrderSide.SELL,
                price=ask_price,
                size=self.quantity,
                post_only=False,
            ))
            logger.debug(
                f"New ask: cloid={new_orders[-1].cloid} price={float(ask_price):.6f} "
                f"size={float(self.quantity)} edge={float(ask_edge):.2f}bps"
            )

        if new_orders:
            logger.debug(
                f"Quoter {self.baseline_edge_bps}bps: Generating {len(new_orders)} new orders "
                f"(bid={'yes' if final_need_bid else 'no'}, ask={'yes' if final_need_ask else 'no'})"
            )

        return QuoterDecision(cancels=cancels, new_orders=new_orders)

    @classmethod
    def from_config(cls, config_section: dict) -> "SkewQuoter":
        """Construct from a [[strategy.quoters]] config dict."""
        return cls(
            baseline_edge_bps=float(config_section["baseline_edge_bps"]),
            quantity=Decimal(str(config_section["quantity"])),
            prop_skew_entry=float(config_section.get("prop_skew_entry", 0.5)),
            prop_skew_exit=float(config_section.get("prop_skew_exit", 0.5)),
        )
