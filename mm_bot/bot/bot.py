import asyncio
import csv
import time
from typing import Set, List, Optional, Dict
from pathlib import Path
from datetime import datetime
from loguru import logger

from mm_bot.kuru_imports import KuruClient, Order, OrderType, OrderStatus, OrderSide
from mm_bot.config.config import BotConfig
from mm_bot.quoter.quoter import Quoter
from mm_bot.position.position_tracker import PositionTracker
from mm_bot.pricing.oracle import OracleService, KuruPriceSource, CoinbasePriceSource
from mm_bot.pnl.tracker import PnlTracker


class OrderInfo:
    """Local representation of an active order for inventory tracking."""
    def __init__(self, cloid: str, side: OrderSide, price: float, size: float, order_id: int):
        self.cloid = cloid
        self.side = side
        self.price = price
        self.size = size  # Remaining size (updated on partial fills)
        self.order_id = order_id


class Bot:
    """
    Market making bot using the kuru-sdk-py SDK.

    Uses event-driven callbacks for order tracking and unified place_orders() API
    for both cancellations and new order placement.
    """

    def __init__(self, connection_config, wallet_config, market_config, bot_config: BotConfig):
        """
        Initialize the bot.

        Args:
            connection_config: ConnectionConfig instance
            wallet_config: WalletConfig instance
            market_config: MarketConfig instance
            bot_config: BotConfig instance
        """
        self.connection_config = connection_config
        self.wallet_config = wallet_config
        self.market_config = market_config
        self.bot_config = bot_config

        # Setup debug log file
        debug_log_dir = Path("tracking")
        debug_log_dir.mkdir(exist_ok=True)
        self.debug_log_path = debug_log_dir / "position_debug.log"

        # Create/clear debug log file
        with open(self.debug_log_path, 'w') as f:
            f.write(f"=== Position Debug Log - Started {datetime.now().isoformat()} ===\n\n")

        logger.warning(f"[DEBUG] Writing debug logs to: {self.debug_log_path}")

        # Client and tracking
        self.client: Optional[KuruClient] = None
        self.active_cloids: Set[str] = set()
        self.cloid_to_order_id: Dict[str, int] = {}  # Track cloid → order_id mapping
        self.order_id_to_cloid: Dict[int, str] = {}  # Track order_id → cloid mapping
        self.order_sizes: Dict[str, float] = {}  # Track cloid → original_size for fill calculation
        self.last_reference_price: Optional[float] = None
        self.last_position_at_update: Optional[float] = None  # Track position at last update
        self.last_reconcile_time: float = 0.0

        # Pre-registration for immediate fills (orders sent but not yet confirmed)
        self.preregistered_orders: Dict[str, tuple[float, float]] = {}  # cloid → (size, timestamp)

        # Active orders tracked from callbacks (for inventory, no API calls needed)
        self.active_orders: Dict[str, OrderInfo] = {}  # cloid → OrderInfo

        # Validation counter for periodic API checks
        self._validation_counter: int = 0

        # Initialize components (position tracker will be initialized in start())
        self.position_tracker: Optional[PositionTracker] = None

        # Oracle service - using Coinbase for MON-USD price
        self.oracle_service = OracleService()
        self.oracle_service.add_price_source("coinbase", CoinbasePriceSource("MON-USD"))
        self.oracle_service.add_price_source("kuru", KuruPriceSource())  # Fallback

        # PnL tracker (will be initialized after position tracker in start())
        self.pnl_tracker: Optional[PnlTracker] = None

        # Quoters will be created after position tracker is initialized
        self.quoters: List[Quoter] = []

        self.shutdown_event = asyncio.Event()

    def _debug_log(self, message: str) -> None:
        """Write to both logger and debug file."""
        logger.warning(message)
        with open(self.debug_log_path, 'a') as f:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            f.write(f"[{timestamp}] {message}\n")

    async def order_callback(self, order: Order) -> None:
        """
        Unified callback to track order lifecycle events AND position updates.

        This combines:
        - Order tracking for active_cloids (for cancellations)
        - Position tracking from fills (for PnL)

        Args:
            order: Order object with updated status
        """
        # CRITICAL: Check if this is our order
        # The WebSocket may deliver callbacks for ALL market activity!
        # We can identify our orders by checking if we've seen this cloid before
        is_our_order = (
            order.cloid in self.active_cloids or           # Currently active
            order.cloid in self.order_sizes or              # We placed it
            order.cloid in self.cloid_to_order_id or        # We tracked it
            # Additional check: our cloids follow specific pattern (e.g., "bid-", "ask-")
            (order.cloid and (order.cloid.startswith('bid-') or order.cloid.startswith('ask-')))
        )

        if not is_our_order:
            # This is someone else's order - ignore it completely
            logger.debug(f"Ignoring order callback for non-bot order: {order.cloid}")
            return

        # Track order placement
        if order.status == OrderStatus.ORDER_PLACED:
            self.active_cloids.add(order.cloid)
            # Store bidirectional mapping
            if order.kuru_order_id is not None:
                self.cloid_to_order_id[order.cloid] = order.kuru_order_id
                self.order_id_to_cloid[order.kuru_order_id] = order.cloid

            # Confirm pre-registration (if order was pre-registered)
            if order.cloid in self.preregistered_orders:
                del self.preregistered_orders[order.cloid]
                self._debug_log(f"[ORDER] PLACED - {order.cloid} confirmed (was pre-registered)")

            # Store initial size for fill calculation (if not already there from pre-reg)
            if order.cloid not in self.order_sizes:
                self.order_sizes[order.cloid] = order.size

            # Add to active orders for inventory tracking (callback-based, no API!)
            self.active_orders[order.cloid] = OrderInfo(
                cloid=order.cloid,
                side=order.side,
                price=order.price,
                size=order.size,
                order_id=order.kuru_order_id
            )

            # DEBUG: Log placement and current tracking state
            self._debug_log(f"[ORDER] PLACED - {order.cloid} with size {order.size}")
            self._debug_log(f"[ORDER] Active orders tracked: {len(self.active_orders)}\n")

            logger.debug(f"✓ Order {order.cloid} placed on orderbook (ID: {order.kuru_order_id}, size: {order.size})")

        # Track cancellations
        elif order.status == OrderStatus.ORDER_CANCELLED:
            self.active_cloids.discard(order.cloid)
            # Clean up mapping
            if order.cloid in self.cloid_to_order_id:
                order_id = self.cloid_to_order_id[order.cloid]
                del self.cloid_to_order_id[order.cloid]
                del self.order_id_to_cloid[order_id]
            # Clean up size tracking
            self.order_sizes.pop(order.cloid, None)
            # Clean up pre-registration (if exists)
            self.preregistered_orders.pop(order.cloid, None)
            # Remove from active orders
            if order.cloid in self.active_orders:
                del self.active_orders[order.cloid]
                self._debug_log(f"[INVENTORY] Removed cancelled order: {order.cloid}")
            logger.debug(f"✗ Order {order.cloid} cancelled")

        # Track fills and update position
        elif order.status == OrderStatus.ORDER_FULLY_FILLED:
            self._debug_log(f"[ORDER] FULLY_FILLED event - cloid: {order.cloid}")
            self._debug_log(f"[ORDER]   side: {order.side.value if order.side else 'None'}")
            self._debug_log(f"[ORDER]   remaining size: {order.size:.2f}")
            self._debug_log(f"[ORDER]   price: {order.price:.6f}")

            # Calculate filled size - check both order_sizes and preregistered_orders
            previous_size = None
            source = None

            if order.cloid in self.order_sizes:
                # Normal path: ORDER_PLACED fired before fill
                previous_size = self.order_sizes[order.cloid]
                source = "order_sizes"
                del self.order_sizes[order.cloid]
            elif order.cloid in self.preregistered_orders:
                # Immediate fill path: ORDER_PLACED never fired
                previous_size = self.preregistered_orders[order.cloid][0]
                source = "preregistered"
                del self.preregistered_orders[order.cloid]

            if previous_size is not None:
                filled_size = previous_size - order.size  # order.size should be 0 for fully filled

                self._debug_log(f"[ORDER]   previous size: {previous_size:.2f} (from {source})")
                self._debug_log(f"[ORDER]   FILLED SIZE: {filled_size:.2f}")

                # Update position tracker with actual filled amount
                self.position_tracker.update_position(
                    side=order.side,
                    filled_size=filled_size,
                    price=order.price
                )

                # SUCCESS
                self._debug_log(f"[ORDER] Position tracker updated successfully\n")

            else:
                # FAILURE - not in either dict
                self._debug_log(f"[ORDER] ⚠️ SKIPPED - Order not in order_sizes or preregistered!")
                self._debug_log(f"[ORDER] Position NOT updated for {order.cloid}\n")
                logger.error(
                    f"⚠️ Fill received for unknown order: {order.cloid} "
                    f"(side: {order.side.value if order.side else 'N/A'}, "
                    f"size: {order.size}) - POSITION NOT UPDATED!"
                )

            self.active_cloids.discard(order.cloid)
            # Clean up mapping
            if order.cloid in self.cloid_to_order_id:
                order_id = self.cloid_to_order_id[order.cloid]
                del self.cloid_to_order_id[order.cloid]
                del self.order_id_to_cloid[order_id]

            # Remove from active orders (no longer on book)
            if order.cloid in self.active_orders:
                del self.active_orders[order.cloid]
                self._debug_log(f"[INVENTORY] Removed filled order: {order.cloid}")

            logger.success(
                f"✓ Order {order.cloid} filled! "
                f"Side: {order.side.value if order.side else 'N/A'}, "
                f"Price: {order.price}"
            )

        elif order.status == OrderStatus.ORDER_PARTIALLY_FILLED:
            self._debug_log(f"[ORDER] PARTIALLY_FILLED event - cloid: {order.cloid}")
            self._debug_log(f"[ORDER]   side: {order.side.value if order.side else 'None'}")
            self._debug_log(f"[ORDER]   remaining size: {order.size:.2f}")
            self._debug_log(f"[ORDER]   price: {order.price:.6f}")

            # Calculate filled size - check both order_sizes and preregistered_orders
            previous_size = None
            source = None

            if order.cloid in self.order_sizes:
                previous_size = self.order_sizes[order.cloid]
                source = "order_sizes"
            elif order.cloid in self.preregistered_orders:
                previous_size = self.preregistered_orders[order.cloid][0]
                source = "preregistered"
                # Move to order_sizes since it's confirmed now
                self.order_sizes[order.cloid] = order.size
                del self.preregistered_orders[order.cloid]

            if previous_size is not None:
                filled_size = previous_size - order.size

                self._debug_log(f"[ORDER]   previous size: {previous_size:.2f} (from {source})")
                self._debug_log(f"[ORDER]   FILLED SIZE: {filled_size:.2f}")

                # Update position tracker with actual filled amount
                self.position_tracker.update_position(
                    side=order.side,
                    filled_size=filled_size,
                    price=order.price
                )

                # Update stored size for next partial fill
                self.order_sizes[order.cloid] = order.size

                # Update size in active_orders (still on book, but with less remaining)
                if order.cloid in self.active_orders:
                    self.active_orders[order.cloid].size = order.size
                    self._debug_log(f"[INVENTORY] Updated order size: {order.cloid}, remaining={order.size}")

                # SUCCESS
                self._debug_log(f"[ORDER] Position tracker updated successfully\n")

            else:
                # FAILURE
                self._debug_log(f"[ORDER] ⚠️ SKIPPED - Order not in order_sizes or preregistered!")
                self._debug_log(f"[ORDER] Position NOT updated for {order.cloid}\n")
                logger.error(
                    f"⚠️ Partial fill received for unknown order: {order.cloid} "
                    f"(side: {order.side.value if order.side else 'N/A'}, "
                    f"remaining: {order.size}) - POSITION NOT UPDATED!"
                )

            # Keep in active_cloids since it's still on the book
            logger.info(f"⚡ Order {order.cloid} partially filled")

    async def start(self) -> None:
        """
        Start the bot: create client, setup callbacks, and run main loop.
        """
        # Create client using async factory
        logger.info("Creating KuruClient...")
        self.client = await KuruClient.create(
            market_config=self.market_config,
            connection_config=self.connection_config,
            wallet_config=self.wallet_config,
        )

        # Set unified callback (handles both order tracking AND position updates)
        self.client.set_order_callback(self.order_callback)

        # Start client
        logger.info("Starting client...")
        logger.debug(f"Client type: {type(self.client)}")
        logger.debug(f"Client module: {type(self.client).__module__}")
        await self.client.start()
        logger.success(f"Connected to market: {self.market_config.market_symbol}")

        # ONE-TIME cleanup: Cancel any leftover orders from previous runs
        await self._cancel_all_existing_orders()

        # Initialize position tracker with starting position from margin balances
        await self._initialize_position_tracker()

        # Initialize PnL tracker
        self.pnl_tracker = PnlTracker(
            position_tracker=self.position_tracker,
            oracle_service=self.oracle_service,
            market_id=self.market_config.market_address,
            source_name="coinbase"
        )

        # Create quoters with calculated quantity
        self._initialize_quoters()

        # Run main loop
        await self.run_main_loop()

    async def _initialize_position_tracker(self) -> None:
        """
        Initialize position tracker with starting position.
        Uses override if provided, otherwise defaults to 0 (neutral strategy).
        Position tracking represents net buys/sells, not total holdings.
        """
        try:
            if self.bot_config.override_start_position is not None:
                start_position = self.bot_config.override_start_position
                self._debug_log(f"[INIT] Using OVERRIDE start position: {start_position:.6f}")
                logger.info(f"Using override starting position: {start_position:.6f}")
            else:
                # Default to 0 for neutral market-making strategy
                start_position = 0.0
                self._debug_log(f"[INIT] Defaulting to start position: 0.0 (neutral strategy)")
                logger.info("Starting position set to 0 (neutral strategy - tracks net buys/sells)")

            # Initialize position tracker
            self.position_tracker = PositionTracker(start_position=start_position)
            self._debug_log(f"[INIT] ✓ Position tracker initialized")
            self._debug_log(f"[INIT]   start_position: {self.position_tracker.get_start_position():.6f}")
            self._debug_log(f"[INIT]   current_position: {self.position_tracker.get_current_position():.6f}\n")
            logger.success(f"✓ Position tracker initialized with start_position={start_position:.6f}")

        except Exception as e:
            logger.error(f"Failed to initialize position tracker: {e}")
            logger.warning("Falling back to start_position=0.0")
            self.position_tracker = PositionTracker(start_position=0.0)

    def _initialize_quoters(self) -> None:
        """
        Initialize quoters with calculated quantity based on config.
        Uses quantity_bps_per_level if set, otherwise uses fixed quantity.
        """
        for baseline_edge_bps in self.bot_config.quoters_bps:
            # Calculate quantity
            if self.bot_config.quantity_bps_per_level is not None:
                # Use BPS of max position
                quantity = (self.bot_config.max_position * self.bot_config.quantity_bps_per_level) / 10000
                logger.info(
                    f"Quoter {baseline_edge_bps}bps: Using quantity_bps={self.bot_config.quantity_bps_per_level} "
                    f"→ quantity={quantity:.2f}"
                )
            else:
                # Use fixed quantity
                quantity = self.bot_config.quantity
                logger.info(f"Quoter {baseline_edge_bps}bps: Using fixed quantity={quantity}")

            quoter = Quoter(
                oracle_service=self.oracle_service,
                position_tracker=self.position_tracker,
                source_name="coinbase",
                market_id=self.market_config.market_address,
                strategy_type=self.bot_config.strategy_type,
                baseline_edge_bps=baseline_edge_bps,
                max_position=self.bot_config.max_position,
                prop_skew_entry=self.bot_config.prop_skew_entry,
                prop_skew_exit=self.bot_config.prop_skew_exit,
                quantity=quantity,
                market_config=self.market_config
            )
            self.quoters.append(quoter)

        logger.success(f"✓ Initialized {len(self.quoters)} quoters")

    def get_locked_inventory(self) -> tuple[float, float]:
        """
        Calculate locked inventory from callback-tracked orders.
        No API call needed!

        Returns:
            (locked_base, locked_quote)
        """
        locked_base = 0.0
        locked_quote = 0.0

        for order_info in self.active_orders.values():
            if order_info.side == OrderSide.BUY:
                # Buy orders lock quote
                locked_quote += order_info.size * order_info.price
            else:
                # Sell orders lock base
                locked_base += order_info.size

        return locked_base, locked_quote

    async def _reconcile_position(self, block_number: Optional[int] = None) -> None:
        """
        Take snapshot of margin balances and active orders at specific block.
        Calculate free vs locked inventory. Write to CSV for tracking.

        Args:
            block_number: Block number to reconcile at (None = latest)
        """
        try:
            # Get current block if not specified
            if block_number is None:
                block_number = await self.client.user.w3.eth.block_number

            # Get margin balances
            base_wei, quote_wei = await self.client.user.get_margin_balances()

            base_balance = base_wei / (10 ** self.market_config.base_token_decimals)
            quote_balance = quote_wei / (10 ** self.market_config.quote_token_decimals)

            self._debug_log(f"[RECONCILE] ===== RECONCILIATION @ block {block_number} =====")
            self._debug_log(f"[RECONCILE] Margin balances - base: {base_balance:.6f}, quote: {quote_balance:.6f}")

            # Calculate locked inventory from callback-tracked orders (NO API CALL!)
            locked_base, locked_quote = self.get_locked_inventory()

            self._debug_log(f"[RECONCILE] Tracked active orders: {len(self.active_orders)}")
            for i, order_info in enumerate(self.active_orders.values()):
                side_str = "BUY" if order_info.side == OrderSide.BUY else "SELL"
                self._debug_log(f"[RECONCILE] Order {i}: {side_str} {order_info.size:.2f} @ {order_info.price:.6f}")

            self._debug_log(f"[RECONCILE] Locked (from callbacks) - base: {locked_base:.6f}, quote: {locked_quote:.6f}")

            # Margin balance IS the free balance (get_margin_balances returns only free tokens)
            free_base = base_balance
            free_quote = quote_balance

            # Calculate total owned (free + locked)
            total_base = base_balance + locked_base
            total_quote = quote_balance + locked_quote

            self._debug_log(f"[RECONCILE] Free - base: {free_base:.6f}, quote: {free_quote:.6f}")
            self._debug_log(f"[RECONCILE] Total owned - base: {total_base:.6f}, quote: {total_quote:.6f}")

            # Calculate tracked position
            current_pos = self.position_tracker.get_current_position()
            start_pos = self.position_tracker.get_start_position()
            tracked_position = current_pos + start_pos

            self._debug_log(f"[RECONCILE] Position tracker:")
            self._debug_log(f"[RECONCILE]   start_position: {start_pos:.6f}")
            self._debug_log(f"[RECONCILE]   current_position: {current_pos:.6f}")
            self._debug_log(f"[RECONCILE]   total tracked: {tracked_position:.6f}")

            # Drift detection - compare total owned vs tracked
            drift = total_base - tracked_position

            # Track drift over time and alert on changes
            previous_drift = getattr(self, '_last_reconcile_drift', None)
            drift_delta = 0.0

            if previous_drift is not None:
                drift_delta = drift - previous_drift

                # Alert if drift changes by >10 tokens (indicates missing fills or external transactions)
                if abs(drift_delta) > 10.0:
                    self._debug_log(f"[RECONCILE] ⚠️ DRIFT CHANGE DETECTED: {drift_delta:+.2f} tokens")
                    self._debug_log(f"[RECONCILE]   Previous drift: {previous_drift:.6f}")
                    self._debug_log(f"[RECONCILE]   Current drift: {drift:.6f}")
                    self._debug_log(f"[RECONCILE]   total_base: {total_base:.6f}, tracked_position: {tracked_position:.6f}")
                    logger.warning(
                        f"⚠️ Drift changed by {drift_delta:+.2f} tokens - "
                        f"likely missing fill events or external transactions"
                    )
                else:
                    # Tracking is working correctly
                    self._debug_log(f"[RECONCILE] ✓ Drift stable: {drift_delta:+.2f} tokens (tracking OK)")
                    self._debug_log(f"[RECONCILE]   total_base: {total_base:.6f}, tracked_position: {tracked_position:.6f}, drift: {drift:.6f}")
            else:
                # First reconciliation - establish baseline
                self._debug_log(f"[RECONCILE] Drift baseline established: {drift:.6f}")
                self._debug_log(f"[RECONCILE]   total_base: {total_base:.6f}, tracked_position: {tracked_position:.6f}")

            self._last_reconcile_drift = drift

            # For reference: free base comparison (can be negative if insufficient for new orders)
            drift_vs_free = abs(free_base - tracked_position)
            self._debug_log(f"[RECONCILE] Free base comparison (for reference):")
            self._debug_log(f"[RECONCILE]   free_base: {free_base:.6f}")
            self._debug_log(f"[RECONCILE]   Diff vs free: {drift_vs_free:.6f}")

            self._debug_log(f"[RECONCILE] ===== END RECONCILIATION =====\n")

            # Write to CSV
            tracking_dir = Path("tracking")
            tracking_dir.mkdir(exist_ok=True)
            csv_path = tracking_dir / "position_reconciliation.csv"

            # Check if file exists to write header
            write_header = not csv_path.exists()

            with open(csv_path, 'a', newline='') as f:
                writer = csv.writer(f)

                if write_header:
                    writer.writerow([
                        'timestamp', 'block_number',
                        'margin_base', 'locked_base', 'free_base', 'total_base_owned',
                        'margin_quote', 'locked_quote', 'free_quote', 'total_quote_owned',
                        'tracked_position', 'drift',
                        'num_active_orders', 'current_price'
                    ])

                current_price = self.oracle_service.get_price(
                    self.market_config.market_address, "coinbase"
                )

                writer.writerow([
                    datetime.now().isoformat(),
                    block_number,
                    f"{base_balance:.6f}",
                    f"{locked_base:.6f}",
                    f"{free_base:.6f}",
                    f"{total_base:.6f}",
                    f"{quote_balance:.6f}",
                    f"{locked_quote:.6f}",
                    f"{free_quote:.6f}",
                    f"{total_quote:.6f}",
                    f"{tracked_position:.6f}",
                    f"{drift:.6f}",
                    len(self.active_orders),
                    f"{current_price:.8f}" if current_price else "None"
                ])

            # Periodic API validation (every 10th reconciliation)
            await self._validate_against_api()

            # Show clean summary with drift delta
            drift_status = f"Δ{drift_delta:+.2f}" if previous_drift is not None else "baseline"
            logger.info(
                f"📊 Reconciliation @ block {block_number}: "
                f"Position {tracked_position:+.2f}, "
                f"Inventory {total_base:.2f} (free: {free_base:.2f}, locked: {locked_base:.2f}), "
                f"Drift {drift_status}"
            )

        except Exception as e:
            logger.error(f"Failed to reconcile position: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _validate_against_api(self) -> None:
        """
        Periodically validate our callback-tracked state against API.
        Call this every ~10 reconciliations, not every time.
        """
        try:
            self._validation_counter += 1

            # Only validate every 10th reconciliation
            if self._validation_counter % 10 != 0:
                return

            self._debug_log(f"[VALIDATE] ===== API VALIDATION =====")

            # Fetch actual active orders from API
            api_active_orders = self.client.user.get_active_orders()

            # Compare counts
            tracked_count = len(self.active_orders)
            api_count = len(api_active_orders)

            if tracked_count != api_count:
                logger.warning(
                    f"⚠️ Order count mismatch: Tracked={tracked_count}, API={api_count}"
                )
                self._debug_log(f"[VALIDATE] Count mismatch: tracked={tracked_count}, api={api_count}")

            # Compare order IDs (use 'orderid' lowercase to match API response)
            api_order_ids = {int(order.get('orderid')) for order in api_active_orders if order.get('orderid') is not None}
            tracked_order_ids = {info.order_id for info in self.active_orders.values()}

            missing_in_tracked = api_order_ids - tracked_order_ids
            missing_in_api = tracked_order_ids - api_order_ids

            if missing_in_tracked:
                logger.warning(f"⚠️ Orders on chain but not tracked: {missing_in_tracked}")
                self._debug_log(f"[VALIDATE] Missing in tracked: {missing_in_tracked}")

            if missing_in_api:
                logger.warning(f"⚠️ Orders tracked but not on chain: {missing_in_api}")
                self._debug_log(f"[VALIDATE] Missing in API (phantom orders): {missing_in_api}")
                # Clean up phantom orders
                for cloid, info in list(self.active_orders.items()):
                    if info.order_id in missing_in_api:
                        logger.warning(f"Cleaning up phantom order: {cloid}")
                        del self.active_orders[cloid]
                        self._debug_log(f"[VALIDATE] Cleaned up phantom: {cloid}")

            if tracked_count == api_count and not missing_in_tracked and not missing_in_api:
                self._debug_log(f"[VALIDATE] ✓ All orders match!")

            self._debug_log(f"[VALIDATE] ===== END VALIDATION =====")

        except Exception as e:
            logger.error(f"Failed to validate against API: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _cleanup_stale_preregistrations(self) -> None:
        """
        Clean up pre-registered orders that never received confirmation.
        Call this periodically (e.g., every 30 seconds).
        """
        try:
            current_time = time.time()
            stale_timeout = 5  # seconds (short timeout since transactions are fast)

            stale_cloids = []
            for cloid, (size, timestamp) in self.preregistered_orders.items():
                age = current_time - timestamp
                if age > stale_timeout:
                    stale_cloids.append(cloid)

            for cloid in stale_cloids:
                # Remove from order_sizes (order never confirmed)
                if cloid in self.order_sizes:
                    del self.order_sizes[cloid]
                del self.preregistered_orders[cloid]
                logger.warning(
                    f"⚠️ Cleaned up stale pre-registration for {cloid} "
                    f"(no confirmation after {stale_timeout}s)"
                )
                self._debug_log(f"[CLEANUP] Removed stale pre-registration: {cloid}")

        except Exception as e:
            logger.error(f"Failed to cleanup stale pre-registrations: {e}")

    async def _cancel_all_existing_orders(self) -> None:
        """
        Cancel all existing orders from previous runs using API.
        This is a one-time cleanup at startup to ensure clean slate.
        """
        try:
            active_orders = self.client.user.get_active_orders()
            if active_orders:
                logger.info(f"Found {len(active_orders)} existing orders from previous run, cancelling...")
                cancel_tx = await self.client.cancel_all_active_orders_for_market()
                logger.success(f"✓ Cancelled {len(active_orders)} existing orders (TX: {cancel_tx})")
            else:
                logger.info("No existing orders to cancel")
        except Exception as e:
            logger.error(f"Failed to cancel existing orders: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def run_main_loop(self) -> None:
        """
        Main bot loop: generate quotes, check thresholds, and place orders.
        """
        logger.info("🚀 Starting market making loop...\n")
        iteration = 0
        self.last_reconcile_time = time.time()
        self.last_cleanup_time = time.time()

        while not self.shutdown_event.is_set():
            try:
                iteration += 1

                # Periodic reconciliation
                if self.bot_config.reconcile_interval > 0:
                    if time.time() - self.last_reconcile_time >= self.bot_config.reconcile_interval:
                        await self._reconcile_position()
                        self.last_reconcile_time = time.time()

                # Periodic cleanup of stale pre-registrations (every 5 seconds)
                cleanup_interval = 5.0
                if time.time() - self.last_cleanup_time >= cleanup_interval:
                    await self._cleanup_stale_preregistrations()
                    self.last_cleanup_time = time.time()

                # Get reference price from Coinbase
                reference_price = self.oracle_service.get_price(
                    self.market_config.market_address,
                    "coinbase"
                )

                if reference_price is None:
                    logger.warning("Could not fetch reference price, skipping iteration")
                    await asyncio.sleep(1.0)
                    continue

                # Check price threshold
                should_update = self._should_update_orders(reference_price)
                logger.info(f"Iteration {iteration}: Price=${reference_price:.5f}, should_update={should_update}")

                if should_update:
                    # Build order list: cancels + new orders
                    all_orders = []

                    # Get current on-chain active orders to validate before cancelling
                    on_chain_orders = self.client.user.get_active_orders()

                    # Build set of on-chain order IDs
                    on_chain_order_ids = {int(order.get('orderid')) for order in on_chain_orders if order.get('orderid') is not None}

                    # Filter tracked CLOIDs: keep only those whose order_id still exists on-chain
                    tracked_cloids = list(self.active_cloids)
                    valid_cloids = []
                    for cloid in tracked_cloids:
                        order_id = self.cloid_to_order_id.get(cloid)
                        if order_id is not None and order_id in on_chain_order_ids:
                            valid_cloids.append(cloid)

                    filtered_count = len(tracked_cloids) - len(valid_cloids)

                    if filtered_count > 0:
                        logger.debug(f"Filtered out {filtered_count} orders that no longer exist on-chain")

                    # Cancel only orders that still exist on-chain
                    for cloid in valid_cloids:
                        all_orders.append(Order(cloid=cloid, order_type=OrderType.CANCEL))

                    num_cancels = len(valid_cloids)

                    # Get new orders from all quoters
                    new_orders = []
                    for quoter in self.quoters:
                        quoter_orders = quoter.get_orders()
                        logger.debug(f"Quoter {quoter.baseline_edge_bps}bps generated {len(quoter_orders)} orders")
                        new_orders.extend(quoter_orders)

                    # Validate balance before placing orders
                    # Get current margin balances
                    base_wei, quote_wei = await self.client.user.get_margin_balances()
                    base_balance = base_wei / (10 ** self.market_config.base_token_decimals)
                    quote_balance = quote_wei / (10 ** self.market_config.quote_token_decimals)

                    # Calculate locked balances from on-chain orders
                    locked_base = 0.0
                    locked_quote = 0.0
                    for order in on_chain_orders:
                        is_buy = order.get("isbuy", False)
                        size = float(order.get("size", 0)) / self.market_config.size_precision
                        price = float(order.get("price", 0)) / self.market_config.price_precision

                        if is_buy:
                            locked_quote += size * price
                        else:
                            locked_base += size

                    # Calculate free balances (excluding orders we're about to cancel)
                    free_base = base_balance - locked_base
                    free_quote = quote_balance - locked_quote

                    # Add back the balances from orders we're cancelling
                    for cloid in valid_cloids:
                        order_id = self.cloid_to_order_id.get(cloid)
                        if order_id is None:
                            continue

                        for order in on_chain_orders:
                            if int(order.get('orderid', 0)) == order_id:
                                is_buy = order.get("isbuy", False)
                                size = float(order.get("size", 0)) / self.market_config.size_precision
                                price = float(order.get("price", 0)) / self.market_config.price_precision

                                if is_buy:
                                    free_quote += size * price
                                else:
                                    free_base += size
                                break

                    # Calculate required balances for new orders
                    required_base = 0.0
                    required_quote = 0.0
                    buy_orders = []
                    sell_orders = []

                    for order in new_orders:
                        if order.side == OrderSide.BUY:
                            buy_orders.append(order)
                            required_quote += order.size * order.price
                        elif order.side == OrderSide.SELL:
                            sell_orders.append(order)
                            required_base += order.size

                    # Filter orders based on available balance
                    filtered_orders = []
                    skipped_buys = 0
                    skipped_sells = 0

                    # Add buy orders only if we have enough quote balance
                    if required_quote <= free_quote:
                        filtered_orders.extend(buy_orders)
                    else:
                        skipped_buys = len(buy_orders)
                        logger.warning(f"⚠️  Insufficient quote balance for buy orders. Need {required_quote:.2f}, have {free_quote:.2f}. Skipping {skipped_buys} buy orders.")

                    # Add sell orders only if we have enough base balance
                    if required_base <= free_base:
                        filtered_orders.extend(sell_orders)
                    else:
                        skipped_sells = len(sell_orders)
                        logger.warning(f"⚠️  Insufficient base balance for sell orders. Need {required_base:.2f}, have {free_base:.2f}. Skipping {skipped_sells} sell orders.")

                    # Add filtered orders to transaction
                    all_orders.extend(filtered_orders)

                    logger.info(f"Balance check: Free base={free_base:.2f}, Free quote={free_quote:.2f}")
                    logger.info(f"Total orders to place: {len(all_orders)} (cancels: {num_cancels}, new: {len(filtered_orders)}, skipped: {skipped_buys + skipped_sells})")

                    # Debug: log order details
                    for i, order in enumerate(all_orders):
                        logger.debug(f"Order {i}: {order}")

                    if all_orders:
                        # Debug: Check orders before sending
                        logger.info(f"Order details before sending:")
                        for order in all_orders:
                            logger.info(f"  {order.cloid}: side={order.side}, type={order.order_type}, price={order.price}, size={order.size}")

                        # PRE-REGISTER new orders BEFORE sending (handles immediate fills)
                        for order in all_orders:
                            if order.order_type == OrderType.LIMIT and order.size is not None:
                                # Skip cancels, only pre-register new limit orders
                                self.preregistered_orders[order.cloid] = (order.size, time.time())
                                self.order_sizes[order.cloid] = order.size
                                self._debug_log(f"[PRESEND] Pre-registered {order.cloid} with size {order.size}")

                        # Single transaction for cancel + place
                        txhash = await self.client.place_orders(all_orders, post_only=False)
                        logger.info(f"Transaction hash: {txhash}")

                        # Update last reference price and position
                        self.last_reference_price = reference_price
                        self.last_position_at_update = self.position_tracker.get_current_position()

                        # Print PnL
                        self.pnl_tracker.print_pnl()

                # Sleep 1 second
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    pass

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(1.0)

    def _should_update_orders(self, current_price: float) -> bool:
        """
        Check if orders should be updated based on price or position threshold.

        Args:
            current_price: Current reference price

        Returns:
            True if orders should be updated
        """
        # First iteration - always update
        if self.last_reference_price is None:
            return True

        # Check price change
        price_change_bps = abs(
            (current_price - self.last_reference_price) / self.last_reference_price * 10000
        )

        if price_change_bps >= self.bot_config.price_update_threshold_bps:
            logger.debug(f"Update triggered by price change: {price_change_bps:.2f} bps")
            return True

        # Check position change (as BPS of max_position)
        if self.last_position_at_update is not None:
            current_position = self.position_tracker.get_current_position()
            position_change = abs(current_position - self.last_position_at_update)
            position_change_bps = (position_change / self.bot_config.max_position) * 10000

            if position_change_bps >= self.bot_config.position_update_threshold_bps:
                logger.debug(
                    f"Update triggered by position change: {position_change:.2f} tokens "
                    f"({position_change_bps:.2f} bps of max_position)"
                )
                return True

        return False

    async def stop(self) -> None:
        """
        Stop the bot: cancel all active orders and stop the client.
        """
        logger.info("\n🛑 Stopping bot...")

        # Cancel all active orders using cancel_all (more reliable than individual cancels)
        try:
            # Check if there are any orders to cancel
            active_orders = self.client.user.get_active_orders()
            if active_orders:
                logger.info(f"Cancelling {len(active_orders)} active orders...")
                cancel_tx = await self.client.cancel_all_active_orders_for_market()
                logger.success(f"✓ Sent cancel transaction: {cancel_tx}")

                # Wait a moment for cancel transaction to confirm
                logger.info("Waiting for cancellation to confirm...")
                await asyncio.sleep(3)

                # Verify cancellation
                remaining = self.client.user.get_active_orders()
                if remaining:
                    logger.warning(f"⚠️ {len(remaining)} orders still active after cancellation")
                else:
                    logger.success("✓ All orders cancelled successfully")
            else:
                logger.info("No active orders to cancel")
        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")
            import traceback
            logger.error(traceback.format_exc())

        # Stop client
        if self.client:
            await self.client.stop()
            logger.success("✓ Client stopped")
