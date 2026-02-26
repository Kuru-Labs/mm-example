from typing import Optional
from pathlib import Path
from datetime import datetime
from decimal import Decimal
import json
from loguru import logger


def _to_decimal(value) -> Decimal:
    """Convert numeric values to Decimal without binary float artifacts."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class PositionTracker:
    """
    Tracks position changes based on order fill events.

    Position tracking is callback-based - updates occur when orders are filled.
    Buy orders increase position, sell orders decrease position.
    """

    def __init__(self, starting_position: float | Decimal = 0.0):
        """
        Initialize position tracker.

        Args:
            starting_position: Initial position in base currency
        """
        self.current_position = _to_decimal(starting_position)  # Total position in base currency
        self.quote_position = Decimal("0")  # Net quote spent/received

        # Setup debug log file
        debug_log_dir = Path("tracking")
        debug_log_dir.mkdir(exist_ok=True)
        self.debug_log_path = debug_log_dir / "position_tracker_debug.log"
        self.state_file_path = debug_log_dir / "position_state.json"

        # Create/clear debug log file
        with open(self.debug_log_path, 'w') as f:
            f.write(f"=== Position Tracker Debug Log - Started {datetime.now().isoformat()} ===\n")
            f.write(f"Initial position: {float(self.current_position):.6f}\n\n")

    def _debug_log(self, message: str) -> None:
        """Write to both logger and debug file."""
        logger.debug(message)
        with open(self.debug_log_path, 'a') as f:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            f.write(f"[{timestamp}] {message}\n")

    def update_position(self, side, filled_size: float | Decimal, price: float | Decimal) -> None:
        """
        Update position based on a trade fill.

        Args:
            side: OrderSide.BUY or OrderSide.SELL
            filled_size: Amount that was filled
            price: Fill price
        """
        # Import here to avoid circular dependency
        import sys
        from pathlib import Path
        our_src = Path(__file__).resolve().parent.parent.parent
        if str(our_src) not in sys.path:
            sys.path.insert(0, str(our_src))
        from mm_bot.kuru_imports import OrderSide

        filled_size_dec = _to_decimal(filled_size)
        price_dec = _to_decimal(price)

        self._debug_log(
            f"[POSITION] BEFORE fill - position: {float(self.current_position):.2f}"
        )
        self._debug_log(
            f"[POSITION] Fill details - side: {side.value if hasattr(side, 'value') else side}, "
            f"filled_size: {float(filled_size_dec):.2f}, price: {float(price_dec):.6f}"
        )

        if side == OrderSide.BUY:
            self.current_position += filled_size_dec
            self.quote_position -= price_dec * filled_size_dec
            self._debug_log(
                f"[POSITION] AFTER BUY - position: {float(self.current_position):.2f} "
                f"(+{float(filled_size_dec):.2f})"
            )
            logger.info(
                f"Position update (BUY): +{float(filled_size_dec)} base @ {float(price_dec)} | "
                f"Total: {float(self.get_current_position())}"
            )
        elif side == OrderSide.SELL:
            self.current_position -= filled_size_dec
            self.quote_position += price_dec * filled_size_dec
            self._debug_log(
                f"[POSITION] AFTER SELL - position: {float(self.current_position):.2f} "
                f"(-{float(filled_size_dec):.2f})"
            )
            logger.info(
                f"Position update (SELL): -{float(filled_size_dec)} base @ {float(price_dec)} | "
                f"Total: {float(self.get_current_position())}"
            )

        self._debug_log(
            f"[POSITION] New position: {float(self.current_position):.2f}\n"
        )

        # Auto-save state after each position update
        self.save_state()


    def get_current_position(self) -> Decimal:
        """
        Get total current position in base currency.

        Returns:
            Total position in base currency
        """
        return self.current_position

    def get_quote_position(self) -> Decimal:
        """
        Get net quote position (positive = received, negative = spent).

        Returns:
            Net quote position
        """
        return self.quote_position

    def save_state(self) -> None:
        """
        Save position state to JSON file for persistence across restarts.
        """
        try:
            state = {
                'current_position': str(self.current_position),
                'quote_position': str(self.quote_position),
                'last_updated': datetime.now().isoformat()
            }

            with open(self.state_file_path, 'w') as f:
                json.dump(state, f, indent=2)

            logger.debug(f"Position state saved: position={float(self.current_position):.2f}")
        except Exception as e:
            logger.error(f"Failed to save position state: {e}")

    @classmethod
    def load_state(cls, state_file_path: Path) -> Optional[dict]:
        """
        Load position state from JSON file.

        Supports both old format (with start_position/current_position split)
        and new format (single current_position).

        Args:
            state_file_path: Path to the state file

        Returns:
            Dict with state data, or None if file doesn't exist or is invalid
        """
        try:
            if not state_file_path.exists():
                logger.info("No saved position state found (first run)")
                return None

            with open(state_file_path, 'r') as f:
                state = json.load(f)

            # Migration: Handle old format with start_position + current_position
            if 'total_position' in state:
                # Old format - use total_position
                position = _to_decimal(state.get('total_position', "0"))
                logger.info(f"Migrating old position state format (using total_position)")
            elif 'start_position' in state:
                # Old format - calculate total
                start = _to_decimal(state.get('start_position', "0"))
                current = _to_decimal(state.get('current_position', "0"))
                position = start + current
                logger.info(f"Migrating old position state format (start + current)")
            else:
                # New format
                position = _to_decimal(state.get('current_position', "0"))

            logger.info(
                f"Loaded position state: position={float(position):.2f}, "
                f"last_updated={state.get('last_updated', 'unknown')}"
            )

            # Return normalized format
            return {
                'current_position': str(position),
                'quote_position': state.get('quote_position', "0"),
                'last_updated': state.get('last_updated')
            }
        except Exception as e:
            logger.error(f"Failed to load position state: {e}")
            logger.warning("Starting with fresh position state")
            return None
