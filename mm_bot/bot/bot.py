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
        self.last_reconcile_time: float = 0.0

        # Pre-registration for immediate fills (orders sent but not yet confirmed)
        self.preregistered_orders: Dict[str, tuple[float, float]] = {}  # cloid → (size, timestamp)

        # Active orders tracked from callbacks (for inventory, no API calls needed)
        self.active_orders: Dict[str, OrderInfo] = {}  # cloid → OrderInfo

        # Orphaned order tracking (orders on chain but no callback received)
        self.orphaned_order_timestamps: Dict[int, float] = {}  # order_id → first_seen_timestamp

        # Validation counter for periodic API checks
        self._validation_counter: int = 0

        # Initialize components (position tracker will be initialized in start())
        self.position_tracker: Optional[PositionTracker] = None

        # Oracle service - price source configured via ORACLE env var
        self.oracle_service = OracleService()
        self.oracle_source = bot_config.oracle_source  # "kuru" or "coinbase"

        # Set up the configured price source
        if self.oracle_source == "kuru":
            # Kuru WebSocket price source (will be started in start() method)
            self.kuru_price_source = KuruPriceSource()
            self.oracle_service.add_price_source("kuru", self.kuru_price_source)
        else:
            # Coinbase API price source (default)
            self.coinbase_price_source = CoinbasePriceSource(symbol="MON-USD")
            self.oracle_service.add_price_source("coinbase", self.coinbase_price_source)
            self.kuru_price_source = None  # Not used

        # PnL tracker (will be initialized after position tracker in start())
        self.pnl_tracker: Optional[PnlTracker] = None

        # Quoters will be created after position tracker is initialized
        self.quoters: List[Quoter] = []

        self.shutdown_event = asyncio.Event()

    def _debug_log(self, message: str) -> None:
        """Write to both logger and debug file."""
        logger.debug(message)
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

            # Clear orphaned tracking if this order was previously detected as orphaned
            if order.kuru_order_id in self.orphaned_order_timestamps:
                self._debug_log(f"[ORDER] Order {order.kuru_order_id} callback arrived (was orphaned), clearing tracking")
                del self.orphaned_order_timestamps[order.kuru_order_id]

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

        # Start price feed based on configured oracle
        if self.oracle_source == "kuru":
            logger.info("Connecting to Kuru orderbook WebSocket...")
            self.kuru_price_source.start(self.market_config.market_address)
        else:
            logger.info(f"Using Coinbase API as oracle (symbol: MON-USD)")

        # ONE-TIME cleanup: Cancel any leftover orders from previous runs
        await self._cancel_all_existing_orders()

        # Initialize position tracker with starting position from margin balances
        await self._initialize_position_tracker()

        # Initialize PnL tracker
        self.pnl_tracker = PnlTracker(
            position_tracker=self.position_tracker,
            oracle_service=self.oracle_service,
            market_id=self.market_config.market_address,
            source_name="kuru"
        )

        # Create quoters with calculated quantity
        self._initialize_quoters()

        # Run main loop
        await self.run_main_loop()

    async def _initialize_position_tracker(self) -> None:
        """
        Initialize position tracker with starting position.
        Priority: 1) Saved state, 2) Config override, 3) Default to 0
        Position tracking represents net buys/sells, not total holdings.
        """
        try:
            # Try to load saved state first
            from pathlib import Path
            state_file = Path("tracking") / "position_state.json"
            saved_state = PositionTracker.load_state(state_file)

            if self.bot_config.override_start_position is not None:
                # Config override takes precedence
                start_position = self.bot_config.override_start_position
                current_position = 0.0
                quote_position = 0.0
                self._debug_log(f"[INIT] Using CONFIG OVERRIDE start position: {start_position:.6f}")
                logger.info(f"Using override starting position: {start_position:.6f} (ignoring saved state)")
            elif saved_state:
                # Restore from saved state
                start_position = saved_state.get('start_position', 0.0)
                current_position = saved_state.get('current_position', 0.0)
                quote_position = saved_state.get('quote_position', 0.0)
                total_position = saved_state.get('total_position', start_position + current_position)
                self._debug_log(f"[INIT] Restoring from saved state:")
                self._debug_log(f"[INIT]   start_position: {start_position:.6f}")
                self._debug_log(f"[INIT]   current_position: {current_position:.6f}")
                self._debug_log(f"[INIT]   total_position: {total_position:.6f}")
                logger.info(f"Restored position from saved state: {total_position:.2f} "
                           f"(last updated: {saved_state.get('last_updated', 'unknown')})")
            else:
                # Default to 0 for neutral market-making strategy
                start_position = 0.0
                current_position = 0.0
                quote_position = 0.0
                self._debug_log(f"[INIT] No saved state - defaulting to start position: 0.0 (neutral strategy)")
                logger.info("Starting position set to 0 (neutral strategy - tracks net buys/sells)")

            # Initialize position tracker
            self.position_tracker = PositionTracker(start_position=start_position)

            # Restore state if loaded
            if saved_state and self.bot_config.override_start_position is None:
                self.position_tracker.current_position = current_position
                self.position_tracker.quote_position = quote_position

            self._debug_log(f"[INIT] ✓ Position tracker initialized")
            self._debug_log(f"[INIT]   start_position: {self.position_tracker.get_start_position():.6f}")
            self._debug_log(f"[INIT]   current_position: {self.position_tracker.get_current_position():.6f}")
            self._debug_log(f"[INIT]   total_position: {self.position_tracker.get_current_position() + self.position_tracker.get_start_position():.6f}\n")
            logger.success(f"✓ Position tracker initialized: total={self.position_tracker.get_current_position() + self.position_tracker.get_start_position():.2f}")

        except Exception as e:
            logger.error(f"Failed to initialize position tracker: {e}")
            import traceback
            logger.error(traceback.format_exc())
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
                source_name="kuru",
                market_id=self.market_config.market_address,
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
                    self.market_config.market_address, self.oracle_source
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

        Quick check (every reconciliation): Detect orphaned orders
        Full check (every 10th): Compare all order IDs and clean up phantoms
        """
        try:
            self._validation_counter += 1

            # Fetch actual active orders from API
            api_active_orders = self.client.user.get_active_orders()

            # QUICK CHECK (every time): Detect orphaned orders
            # This is critical for detecting lost callbacks quickly
            api_order_ids = {int(order.get('orderid')) for order in api_active_orders if order.get('orderid') is not None}
            tracked_order_ids = {info.order_id for info in self.active_orders.values()}
            missing_in_tracked = api_order_ids - tracked_order_ids

            current_time = time.time()
            orphan_timeout = 3.0  # seconds - grace period for late callbacks

            if missing_in_tracked:
                # Track when we first saw these orphaned orders
                for order_id in missing_in_tracked:
                    if order_id not in self.orphaned_order_timestamps:
                        # First time seeing this orphaned order
                        self.orphaned_order_timestamps[order_id] = current_time
                        logger.warning(
                            f"⚠️ Orphaned order detected: {order_id} on chain but no callback yet. "
                            f"Waiting {orphan_timeout}s for late callback..."
                        )
                        self._debug_log(f"[VALIDATE] New orphaned order: {order_id}, starting grace period")
                    else:
                        # Already tracking this orphan
                        time_orphaned = current_time - self.orphaned_order_timestamps[order_id]
                        self._debug_log(
                            f"[VALIDATE] Still orphaned: {order_id}, age={time_orphaned:.1f}s "
                            f"(timeout at {orphan_timeout}s)"
                        )

                # Check if any orphaned orders have exceeded grace period
                old_orphans = []
                for order_id in missing_in_tracked:
                    time_orphaned = current_time - self.orphaned_order_timestamps.get(order_id, current_time)
                    if time_orphaned > orphan_timeout:
                        old_orphans.append(order_id)
                        self._debug_log(f"[VALIDATE] Orphan {order_id} exceeded timeout: {time_orphaned:.1f}s > {orphan_timeout}s")

                self._debug_log(
                    f"[VALIDATE] Orphan summary: {len(missing_in_tracked)} total orphans, "
                    f"{len(old_orphans)} exceeded timeout, "
                    f"{len(missing_in_tracked) - len(old_orphans)} still in grace period"
                )

                if old_orphans:
                    # Orphaned orders exceeded grace period - callbacks were lost
                    logger.error(
                        f"🚨 ORPHANED ORDERS TIMEOUT: {len(old_orphans)} orders on chain for >{orphan_timeout}s with no callbacks. "
                        f"Order IDs: {old_orphans}. Callbacks lost. Cancelling all and resetting state..."
                    )
                    self._debug_log(f"[VALIDATE] Orphaned orders timed out: {old_orphans} - triggering full reset")

                    # Cancel all active orders
                    await self._cancel_all_existing_orders()

                    # Clear all tracking state
                    self.active_orders.clear()
                    self.order_sizes.clear()
                    self.preregistered_orders.clear()
                    self.active_cloids.clear()
                    self.cloid_to_order_id.clear()
                    self.order_id_to_cloid.clear()
                    self.orphaned_order_timestamps.clear()

                    logger.info("✓ State reset complete. Will resume quoting on next iteration.")
                    self._debug_log(f"[VALIDATE] State cleared, ready for fresh start")
                    return
            else:
                # No orphaned orders - clear tracking
                if self.orphaned_order_timestamps:
                    self._debug_log(f"[VALIDATE] All previously orphaned orders now tracked, clearing orphan tracking")
                    self.orphaned_order_timestamps.clear()

            # FULL VALIDATION (every 10th reconciliation): Detailed comparison
            if self._validation_counter % 10 != 0:
                return

            self._debug_log(f"[VALIDATE] ===== FULL API VALIDATION =====")

            # Compare counts
            tracked_count = len(self.active_orders)
            api_count = len(api_active_orders)

            if tracked_count != api_count:
                logger.warning(
                    f"⚠️ Order count mismatch: Tracked={tracked_count}, API={api_count}"
                )
                self._debug_log(f"[VALIDATE] Count mismatch: tracked={tracked_count}, api={api_count}")

            # Check for phantom orders (tracked but not on chain)
            missing_in_api = tracked_order_ids - api_order_ids

            if missing_in_api:
                logger.warning(f"⚠️ Orders tracked but not on chain: {missing_in_api}")
                self._debug_log(f"[VALIDATE] Missing in API (phantom orders): {missing_in_api}")
                for cloid, info in list(self.active_orders.items()):
                    if info.order_id in missing_in_api:
                        logger.warning(f"Cleaning up phantom order: {cloid}")
                        del self.active_orders[cloid]
                        self._debug_log(f"[VALIDATE] Cleaned up phantom: {cloid}")

            if tracked_count == api_count and not missing_in_api:
                self._debug_log(f"[VALIDATE] ✓ All orders match (full validation)!")

            self._debug_log(f"[VALIDATE] ===== END FULL VALIDATION =====")

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
            for cloid, (_, timestamp) in self.preregistered_orders.items():
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

                # Get reference price from configured oracle
                reference_price = self.oracle_service.get_price(
                    self.market_config.market_address,
                    self.oracle_source
                )

                if reference_price is None:
                    logger.warning("Could not fetch reference price, skipping iteration")
                    await asyncio.sleep(1.0)
                    continue

                logger.info(f"Iteration {iteration}: Price=${reference_price:.5f}")

                # Get current on-chain active orders
                on_chain_orders = self.client.user.get_active_orders()

                # Use PropMaintain logic to generate orders (only cancels orders below cancel threshold)
                all_orders, num_cancels, num_new_orders = self._generate_orders_with_prop_maintain(
                    reference_price, on_chain_orders
                )

                logger.info(f"PropMaintain: {num_cancels} cancels, {num_new_orders} new orders")

                # Validate balance before placing orders
                if all_orders:
                    all_orders = await self._filter_orders_by_balance(all_orders, on_chain_orders)

                if all_orders:
                    # Debug: Check orders before sending
                    logger.info(f"Order details before sending:")
                    for order in all_orders:
                        order_type_str = "CANCEL" if order.order_type == OrderType.CANCEL else "LIMIT"
                        logger.info(f"  {order.cloid}: type={order_type_str}, side={order.side}, price={order.price}, size={order.size}")

                    # PRE-REGISTER new orders BEFORE sending (handles immediate fills)
                    for order in all_orders:
                        if order.order_type == OrderType.LIMIT and order.size is not None:
                            # Skip cancels, only pre-register new limit orders
                            self.preregistered_orders[order.cloid] = (order.size, time.time())
                            self.order_sizes[order.cloid] = order.size
                            self._debug_log(f"[PRESEND] Pre-registered {order.cloid} with size {order.size}")

                    # Single transaction for cancel + place
                    txhash = await self.client.place_orders(
                        all_orders,
                        post_only=False,
                        price_rounding="default",
                    )
                    logger.info(f"Transaction hash: {txhash}")

                    # Print PnL
                    self.pnl_tracker.print_pnl()
                else:
                    logger.debug("No order updates needed (PropMaintain kept existing orders)")

                # Sleep 1 second
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    pass

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(1.0)

    def _generate_orders_with_prop_maintain(self, reference_price: float, on_chain_orders: list) -> tuple[list[Order], int, int]:
        """
        Generate orders using PropMaintain logic: only cancel orders below cancel threshold.

        Args:
            reference_price: Current fair/reference price
            on_chain_orders: List of active orders from API

        Returns:
            tuple: (all_orders, num_cancels, num_new_orders)
        """
        all_orders = []
        total_cancels = 0
        total_new_orders = 0

        # Build map of on-chain orders by cloid (for our orders only)
        on_chain_by_cloid = {}
        on_chain_order_ids = set()

        for order in on_chain_orders:
            order_id = int(order.get('orderid', 0))
            if order_id in self.order_id_to_cloid:
                cloid = self.order_id_to_cloid[order_id]
                on_chain_by_cloid[cloid] = order
                on_chain_order_ids.add(order_id)

        # For each quoter, check if existing orders meet cancel threshold
        for quoter in self.quoters:
            # Get cancel thresholds (edges below which we cancel existing orders)
            bid_cancel_edge, ask_cancel_edge = quoter.get_cancel_edges(self.bot_config.prop_maintain)

            # Find existing orders for this quoter (by baseline_edge_bps in cloid)
            quoter_prefix_bid = f"bid-{quoter.baseline_edge_bps}-"
            quoter_prefix_ask = f"ask-{quoter.baseline_edge_bps}-"

            existing_bid_cloid = None
            existing_ask_cloid = None
            need_bid = True
            need_ask = True

            # Primary: search active_cloids (callback-driven, no RPC lag).
            # This is the reliable source - it's updated immediately when callbacks fire,
            # unlike on_chain_by_cloid which requires RPC to reflect the mined block.
            for cloid in list(self.active_cloids):
                if cloid.startswith(quoter_prefix_bid):
                    existing_bid_cloid = cloid
                elif cloid.startswith(quoter_prefix_ask):
                    existing_ask_cloid = cloid

            # Fallback: order just sent but ORDER_PLACED callback not yet received
            if existing_bid_cloid is None:
                for cloid in self.preregistered_orders:
                    if cloid.startswith(quoter_prefix_bid):
                        existing_bid_cloid = cloid
                        break
            if existing_ask_cloid is None:
                for cloid in self.preregistered_orders:
                    if cloid.startswith(quoter_prefix_ask):
                        existing_ask_cloid = cloid
                        break

            # Check bid: calculate edge and compare to cancel threshold
            if existing_bid_cloid and existing_bid_cloid in on_chain_by_cloid:
                order = on_chain_by_cloid[existing_bid_cloid]
                order_price = float(order.get('price', 0)) / self.market_config.price_precision
                order_edge = quoter.calculate_order_edge(order_price, OrderSide.BUY, reference_price)

                if order_edge >= bid_cancel_edge:
                    # Edge is good, keep the order
                    need_bid = False
                    logger.debug(
                        f"Quoter {quoter.baseline_edge_bps}bps: Keeping bid @ {order_price:.6f} "
                        f"(edge={order_edge:.1f} >= cancel_threshold={bid_cancel_edge:.1f})"
                    )
                else:
                    # Edge too low, cancel it
                    all_orders.append(Order(cloid=existing_bid_cloid, order_type=OrderType.CANCEL))
                    total_cancels += 1
                    # Proactively remove from active_cloids so the next iteration doesn't
                    # find this stale cloid and mistakenly think the slot is still filled.
                    self.active_cloids.discard(existing_bid_cloid)
                    logger.debug(
                        f"Quoter {quoter.baseline_edge_bps}bps: Cancelling bid @ {order_price:.6f} "
                        f"(edge={order_edge:.1f} < cancel_threshold={bid_cancel_edge:.1f})"
                    )
            elif existing_bid_cloid and existing_bid_cloid in self.preregistered_orders:
                # Order just sent, awaiting on-chain confirmation - don't place another
                need_bid = False
                logger.debug(f"Quoter {quoter.baseline_edge_bps}bps: Bid pending confirmation, holding")
            elif existing_bid_cloid:
                # In active_cloids but not in on_chain_by_cloid - likely API lag.
                # Trust active_cloids (ORDER_PLACED callback = order IS on chain).
                need_bid = False
                logger.debug(f"Quoter {quoter.baseline_edge_bps}bps: Bid in active_cloids (API lag?), holding")

            # Check ask: calculate edge and compare to cancel threshold
            if existing_ask_cloid and existing_ask_cloid in on_chain_by_cloid:
                order = on_chain_by_cloid[existing_ask_cloid]
                order_price = float(order.get('price', 0)) / self.market_config.price_precision
                order_edge = quoter.calculate_order_edge(order_price, OrderSide.SELL, reference_price)

                if order_edge >= ask_cancel_edge:
                    # Edge is good, keep the order
                    need_ask = False
                    logger.debug(
                        f"Quoter {quoter.baseline_edge_bps}bps: Keeping ask @ {order_price:.6f} "
                        f"(edge={order_edge:.1f} >= cancel_threshold={ask_cancel_edge:.1f})"
                    )
                else:
                    # Edge too low, cancel it
                    all_orders.append(Order(cloid=existing_ask_cloid, order_type=OrderType.CANCEL))
                    total_cancels += 1
                    # Proactively remove from active_cloids (same reason as bid above)
                    self.active_cloids.discard(existing_ask_cloid)
                    logger.debug(
                        f"Quoter {quoter.baseline_edge_bps}bps: Cancelling ask @ {order_price:.6f} "
                        f"(edge={order_edge:.1f} < cancel_threshold={ask_cancel_edge:.1f})"
                    )
            elif existing_ask_cloid and existing_ask_cloid in self.preregistered_orders:
                # Order just sent, awaiting on-chain confirmation - don't place another
                need_ask = False
                logger.debug(f"Quoter {quoter.baseline_edge_bps}bps: Ask pending confirmation, holding")
            elif existing_ask_cloid:
                # In active_cloids but not in on_chain_by_cloid - likely API lag.
                # Trust active_cloids (ORDER_PLACED callback = order IS on chain).
                need_ask = False
                logger.debug(f"Quoter {quoter.baseline_edge_bps}bps: Ask in active_cloids (API lag?), holding")

            # Generate new orders for sides that need updating
            new_quoter_orders = quoter.generate_orders(reference_price, need_bid=need_bid, need_ask=need_ask)
            if new_quoter_orders:
                all_orders.extend(new_quoter_orders)
                total_new_orders += len(new_quoter_orders)
                logger.debug(
                    f"Quoter {quoter.baseline_edge_bps}bps: Generating {len(new_quoter_orders)} new orders "
                    f"(bid={'yes' if need_bid else 'no'}, ask={'yes' if need_ask else 'no'})"
                )

        return all_orders, total_cancels, total_new_orders

    async def _filter_orders_by_balance(self, orders: list[Order], on_chain_orders: list) -> list[Order]:
        """
        Filter orders to only include those we have sufficient balance for.

        Args:
            orders: List of orders (cancels + new orders)
            on_chain_orders: Current on-chain orders

        Returns:
            Filtered list of orders we can afford
        """
        # Get current margin balances
        base_wei, quote_wei = await self.client.user.get_margin_balances()
        base_balance = base_wei / (10 ** self.market_config.base_token_decimals)
        quote_balance = quote_wei / (10 ** self.market_config.quote_token_decimals)

        # Margin balance IS the free balance - tokens in margin are fully available.
        # Existing orders have already been deducted from margin when they were placed.
        free_base = base_balance
        free_quote = quote_balance

        logger.debug(f"Margin balance (free): base={free_base:.2f}, quote={free_quote:.2f}")

        # Build cloid lookup for cancels
        on_chain_by_cloid = {}
        for order in on_chain_orders:
            order_id = int(order.get('orderid', 0))
            if order_id in self.order_id_to_cloid:
                cloid = self.order_id_to_cloid[order_id]
                on_chain_by_cloid[cloid] = order

        # Separate cancels and new orders
        cancels = [o for o in orders if o.order_type == OrderType.CANCEL]
        new_orders = [o for o in orders if o.order_type == OrderType.LIMIT]

        # Cancels return tokens to margin - add them to available balance
        for cancel_order in cancels:
            if cancel_order.cloid in on_chain_by_cloid:
                order = on_chain_by_cloid[cancel_order.cloid]
                is_buy = order.get("isbuy", False)
                size = float(order.get("size", 0)) / self.market_config.size_precision
                price = float(order.get("price", 0)) / self.market_config.price_precision

                if is_buy:
                    free_quote += size * price
                    logger.debug(f"Cancel {cancel_order.cloid} returns {size * price:.2f} quote to margin")
                else:
                    free_base += size
                    logger.debug(f"Cancel {cancel_order.cloid} returns {size:.2f} base to margin")

        logger.debug(f"Available after cancels: base={free_base:.2f}, quote={free_quote:.2f}")

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

        logger.debug(f"New orders require: base={required_base:.2f}, quote={required_quote:.2f}")

        # Filter orders based on available balance
        filtered_orders = list(cancels)  # Always include cancels
        skipped_buys = 0
        skipped_sells = 0

        # Add buy orders only if we have enough quote balance
        if required_quote <= free_quote:
            filtered_orders.extend(buy_orders)
            logger.debug(f"✓ All {len(buy_orders)} buy orders can be placed")
        else:
            skipped_buys = len(buy_orders)
            logger.warning(
                f"⚠️  Insufficient quote balance for buy orders. "
                f"Need {required_quote:.2f}, have {free_quote:.2f}. "
                f"Skipping {skipped_buys} buy orders."
            )

        # Add sell orders only if we have enough base balance
        if required_base <= free_base:
            filtered_orders.extend(sell_orders)
            logger.debug(f"✓ All {len(sell_orders)} sell orders can be placed")
        else:
            skipped_sells = len(sell_orders)
            logger.warning(
                f"⚠️  Insufficient base balance for sell orders. "
                f"Need {required_base:.2f}, have {free_base:.2f}. "
                f"Skipping {skipped_sells} sell orders."
            )

        if skipped_buys > 0 or skipped_sells > 0:
            logger.info(
                f"Balance filter: {len(filtered_orders)} orders kept "
                f"({len(cancels)} cancels, {len(filtered_orders) - len(cancels)} new), "
                f"{skipped_buys + skipped_sells} skipped"
            )

        return filtered_orders

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

        # Save position state before shutdown
        if hasattr(self, 'position_tracker') and self.position_tracker:
            try:
                self.position_tracker.save_state()
                total_pos = self.position_tracker.get_current_position() + self.position_tracker.get_start_position()
                logger.info(f"💾 Position state saved: {total_pos:.2f}")
            except Exception as e:
                logger.error(f"Failed to save position state on shutdown: {e}")

        # Stop Kuru WebSocket
        if hasattr(self, 'kuru_price_source') and self.kuru_price_source:
            try:
                self.kuru_price_source.stop()
                logger.success("✓ Kuru WebSocket stopped")
            except Exception as e:
                logger.error(f"Failed to stop Kuru WebSocket: {e}")

        # Stop client
        if self.client:
            await self.client.stop()
            logger.success("✓ Client stopped")
